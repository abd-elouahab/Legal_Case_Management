"""Error tracking: what is failing, how often, and since when.

``22-monitoring.md``'s Error Tracking section asks for four things to be captured
— unhandled exceptions, failed background jobs, failed external service calls,
and failed WebSocket operations — with *"sufficient diagnostic information"*,
while *"users should never receive internal implementation details"*. Those two
requirements pull in opposite directions, and this module is where the line is
drawn: **the detail goes to the log, the shape goes here, and neither goes to a
caller.**

**Failures are grouped, not listed.** A tracker that appended a row per exception
would be a log with extra steps, and the question an operator has is never *"what
was the four hundredth failure?"* — it is *"what is broken, how long has it been
broken, and is it getting worse?"* So failures are folded onto a
:func:`~core.observability.error_fingerprint`, built from the exception's type
and where it was raised and deliberately **not** from its message: a message
usually carries the identifier of whatever was being worked on, so fingerprinting
on one would produce a group per request and answer none of those questions.

**What a group keeps is bounded and safe.** The exception type, the location, the
component, the category, a first-seen and last-seen timestamp, a count, and one
bounded, single-line, redacted sample message. **No traceback and no request
body**: the traceback is written by the handler that caught the exception, beside
the request id and trace id that lead back to this group, and keeping a second
copy in a memory buffer would double the exposure of the string most likely to
quote a case file.

**The buffer is bounded and evicts by staleness.** A platform failing in a
thousand distinct ways has a bigger problem than its monitoring, and the groups
worth keeping in that situation are the ones still happening — so the eviction
victim is the group whose last occurrence is oldest, never the one with the
lowest count. A rare failure that just happened is news; a common one that
stopped an hour ago is history.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from core.observability import (
    ErrorCategory,
    MetricName,
    MonitoringComponent,
    error_fingerprint,
    redact_text,
    truncate,
)
from core.tracing import current_trace_context
from services.metrics_registry import MetricsRegistry, NullMetricsRegistry

__all__ = [
    "ErrorSnapshot",
    "ErrorTracker",
    "InMemoryErrorTracker",
    "NullErrorTracker",
    "TrackedError",
    "get_error_tracker",
    "reset_error_tracker",
]

#: Distinct failure groups retained. Past this the least recently seen is
#: evicted — see the module docstring for why staleness rather than rarity.
_DEFAULT_MAX_GROUPS: Final[int] = 200
#: Longest a recorded operation name may be. Chosen in code, so this bounds a bug.
_MAX_OPERATION: Final[int] = 120
#: Longest a recorded source location may be.
_MAX_LOCATION: Final[int] = 200


@dataclass(frozen=True, slots=True)
class TrackedError:
    """One *class* of failure, as the monitoring endpoint reports it."""

    fingerprint: str
    category: ErrorCategory
    component: MonitoringComponent
    #: The exception's class name — ``IntegrityError``, ``TimeoutError``. Never
    #: its module path, which says where a library lives rather than what broke.
    exception_type: str
    #: ``repositories/case.py:118``, when it could be determined. The single most
    #: useful field on this record, and the one that makes the fingerprint stable
    #: across the many call sites one exception type has.
    location: str | None
    #: What the platform was doing — a route template, a job name, a provider
    #: call. Never a resource identifier.
    operation: str | None
    #: One bounded, redacted, single-line sample. The *most recent*, deliberately:
    #: a group's oldest message describes the failure that started it, and the
    #: newest describes the one somebody is looking at right now.
    sample_message: str | None
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    #: The status the platform answered with, when the failure had a response.
    #: ``None`` for a background job, which has no caller to answer.
    status_code: int | None = None
    #: The trace the most recent occurrence belonged to, when there was one. This
    #: is the handle that turns *"this is failing"* into *"here is one of them,
    #: end to end"* — and it is why tracing and error tracking are one feature.
    last_trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class ErrorSnapshot:
    """Every failure group and the totals, at one instant."""

    since: datetime
    total_errors: int
    errors_by_category: dict[str, int]
    errors_by_component: dict[str, int]
    #: Most recently seen first.
    groups: tuple[TrackedError, ...]
    #: Groups evicted to stay inside the ceiling. Non-zero means this list is a
    #: sample rather than a census, which is worth knowing before drawing a
    #: conclusion from it.
    evicted_groups: int

    @property
    def distinct_errors(self) -> int:
        """How many *kinds* of failure are being tracked.

        The number that distinguishes *"one thing is broken and happened nine
        hundred times"* from *"nine hundred things are broken"*, which are the
        same total and completely different situations.
        """
        return len(self.groups)


class ErrorTracker(Protocol):
    """What the exception handlers and workers require of a failure recorder.

    One recording method and one read. Note what :meth:`record` does **not**
    take: no request body, no user, no case, no traceback, no exception object
    the tracker might stringify at leisure. The caller extracts what is safe and
    hands over exactly that — the same structural guarantee every recorder on this
    platform makes.
    """

    def record(
        self,
        *,
        category: ErrorCategory,
        component: MonitoringComponent,
        exception_type: str,
        message: str | None = None,
        location: str | None = None,
        operation: str | None = None,
        status_code: int | None = None,
    ) -> str:
        """Record one occurrence and return the group's fingerprint."""
        ...

    def snapshot(self) -> ErrorSnapshot:
        """Read the groups and the totals."""
        ...


class InMemoryErrorTracker:
    """Process-local failure groups, guarded by a lock.

    **Never raises.** :meth:`record` is called from exception handlers and from
    ``except`` blocks in background workers — the two places in a codebase where
    a second exception is hardest to recover from — so every path here is inside
    a ``try`` and the worst outcome is a failure that was not counted.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(
        self,
        *,
        metrics: MetricsRegistry | None = None,
        max_groups: int = _DEFAULT_MAX_GROUPS,
    ) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._metrics = metrics or NullMetricsRegistry()
        self._max_groups = max(1, max_groups)
        self._groups: dict[str, _Group] = {}
        self._total = 0
        self._by_category: dict[str, int] = {}
        self._by_component: dict[str, int] = {}
        self._evicted = 0
        self._sequence = 0

    def record(
        self,
        *,
        category: ErrorCategory,
        component: MonitoringComponent,
        exception_type: str,
        message: str | None = None,
        location: str | None = None,
        operation: str | None = None,
        status_code: int | None = None,
    ) -> str:
        """Fold one occurrence into its group, creating the group if needed."""
        try:
            clean_type = truncate(str(exception_type), 80) or "Exception"
            clean_location = truncate(str(location), _MAX_LOCATION) if location else None
            fingerprint = error_fingerprint(
                category=category,
                component=component,
                exception_type=clean_type,
                location=clean_location,
            )
            now = datetime.now(UTC)
            trace = current_trace_context()

            with self._lock:
                self._total += 1
                self._by_category[category.value] = self._by_category.get(category.value, 0) + 1
                self._by_component[component.value] = (
                    self._by_component.get(component.value, 0) + 1
                )

                group = self._groups.get(fingerprint)
                if group is None:
                    if len(self._groups) >= self._max_groups:
                        self._evict()
                    group = _Group(
                        fingerprint=fingerprint,
                        category=category,
                        component=component,
                        exception_type=clean_type,
                        location=clean_location,
                        first_seen=now,
                        last_seen=now,
                    )
                    self._groups[fingerprint] = group

                self._sequence += 1
                group.occurrences += 1
                group.last_seen = now
                group.sequence = self._sequence
                group.operation = (
                    truncate(str(operation), _MAX_OPERATION) if operation else group.operation
                )
                group.sample_message = redact_text(message) if message else group.sample_message
                group.status_code = status_code if status_code is not None else group.status_code
                group.last_trace_id = trace.trace_id if trace else group.last_trace_id

            self._metrics.increment(
                MetricName.ERRORS_TOTAL,
                labels={"category": category.value, "component": component.value},
            )
            if category is ErrorCategory.UNHANDLED:
                self._metrics.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL)
            return fingerprint
        except Exception:  # pragma: no cover - defensive; see the class docstring
            return ""

    def _evict(self) -> None:
        """Drop the group whose last occurrence is oldest. Lock held."""
        oldest = min(self._groups.values(), key=lambda group: group.sequence)
        del self._groups[oldest.fingerprint]
        self._evicted += 1

    def snapshot(self) -> ErrorSnapshot:
        """Read the groups and the totals as one consistent value."""
        with self._lock:
            # Sorted before freezing, because the ordering key is deliberately
            # not part of what a reader is served: `sequence` is bookkeeping, and
            # `TrackedError` carries the timestamps somebody actually reads.
            ordered = sorted(
                self._groups.values(),
                # By the monotonic sequence rather than the timestamp; see
                # `_Group.sequence` for why that distinction is load-bearing.
                key=lambda group: group.sequence,
                reverse=True,
            )
            groups = [group.freeze() for group in ordered]
            return ErrorSnapshot(
                since=self._since,
                total_errors=self._total,
                errors_by_category=dict(self._by_category),
                errors_by_component=dict(self._by_component),
                groups=tuple(groups),
                evicted_groups=self._evicted,
            )

    def reset(self) -> None:
        """Discard every group and counter. For tests."""
        with self._lock:
            self._since = datetime.now(UTC)
            self._groups.clear()
            self._total = 0
            self._by_category.clear()
            self._by_component.clear()
            self._evicted = 0
            self._sequence = 0


@dataclass(slots=True)
class _Group:
    """A failure group while it is being accumulated."""

    fingerprint: str
    category: ErrorCategory
    component: MonitoringComponent
    exception_type: str
    location: str | None
    first_seen: datetime
    last_seen: datetime
    #: Monotonic sequence number of this group's most recent occurrence, used
    #: only to order the list.
    #:
    #: ``last_seen`` alone cannot do it, for the reason
    #: :attr:`~services.tracer.RecordedSpan.order` records: the system clock's
    #: resolution is roughly 15 ms on Windows, so two failures inside one request
    #: carry the same timestamp and a stable sort then reports them in dictionary
    #: order. A counter is exact and cannot step backwards.
    sequence: int = 0
    occurrences: int = 0
    operation: str | None = None
    sample_message: str | None = None
    status_code: int | None = None
    last_trace_id: str | None = None

    def freeze(self) -> TrackedError:
        """Return the immutable record a snapshot carries."""
        return TrackedError(
            fingerprint=self.fingerprint,
            category=self.category,
            component=self.component,
            exception_type=self.exception_type,
            location=self.location,
            operation=self.operation,
            sample_message=self.sample_message,
            occurrences=self.occurrences,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            status_code=self.status_code,
            last_trace_id=self.last_trace_id,
        )


class NullErrorTracker:
    """A tracker that records nothing.

    The default for code constructed without observability. Same role and
    reasoning as every other null recorder on this platform.
    """

    def record(
        self,
        *,
        category: ErrorCategory,
        component: MonitoringComponent,
        exception_type: str,
        message: str | None = None,
        location: str | None = None,
        operation: str | None = None,
        status_code: int | None = None,
    ) -> str:
        """Discard the observation."""
        return ""

    def snapshot(self) -> ErrorSnapshot:
        """Report an empty window."""
        return ErrorSnapshot(
            since=datetime.now(UTC),
            total_errors=0,
            errors_by_category={},
            errors_by_component={},
            groups=(),
            evicted_groups=0,
        )


#: The one tracker the process shares.
_shared: InMemoryErrorTracker | None = None
_shared_lock = threading.Lock()


def get_error_tracker() -> InMemoryErrorTracker:
    """Return the process-wide error tracker, creating it on first use.

    Lazily for the reason :func:`~services.tracer.get_tracer` is: it holds the
    metric registry, and constructing it at import time would fix that wiring
    before configuration has been read.
    """
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                from core.config import settings
                from services.metrics_registry import get_metrics_registry

                _shared = InMemoryErrorTracker(
                    metrics=get_metrics_registry(),
                    max_groups=settings.MONITORING_MAX_ERROR_GROUPS,
                )
    return _shared


def reset_error_tracker() -> None:
    """Clear every group. For tests."""
    get_error_tracker().reset()
