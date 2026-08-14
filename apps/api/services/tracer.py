"""Distributed tracing: spans, their nesting, and a bounded recent-trace buffer.

``22-monitoring.md`` asks tracing to follow a request through *"authentication,
authorization, business service, database, external service, response"*. This
module is the mechanism; :mod:`core.middleware`, :mod:`services.database_metrics`,
and the provider boundaries are where it is attached, so no business module holds
a tracer or knows one exists.

**A span here is a measurement, not a message.** Starting one costs a
:class:`~core.tracing.TraceContext` (two ``secrets.token_hex`` calls), a
dictionary, and a ``perf_counter``; ending one records a duration into the metric
registry and, if the trace is still being kept, appends a small frozen record.
There is no exporter, no queue, no background flush, and nothing that can block —
which is what makes *"monitoring must never become a dependency of the
application"* a property of the design rather than a claim.

**What is kept, and why it is so little.** The buffer holds the most recent
:data:`MONITORING_TRACE_BUFFER` traces and drops the oldest, and each trace holds
at most a bounded number of spans. Keeping everything would make a tracer a leak
proportional to traffic; keeping nothing would make the endpoint useless. What
the buffer is *for* is the question an operator actually asks — *"that request
took nine seconds, where did it go?"* — and a few hundred recent traces answer it
while a week of them needs a backend this feature is deliberately preparing for
rather than being.

**Span attributes are redacted on the way in**, through
:func:`~core.observability.redact_mapping`, and bounded in number. A span is the
one place in monitoring where a caller supplies free-form keys, so it is the one
place that has to assume somebody will eventually attach the wrong thing.

**Every method swallows its own failures.** A tracer that raised would turn an
observation into an outage; :meth:`InMemoryTracer.span` in particular is a
context manager wrapped around real work, so a bug in it would fail the work.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol, Self

from core.observability import (
    MetricName,
    MonitoringComponent,
    redact_mapping,
    redact_text,
    truncate,
)
from core.tracing import (
    SpanKind,
    SpanStatus,
    TraceContext,
    bind_trace_context,
    current_trace_context,
    new_span_id,
    new_trace_id,
    reset_trace_context,
)
from services.metrics_registry import MetricsRegistry, NullMetricsRegistry

__all__ = [
    "InMemoryTracer",
    "NullTracer",
    "RecordedSpan",
    "RecordedTrace",
    "Span",
    "TraceSnapshot",
    "Tracer",
    "get_tracer",
    "reset_tracer",
]

#: Traces retained. Small on purpose — see the module docstring.
_DEFAULT_TRACE_BUFFER: Final[int] = 200
#: Spans kept per trace. A request that opened more than this has a loop in it,
#: and the first fifty are what says where.
_MAX_SPANS_PER_TRACE: Final[int] = 50
#: Attributes kept per span, and the longest a rendered attribute value may be.
_MAX_SPAN_ATTRIBUTES: Final[int] = 24
_MAX_ATTRIBUTE_LENGTH: Final[int] = 200
#: Longest a span name may be. Names are chosen in code, so this bounds a bug.
_MAX_SPAN_NAME: Final[int] = 120


@dataclass(slots=True)
class Span:
    """One timed unit of work, and the handle instrumented code holds.

    Mutable — unlike almost everything else in this codebase — because that is
    what a span *is*: a measurement that is open, accumulates what is learned
    about it, and then closes. What is *kept* is the frozen
    :class:`RecordedSpan` produced at the end, so nothing mutable escapes.
    """

    name: str
    component: MonitoringComponent
    kind: SpanKind
    context: TraceContext
    started_at: datetime
    #: Monotonic start, kept separately from ``started_at``: a wall clock can
    #: step backwards over an NTP correction and turn a fast span into a negative
    #: duration, and durations are the only thing anybody reads from these.
    _started: float
    status: SpanStatus = SpanStatus.UNSET
    attributes: dict[str, Any] = field(default_factory=dict)
    #: Set when :meth:`record_exception` is called. The type's name and a bounded,
    #: single-line message — never a traceback, which goes to the log where the
    #: full context already is.
    error_type: str | None = None
    error_message: str | None = None
    _ended: bool = False
    _duration_ms: float | None = None

    # ------------------------------------------------------------- mutations #

    def set_attribute(self, key: str, value: Any) -> Self:
        """Attach one fact to this span, if there is room and it is safe to keep.

        Returns ``self`` so attributes can be chained onto a ``with`` target.
        Silently ignores everything past :data:`_MAX_SPAN_ATTRIBUTES` — a span
        with two dozen attributes is already telling a complete story, and the
        alternative to a ceiling is a caller in a loop.
        """
        try:
            if len(self.attributes) >= _MAX_SPAN_ATTRIBUTES and key not in self.attributes:
                return self
            redacted = redact_mapping({key: value})
            rendered = redacted[key]
            if isinstance(rendered, str):
                rendered = truncate(rendered, _MAX_ATTRIBUTE_LENGTH)
            self.attributes[key] = rendered
        except Exception:  # pragma: no cover - defensive
            return self
        return self

    def set_attributes(self, attributes: Mapping[str, Any]) -> Self:
        """Attach several facts at once."""
        for key, value in attributes.items():
            self.set_attribute(key, value)
        return self

    def set_status(self, status: SpanStatus) -> Self:
        """Declare how this span ended."""
        self.status = status
        return self

    def record_exception(self, exc: BaseException) -> Self:
        """Mark this span failed and keep what identifies the failure.

        The type and a bounded message, and deliberately **not** a traceback: the
        traceback is written to the log by whichever handler caught the exception,
        where it sits beside the request id and the trace id that lead back here.
        Keeping a second copy in a memory buffer would double the exposure of the
        one string in this system most likely to quote a case file.
        """
        self.status = SpanStatus.ERROR
        self.error_type = type(exc).__name__
        self.error_message = redact_text(str(exc))
        return self

    # ------------------------------------------------------------ properties #

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration, or ``None`` while the span is still open."""
        return self._duration_ms

    @property
    def trace_id(self) -> str:
        """The trace this span belongs to."""
        return self.context.trace_id

    @property
    def span_id(self) -> str:
        """This span's own identifier."""
        return self.context.span_id

    # --------------------------------------------------------------- closing #

    def _close(self) -> float:
        """Stop the clock. Idempotent, and returns the duration in milliseconds."""
        if not self._ended:
            self._duration_ms = round((time.perf_counter() - self._started) * 1000, 3)
            self._ended = True
        return self._duration_ms or 0.0

    def freeze(self) -> RecordedSpan:
        """Return the immutable record of this span."""
        return RecordedSpan(
            name=self.name,
            component=self.component,
            kind=self.kind,
            span_id=self.context.span_id,
            parent_span_id=self.context.parent_span_id,
            started_at=self.started_at,
            duration_ms=self._duration_ms or 0.0,
            status=self.status,
            error_type=self.error_type,
            error_message=self.error_message,
            attributes=dict(self.attributes),
            order=self._started,
        )


@dataclass(frozen=True, slots=True)
class RecordedSpan:
    """A completed span, as the buffer keeps it and the API reports it."""

    name: str
    component: MonitoringComponent
    kind: SpanKind
    span_id: str
    parent_span_id: str | None
    started_at: datetime
    duration_ms: float
    status: SpanStatus
    error_type: str | None
    error_message: str | None
    attributes: dict[str, Any]
    #: Monotonic start, used only to order the spans within a trace.
    #:
    #: ``started_at`` cannot do that job and the reason is a real finding rather
    #: than caution: the system clock's resolution on Windows is roughly 15 ms, so
    #: three spans opened inside one request routinely carry the *same* wall-clock
    #: timestamp and a stable sort then falls back to the order they **finished**
    #: in — which puts every child above its own parent. A monotonic counter has
    #: microsecond resolution and cannot step backwards, so it orders them the way
    #: they actually happened. Not exposed in the API: a reader wants the
    #: timestamps and the nesting, not this.
    order: float = 0.0


@dataclass(frozen=True, slots=True)
class RecordedTrace:
    """One completed trace: a root span and everything that happened inside it."""

    trace_id: str
    #: The root span's name — what the trace *was*, in one string.
    name: str
    component: MonitoringComponent
    started_at: datetime
    duration_ms: float
    status: SpanStatus
    spans: tuple[RecordedSpan, ...]
    #: Whether the trace was joined from an upstream ``traceparent`` rather than
    #: beginning here. Today this is always false in practice — there is one
    #: service — and it is recorded because the day it is not is the day this
    #: feature's *"prepare for distributed deployments"* is being cashed.
    remote_parent: bool = False
    #: Spans that happened but were not kept, because the trace hit its ceiling.
    dropped_spans: int = 0

    @property
    def failed(self) -> bool:
        """Whether any span in this trace ended in error."""
        return self.status is SpanStatus.ERROR or any(
            span.status is SpanStatus.ERROR for span in self.spans
        )


@dataclass(frozen=True, slots=True)
class TraceSnapshot:
    """What the tracer holds at one instant."""

    since: datetime
    traces_started: int
    traces_recorded: int
    spans_started: int
    #: Spans dropped because their trace was already at its ceiling.
    spans_dropped: int
    #: Most recent first.
    recent: tuple[RecordedTrace, ...]

    @property
    def failed_traces(self) -> int:
        """Retained traces in which something failed."""
        return sum(1 for trace in self.recent if trace.failed)


class Tracer(Protocol):
    """What instrumented code requires of a tracing backend.

    Two members. :meth:`span` is a context manager so that closing a span is the
    language's job rather than a caller's discipline — a span that had to be
    ended explicitly would be a span that leaks on every early return, and the
    early returns are precisely the interesting paths.
    """

    def span(
        self,
        name: str,
        *,
        component: MonitoringComponent = MonitoringComponent.API,
        kind: SpanKind = SpanKind.INTERNAL,
        context: TraceContext | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Any:
        """Open a span around the ``with`` block, and close it however it exits."""
        ...

    def record_completed(
        self,
        name: str,
        *,
        component: MonitoringComponent,
        kind: SpanKind = SpanKind.CLIENT,
        duration_ms: float,
        status: SpanStatus = SpanStatus.UNSET,
        attributes: Mapping[str, Any] | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Attach a span for work that was already measured elsewhere."""
        ...

    def snapshot(self, *, limit: int | None = None) -> TraceSnapshot:
        """Read the recent traces and the counters, newest first."""
        ...


class InMemoryTracer:
    """Process-local spans and a bounded ring of recent traces.

    **Traces are assembled while they are open**, in a dictionary keyed by trace
    identifier, and moved into the ring when their root closes. That is what lets
    a database span recorded from a SQLAlchemy event handler — which has no idea
    which request it is inside — end up under the right request in the buffer: it
    finds its trace through the context variable, and the trace finds its shape
    from the parent identifiers the spans already carry.

    A lock guards both structures, for the reason
    :class:`~services.metrics_registry.InMemoryMetricsRegistry` gives: a snapshot
    has to be internally consistent, and worker threads close spans concurrently
    with a request thread.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(
        self,
        *,
        metrics: MetricsRegistry | None = None,
        buffer_size: int = _DEFAULT_TRACE_BUFFER,
        max_spans_per_trace: int = _MAX_SPANS_PER_TRACE,
    ) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._metrics = metrics or NullMetricsRegistry()
        self._max_spans = max(1, max_spans_per_trace)
        self._recent: deque[RecordedTrace] = deque(maxlen=max(1, buffer_size))
        self._open: dict[str, _OpenTrace] = {}
        self._traces_started = 0
        self._traces_recorded = 0
        self._spans_started = 0
        self._spans_dropped = 0

    # ------------------------------------------------------------------ span #

    @contextmanager
    def span(
        self,
        name: str,
        *,
        component: MonitoringComponent = MonitoringComponent.API,
        kind: SpanKind = SpanKind.INTERNAL,
        context: TraceContext | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Open a span, make it current, and close it however the block exits.

        ``context`` is supplied only by the middleware, which has an inbound
        ``traceparent`` to honour. Everywhere else the parent is found through the
        context variable, which is what makes instrumenting a repository or a
        provider a one-line ``with`` rather than a signature change up the call
        chain — the coupling ``22-monitoring.md`` forbids.

        **The block's exception is recorded and re-raised, never swallowed.** A
        tracer that ate exceptions would be a tracer that changed behaviour, and
        the whole contract of this feature is that it does not.
        """
        span = self._start(name, component=component, kind=kind, context=context)
        if attributes:
            span.set_attributes(attributes)

        token = bind_trace_context(span.context)
        try:
            yield span
        except BaseException as exc:
            # Recorded before re-raising, so the span carries the failure even
            # though the handler that will log it is further out.
            with contextlib.suppress(Exception):
                span.record_exception(exc)
            raise
        finally:
            reset_trace_context(token)
            self._finish(span)

    # -------------------------------------------------- already-finished work #

    def record_completed(
        self,
        name: str,
        *,
        component: MonitoringComponent,
        kind: SpanKind = SpanKind.CLIENT,
        duration_ms: float,
        status: SpanStatus = SpanStatus.UNSET,
        attributes: Mapping[str, Any] | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Attach a span for work that was measured somewhere a ``with`` cannot go.

        The database instrumentation is why this exists: SQLAlchemy reports a
        statement through **two** event callbacks — one before the cursor
        executes and one after — so the work happens between two function calls
        rather than inside a block, and there is nowhere to put a context
        manager. The alternative would be for the tracer to hand out open spans
        that callers must remember to close, which is precisely the leak
        :meth:`span` exists to prevent; a caller that has already measured its own
        duration can simply say so.

        Attaches to the **current** trace and does nothing when there is none: a
        query issued by a startup routine or a background worker outside any
        request has no request to belong to, and inventing a one-span trace for
        each would fill the buffer with noise. Its duration is still recorded as a
        metric, which is where that work is genuinely visible.
        """
        try:
            parent = current_trace_context()
            if parent is None:
                self._metrics.observe(
                    MetricName.SPAN_DURATION_MS,
                    duration_ms,
                    labels={"component": component.value},
                )
                return

            context = parent.child()
            record = RecordedSpan(
                name=truncate(str(name), _MAX_SPAN_NAME),
                component=component,
                kind=kind,
                span_id=context.span_id,
                parent_span_id=context.parent_span_id,
                started_at=datetime.now(UTC),
                duration_ms=round(max(0.0, duration_ms), 3),
                status=status,
                error_type=error_type,
                error_message=redact_text(error_message) if error_message else None,
                attributes=dict(redact_mapping(dict(attributes or {}))),
                # Ordered at the moment it is reported rather than back-dated by
                # its own duration, and the difference matters: a statement that
                # took longer than the span enclosing it has been open would
                # back-date to *before* its own parent, putting a child above the
                # root. Nesting is carried by ``parent_span_id`` regardless, so
                # ordering by report time costs nothing and cannot invert.
                order=time.perf_counter(),
            )

            with self._lock:
                self._spans_started += 1
                trace = self._open.get(context.trace_id)
                if trace is None:
                    return
                if len(trace.spans) >= self._max_spans:
                    trace.dropped += 1
                    self._spans_dropped += 1
                else:
                    trace.spans.append(record)

            self._metrics.increment(
                MetricName.SPANS_STARTED_TOTAL, labels={"component": component.value}
            )
            self._metrics.observe(
                MetricName.SPAN_DURATION_MS,
                record.duration_ms,
                labels={"component": component.value},
            )
        except Exception:  # pragma: no cover - defensive
            return

    # ------------------------------------------------------------- lifecycle #

    def _start(
        self,
        name: str,
        *,
        component: MonitoringComponent,
        kind: SpanKind,
        context: TraceContext | None,
    ) -> Span:
        """Create a span under the right parent. Never raises."""
        try:
            resolved = context or self._derive_context()
            span = Span(
                name=truncate(str(name), _MAX_SPAN_NAME),
                component=component,
                kind=kind,
                context=resolved,
                started_at=datetime.now(UTC),
                _started=time.perf_counter(),
            )

            with self._lock:
                self._spans_started += 1
                trace = self._open.get(resolved.trace_id)
                if trace is None:
                    self._traces_started += 1
                    self._open[resolved.trace_id] = _OpenTrace(
                        trace_id=resolved.trace_id,
                        root_span_id=resolved.span_id,
                        remote_parent=resolved.remote,
                    )

            self._metrics.increment(
                MetricName.SPANS_STARTED_TOTAL, labels={"component": component.value}
            )
            return span
        except Exception:  # pragma: no cover - defensive
            # A span that could not be registered is still a usable timer; it
            # simply will not appear in the buffer.
            return Span(
                name=str(name)[:_MAX_SPAN_NAME],
                component=component,
                kind=kind,
                context=TraceContext(trace_id=new_trace_id(), span_id=new_span_id()),
                started_at=datetime.now(UTC),
                _started=time.perf_counter(),
            )

    @staticmethod
    def _derive_context() -> TraceContext:
        """Return a child of the current context, or a fresh root."""
        parent = current_trace_context()
        if parent is not None:
            return parent.child()
        return TraceContext(trace_id=new_trace_id(), span_id=new_span_id())

    def _finish(self, span: Span) -> None:
        """Close a span, record its duration, and retire its trace if it was the root."""
        duration = span._close()

        with contextlib.suppress(Exception):
            self._metrics.observe(
                MetricName.SPAN_DURATION_MS,
                duration,
                labels={"component": span.component.value},
            )

        try:
            record = span.freeze()
            with self._lock:
                trace = self._open.get(span.context.trace_id)
                if trace is None:
                    return
                if len(trace.spans) >= self._max_spans:
                    trace.dropped += 1
                    self._spans_dropped += 1
                else:
                    trace.spans.append(record)

                if trace.root_span_id != span.context.span_id:
                    return

                # The root closed: the trace is complete and moves to the ring.
                del self._open[span.context.trace_id]
                self._traces_recorded += 1
                self._recent.appendleft(
                    RecordedTrace(
                        trace_id=trace.trace_id,
                        name=record.name,
                        component=record.component,
                        started_at=record.started_at,
                        duration_ms=record.duration_ms,
                        status=record.status,
                        # Chronological within the trace, so it reads as a
                        # sequence rather than in completion order — a child that
                        # finished first would otherwise appear before its parent.
                        # Ordered by the monotonic key rather than the timestamp;
                        # see `RecordedSpan.order` for why that distinction is
                        # load-bearing rather than pedantic.
                        spans=tuple(sorted(trace.spans, key=lambda item: item.order)),
                        remote_parent=trace.remote_parent,
                        dropped_spans=trace.dropped,
                    )
                )
        except Exception:  # pragma: no cover - defensive
            return

    # -------------------------------------------------------------- snapshot #

    def snapshot(self, *, limit: int | None = None) -> TraceSnapshot:
        """Read the counters and the recent traces, most recent first."""
        with self._lock:
            recent = list(self._recent)[: limit if limit and limit > 0 else None]
            return TraceSnapshot(
                since=self._since,
                traces_started=self._traces_started,
                traces_recorded=self._traces_recorded,
                spans_started=self._spans_started,
                spans_dropped=self._spans_dropped,
                recent=tuple(recent),
            )

    def reset(self) -> None:
        """Discard every trace and counter. For tests."""
        with self._lock:
            self._since = datetime.now(UTC)
            self._recent.clear()
            self._open.clear()
            self._traces_started = 0
            self._traces_recorded = 0
            self._spans_started = 0
            self._spans_dropped = 0


@dataclass(slots=True)
class _OpenTrace:
    """A trace whose root span has not closed yet."""

    trace_id: str
    root_span_id: str
    remote_parent: bool
    spans: list[RecordedSpan] = field(default_factory=list)
    dropped: int = 0


class NullTracer:
    """A tracer that records nothing but still produces working spans.

    The distinction matters: instrumented code does ``with tracer.span(...) as
    span: span.set_attribute(...)``, so a null implementation that returned
    ``None`` would make every call site guard. It returns a real
    :class:`Span` that nobody keeps — the same choice
    :class:`~services.metrics_registry.NullMetricsRegistry` makes, for the same
    reason.

    **It deliberately does not bind a trace context**, which is what makes it
    genuinely free: nothing downstream then believes it is inside a trace.
    """

    @contextmanager
    def span(
        self,
        name: str,
        *,
        component: MonitoringComponent = MonitoringComponent.API,
        kind: SpanKind = SpanKind.INTERNAL,
        context: TraceContext | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Yield a span that is never recorded."""
        yield Span(
            name=str(name)[:_MAX_SPAN_NAME],
            component=component,
            kind=kind,
            context=context or TraceContext(trace_id=new_trace_id(), span_id=new_span_id()),
            started_at=datetime.now(UTC),
            _started=time.perf_counter(),
        )

    def record_completed(
        self,
        name: str,
        *,
        component: MonitoringComponent,
        kind: SpanKind = SpanKind.CLIENT,
        duration_ms: float,
        status: SpanStatus = SpanStatus.UNSET,
        attributes: Mapping[str, Any] | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Discard the observation."""

    def snapshot(self, *, limit: int | None = None) -> TraceSnapshot:
        """Report an empty window."""
        return TraceSnapshot(
            since=datetime.now(UTC),
            traces_started=0,
            traces_recorded=0,
            spans_started=0,
            spans_dropped=0,
            recent=(),
        )


#: The one tracer the process shares.
#:
#: Constructed against the shared metric registry, so span durations land in the
#: same snapshot as every other figure — a trace and a histogram of the same work
#: disagreeing about how long it took is the kind of contradiction that makes
#: people stop trusting a monitoring page.
_shared: InMemoryTracer | None = None
_shared_lock = threading.Lock()


def get_tracer() -> InMemoryTracer:
    """Return the process-wide tracer, creating it on first use.

    Lazily, and behind a lock, because it holds a reference to the metric
    registry: constructing it at import time would fix the wiring before
    :mod:`api.deps` has had a chance to say what it should be.
    """
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                from core.config import settings
                from services.metrics_registry import get_metrics_registry

                _shared = InMemoryTracer(
                    metrics=get_metrics_registry(),
                    buffer_size=settings.MONITORING_TRACE_BUFFER,
                    max_spans_per_trace=settings.MONITORING_MAX_SPANS_PER_TRACE,
                )
    return _shared


def reset_tracer() -> None:
    """Clear every recorded trace. For tests."""
    get_tracer().reset()
