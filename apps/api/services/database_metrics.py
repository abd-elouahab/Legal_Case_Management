"""Database instrumentation: query duration, verb, and failures.

``22-monitoring.md``'s Performance Metrics section asks for *"database query
duration"*, and its tracing diagram puts the database on the path a request
follows. Both are satisfied by attaching to the SQLAlchemy **engine** rather than
to any repository — which is the whole design decision in this module, and it is
worth stating why:

* **Every statement is covered, including the ones nobody wrote.** A lazy load, a
  flush during a commit, a pool pre-ping, and a query issued by a background
  worker all go through the engine and none of them goes through a repository
  method somebody could have decorated.
* **No repository changes, and none can.** Sixteen repositories exist; the spec
  forbids monitoring logic in business modules, and an engine listener is the one
  attachment point that observes all of them while being visible in none.
* **It covers the background workers for free.** OCR, indexing, report
  generation, and both delivery channels open their own sessions on their own
  threads, and they are exactly the queries a request-scoped instrumentation
  would miss.

**The statement text is never recorded.** Not in a metric label, not in a span
attribute, not in a log line. A SQL statement on this platform embeds nothing —
parameters are bound, not interpolated — but the *statement* still names tables
and columns, and a slow-query log that quotes one is one edit away from quoting
the parameters too. What is recorded is the **verb**: ``SELECT``, ``INSERT``,
``UPDATE``, ``DELETE``, ``COMMIT``. That is a five-value label, it is what
distinguishes a read-heavy page from a write storm, and it can never grow.

**Instrumentation is idempotent and reversible.** :func:`instrument_engine` may
be called twice — the lifespan calls it, and a test may too — and
:func:`uninstrument_engine` removes the listeners, which is what lets a test
measure without leaving a global side effect behind.
"""

from __future__ import annotations

import time
from typing import Any, Final

import structlog
from sqlalchemy import event
from sqlalchemy.engine import Engine

from core.config import settings
from core.observability import (
    ErrorCategory,
    LogEvent,
    MetricName,
    MonitoringComponent,
)
from core.tracing import SpanKind, SpanStatus
from services.error_tracker import ErrorTracker, NullErrorTracker
from services.metrics_registry import MetricsRegistry, NullMetricsRegistry
from services.tracer import NullTracer, Tracer

logger = structlog.get_logger(__name__)

__all__ = ["instrument_engine", "is_instrumented", "uninstrument_engine"]

#: Attribute the start time is stashed under, on the execution context SQLAlchemy
#: passes to both callbacks. Prefixed, because that object belongs to SQLAlchemy.
_START_ATTRIBUTE: Final[str] = "_monitoring_started_at"

#: Verbs recognised in a statement's first word. Anything else is reported as
#: ``other`` rather than as itself — a bounded label cannot be widened by a
#: statement, which is what stops a stray ``WITH`` or a driver's own probe from
#: creating a series.
_KNOWN_VERBS: Final[frozenset[str]] = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "COMMIT", "ROLLBACK", "BEGIN", "SAVEPOINT"}
)

#: Engines currently instrumented, so a second call is a no-op rather than a
#: second set of listeners quietly doubling every count.
_instrumented: set[int] = set()

#: The listeners, kept so they can be removed again.
_listeners: dict[int, list[tuple[str, Any]]] = {}


def _verb(statement: str) -> str:
    """Return the statement's leading keyword, or ``other``.

    Only the first word is read, and only if it is one of a closed set — see
    :data:`_KNOWN_VERBS`. Nothing else about the statement is examined, which is
    the cheapest possible way to be sure nothing else about it is kept.
    """
    try:
        head = statement.lstrip()[:12].split(None, 1)[0].upper()
    except (AttributeError, IndexError):
        return "other"
    return head if head in _KNOWN_VERBS else "other"


def instrument_engine(
    engine: Engine,
    *,
    metrics: MetricsRegistry | None = None,
    tracer: Tracer | None = None,
    errors: ErrorTracker | None = None,
) -> bool:
    """Attach duration, count, and failure listeners to ``engine``.

    Returns whether listeners were attached — ``False`` when the engine is
    already instrumented or when the feature is switched off, so a caller can log
    the difference rather than guess at it.

    **Never raises.** The lifespan calls this during startup, and an API that
    refused to come up because it could not attach a metrics listener would take
    authentication, cases, and documents down over an observation — the same
    posture :func:`~core.lifespan._start_realtime` takes for the event channel.
    """
    if not (settings.MONITORING_ENABLED and settings.MONITORING_DB_INSTRUMENTATION):
        return False

    key = id(engine)
    if key in _instrumented:
        return False

    registry = metrics or NullMetricsRegistry()
    span_recorder = tracer or NullTracer()
    tracker = errors or NullErrorTracker()

    def before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        _statement: str,
        _parameters: Any,
        context: Any,
        _executemany: bool,
    ) -> None:
        """Start the clock. Never raises."""
        try:
            setattr(context, _START_ATTRIBUTE, time.perf_counter())
        except Exception:  # pragma: no cover - defensive
            return

    def after_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        context: Any,
        _executemany: bool,
    ) -> None:
        """Stop the clock, count the statement, and attach a span. Never raises."""
        try:
            started = getattr(context, _START_ATTRIBUTE, None)
            if started is None:
                return
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            verb = _verb(statement)

            registry.increment(MetricName.DB_QUERIES_TOTAL, labels={"operation": verb})
            registry.observe(
                MetricName.DB_QUERY_DURATION_MS, duration_ms, labels={"operation": verb}
            )
            span_recorder.record_completed(
                f"db {verb.lower()}",
                component=MonitoringComponent.DATABASE,
                kind=SpanKind.CLIENT,
                duration_ms=duration_ms,
                status=SpanStatus.OK,
                attributes={"db.operation": verb},
            )

            if duration_ms >= settings.MONITORING_SLOW_QUERY_MS:
                # The verb and the duration, and deliberately not the statement:
                # see the module docstring. A slow query is found by correlating
                # this line's request id and trace id with the request it was in.
                logger.warning(
                    LogEvent.SLOW_QUERY,
                    operation=verb,
                    duration_ms=duration_ms,
                    component=MonitoringComponent.DATABASE.value,
                )
        except Exception:  # pragma: no cover - defensive
            return

    def handle_error(context: Any) -> None:
        """Record a statement that raised. Never raises, and never suppresses.

        SQLAlchemy's ``handle_error`` may *replace* an exception by setting a
        field on the context; this listener deliberately reads and sets nothing,
        so the original error propagates exactly as it would without
        instrumentation.
        """
        try:
            statement = getattr(context, "statement", "") or ""
            verb = _verb(statement)
            exc = getattr(context, "original_exception", None)

            registry.increment(MetricName.DB_QUERY_ERRORS_TOTAL, labels={"operation": verb})
            span_recorder.record_completed(
                f"db {verb.lower()}",
                component=MonitoringComponent.DATABASE,
                kind=SpanKind.CLIENT,
                duration_ms=0.0,
                status=SpanStatus.ERROR,
                attributes={"db.operation": verb},
                error_type=type(exc).__name__ if exc is not None else None,
            )
            tracker.record(
                category=ErrorCategory.DEPENDENCY,
                component=MonitoringComponent.DATABASE,
                exception_type=type(exc).__name__ if exc is not None else "DatabaseError",
                message=str(exc) if exc is not None else None,
                operation=verb,
            )
        except Exception:  # pragma: no cover - defensive
            return

    try:
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        event.listen(engine, "after_cursor_execute", after_cursor_execute)
        event.listen(engine, "handle_error", handle_error)
    except Exception:  # pragma: no cover - defensive
        logger.exception("database_instrumentation_failed")
        return False

    _instrumented.add(key)
    _listeners[key] = [
        ("before_cursor_execute", before_cursor_execute),
        ("after_cursor_execute", after_cursor_execute),
        ("handle_error", handle_error),
    ]
    return True


def uninstrument_engine(engine: Engine) -> None:
    """Remove the listeners :func:`instrument_engine` attached. Never raises."""
    key = id(engine)
    for name, listener in _listeners.pop(key, []):
        try:
            event.remove(engine, name, listener)
        except Exception:  # pragma: no cover - defensive
            continue
    _instrumented.discard(key)


def is_instrumented(engine: Engine) -> bool:
    """Whether ``engine`` currently carries the monitoring listeners."""
    return id(engine) in _instrumented


def record_pool_gauges(engine: Engine, metrics: MetricsRegistry) -> None:
    """Read the connection pool's current occupancy into two gauges.

    Called when a monitoring view is assembled rather than on a timer, and that
    is deliberate: a pool's occupancy is meaningful at the instant it is read,
    and a background thread sampling it every few seconds would be a thread the
    platform runs for the benefit of a page nobody may have open. Pull, not push
    — which is also the model every scraper this feature prepares for uses.

    Never raises: the pool's introspection API differs between pool
    implementations, and a deployment using a non-default pool should lose two
    gauges rather than a monitoring page.
    """
    try:
        pool = engine.pool
        checked_out = getattr(pool, "checkedout", None)
        checked_in = getattr(pool, "checkedin", None)
        if callable(checked_out):
            metrics.set_gauge(MetricName.DB_POOL_CHECKED_OUT, float(checked_out()))
        if callable(checked_in):
            metrics.set_gauge(MetricName.DB_POOL_CHECKED_IN, float(checked_in()))
    except Exception:  # pragma: no cover - defensive
        return
