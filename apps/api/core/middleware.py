"""Custom middleware.

``ObservabilityMiddleware`` is the platform's **one HTTP observation point**, and
it is where ``22-monitoring.md``'s three pillars meet a request:

* it assigns or adopts a correlation id and echoes it back as ``X-Request-ID``;
* it joins or begins a **trace**, opening the root ``server`` span every other
  span in the request nests inside, and returns the ``traceparent`` so a caller
  can follow the request into this service;
* it binds the shared **log context** — request id, method, path, route template,
  and (once authentication has resolved) the user and role — so every line any
  module writes during that request carries them without knowing they exist;
* it records the **metrics** the spec's Performance section asks for: request
  counts by method, route, and status class, and a latency histogram.

**Why one middleware rather than three.** Each of those needs the same three
things — the moment the request started, the route it matched, and the status it
ended with — and only one of them can hold the timer. Splitting them would mean
three passes over the same request, three `try/finally` blocks that must agree
about what "the request failed" means, and a latency figure measured at a
different depth of the stack from the one the log line reports.

**Everything here is inside a `try`, and none of it can fail a request.** A
tracer that raised, a registry that overflowed, or a context variable that could
not be bound must leave the response untouched — ``22-monitoring.md``: *"if
logging, metrics, tracing, or monitoring exporters become unavailable, the
platform must continue serving user requests"*. The failure modes are therefore
"an observation was not taken", never "a request was not served".

**The route *template* is the label, never the path.** ``/api/v1/cases/{case_id}``
is one series; ``/api/v1/cases/9f2c…`` would be one series per case, which is
both a cardinality problem and a slow leak of which matters are being worked on.
The template is only known *after* routing, which is why the metric labels are
resolved on the way out rather than on the way in.
"""

from __future__ import annotations

import threading
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from core.config import settings
from core.observability import (
    ErrorCategory,
    LogEvent,
    MetricName,
    MonitoringComponent,
    status_class,
)
from core.tracing import (
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    SpanKind,
    SpanStatus,
    parse_traceparent,
)
from services.error_tracker import get_error_tracker
from services.metrics_registry import get_metrics_registry
from services.tracer import get_tracer

logger = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

#: The route label used when a request matched nothing.
#:
#: A constant rather than the requested path, and it is the single most important
#: line in this module for cardinality: an unrouted request is usually a scanner,
#: and labelling by its path would let anybody on the internet create unbounded
#: series in this platform's metrics by requesting unbounded distinct URLs.
UNMATCHED_ROUTE = "unmatched"

#: Paths excluded from *metric* recording.
#:
#: The liveness and readiness probes are hit every few seconds by an
#: orchestrator, and counting them would put a synthetic majority into every
#: latency figure and every request total on the page. They are still logged and
#: still traced — a readiness probe that started failing is exactly what an
#: operator wants to see.
_UNMETERED_PATHS: frozenset[str] = frozenset({"/health", "/ready"})


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Correlate, trace, time, and count every HTTP request."""

    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Observe one request, and serve it exactly as if this were absent."""
        request_id = _request_id(request)
        request.state.request_id = request_id

        enabled = settings.MONITORING_ENABLED
        tracer = get_tracer() if enabled and settings.MONITORING_TRACING_ENABLED else None
        metrics = get_metrics_registry() if enabled and settings.MONITORING_METRICS_ENABLED else None

        trace_context = parse_traceparent(
            request.headers.get(TRACEPARENT_HEADER),
            trace_state=request.headers.get(TRACESTATE_HEADER),
        )

        _bind_request_context(request_id=request_id, request=request)
        self._adjust_in_flight(metrics, +1)
        start = time.perf_counter()

        if tracer is None:
            try:
                return await self._serve(
                    request, call_next, request_id=request_id, metrics=metrics, start=start
                )
            finally:
                self._adjust_in_flight(metrics, -1)
                _clear_request_context()

        try:
            with tracer.span(
                f"{request.method} {request.url.path}",
                component=MonitoringComponent.API,
                kind=SpanKind.SERVER,
                context=trace_context,
            ) as span:
                _bind_trace_fields(span.trace_id, span.span_id)
                span.set_attributes(
                    {"http.method": request.method, "http.target": request.url.path}
                )
                response = await self._serve(
                    request, call_next, request_id=request_id, metrics=metrics, start=start
                )
                span.set_attribute("http.route", _route_of(request))
                span.set_attribute("http.status_code", response.status_code)
                span.set_status(
                    SpanStatus.ERROR if response.status_code >= 500 else SpanStatus.OK
                )
                _apply_trace_headers(response, span.context.traceparent)
                return response
        finally:
            self._adjust_in_flight(metrics, -1)
            _clear_request_context()

    # ------------------------------------------------------------------------ #

    async def _serve(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
        *,
        request_id: str,
        metrics: object,
        start: float,
    ) -> Response:
        """Run the rest of the stack, then log and count the outcome.

        The exception branch re-raises after recording, because the registered
        handlers in :mod:`core.exceptions` are what turn an exception into a
        response — this middleware observes, and observation must not change the
        answer a caller gets.
        """
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = _elapsed_ms(start)
            route = _route_of(request)
            # Logged here rather than only in the handler, because a failure that
            # escapes `call_next` may never reach one: an exception raised inside
            # a streaming response body, for instance, has no handler left to run.
            logger.exception(LogEvent.REQUEST_FAILED, duration_ms=duration_ms, route=route)
            _record_request(metrics, request=request, route=route, status_code=500, duration_ms=duration_ms)
            _track_exception(exc, route=route)
            raise

        duration_ms = _elapsed_ms(start)
        route = _route_of(request)
        structlog.contextvars.bind_contextvars(route=route)

        response.headers[REQUEST_ID_HEADER] = request_id
        _record_request(
            metrics,
            request=request,
            route=route,
            status_code=response.status_code,
            duration_ms=duration_ms,
            response=response,
        )

        _log_completion(request, route=route, status_code=response.status_code, duration_ms=duration_ms)
        return response

    def _adjust_in_flight(self, metrics: object, delta: int) -> None:
        """Track concurrent requests as a gauge.

        A counter guarded by a lock rather than a metric read-modify-write,
        because the gauge has to be correct across threads and the registry
        deliberately offers no atomic increment for gauges — a gauge is a
        *reading*, and the thing being read is this counter.
        """
        if metrics is None:
            return
        try:
            with self._in_flight_lock:
                self._in_flight = max(0, self._in_flight + delta)
                current = self._in_flight
            metrics.set_gauge(MetricName.HTTP_REQUESTS_IN_FLIGHT, current)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            return


# --------------------------------------------------------------------------- #
# Helpers — each one swallows its own failures
# --------------------------------------------------------------------------- #


def _request_id(request: Request) -> str:
    """Adopt the caller's correlation id, or mint one.

    A supplied id is **bounded and sanitised** rather than trusted verbatim: it
    is echoed in a response header and written into every log line for the
    request, so unbounded caller-supplied text here is both a header-injection
    and a log-injection in one field.
    """
    supplied = request.headers.get(REQUEST_ID_HEADER)
    if supplied:
        cleaned = "".join(
            char for char in supplied.strip() if char.isalnum() or char in "-_."
        )[:64]
        if cleaned:
            return cleaned
    return str(uuid.uuid4())


def _bind_request_context(*, request_id: str, request: Request) -> None:
    """Start a fresh log context for this request."""
    try:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
    except Exception:  # pragma: no cover - defensive
        return


def _bind_trace_fields(trace_id: str, span_id: str) -> None:
    """Put the trace identifiers into the log context.

    Bound explicitly as well as being added by :func:`~core.logging.
    _add_trace_context`, because the two cover different ground: the processor
    covers code running inside the span, and this covers anything that logs with
    this request's context after the span has closed.
    """
    try:
        structlog.contextvars.bind_contextvars(trace_id=trace_id, span_id=span_id)
    except Exception:  # pragma: no cover - defensive
        return


def _clear_request_context() -> None:
    """Discard the log context so it cannot leak into the next request."""
    try:
        structlog.contextvars.clear_contextvars()
    except Exception:  # pragma: no cover - defensive
        return


def _apply_trace_headers(response: Response, traceparent: str) -> None:
    """Return the trace identity to the caller."""
    try:
        response.headers[TRACEPARENT_HEADER] = traceparent
    except Exception:  # pragma: no cover - defensive
        return


def _route_of(request: Request) -> str:
    """Return the matched route *template*, or :data:`UNMATCHED_ROUTE`.

    Only knowable after routing — Starlette resolves the match while dispatching —
    which is why the metric labels are applied on the way out rather than on the
    way in.

    **The template is rebuilt from the request path and its parameters rather than
    read off the route object**, and that is a correction rather than a
    preference: a router registered under a prefix reports its *own* path
    (``/jobs``), not the path the client asked for (``/api/v1/monitoring/jobs``),
    so labelling from it would collapse two different endpoints that happen to end
    in the same segment into one series. Substituting the parameter values back out
    of the real path gives the full template and cannot disagree with the URL that
    was actually served.

    Substitution is **per segment** rather than by string replacement, because a
    parameter's value can legitimately appear elsewhere in the path — replacing
    every occurrence of a case number would rewrite a segment that is not a
    parameter at all.
    """
    try:
        if request.scope.get("route") is None:
            return UNMATCHED_ROUTE

        path = request.url.path
        params = request.scope.get("path_params") or {}
        if not params:
            return path

        by_value = {str(value): name for name, value in params.items()}
        segments = [
            "{" + by_value[segment] + "}" if segment in by_value else segment
            for segment in path.split("/")
        ]
        return "/".join(segments)
    except Exception:  # pragma: no cover - defensive
        return UNMATCHED_ROUTE


def _elapsed_ms(start: float) -> float:
    """Milliseconds since ``start``, from the monotonic clock."""
    return round((time.perf_counter() - start) * 1000, 2)


def _record_request(
    metrics: object,
    *,
    request: Request,
    route: str,
    status_code: int,
    duration_ms: float,
    response: Response | None = None,
) -> None:
    """Count one request and time it."""
    if metrics is None or request.url.path in _UNMETERED_PATHS:
        return
    try:
        metrics.increment(  # type: ignore[attr-defined]
            MetricName.HTTP_REQUESTS_TOTAL,
            labels={
                "method": request.method,
                "route": route,
                "status_class": status_class(status_code),
            },
        )
        metrics.observe(  # type: ignore[attr-defined]
            MetricName.HTTP_REQUEST_DURATION_MS,
            duration_ms,
            labels={"method": request.method, "route": route},
        )
        if response is not None:
            length = response.headers.get("content-length")
            if length is not None and length.isdigit():
                metrics.observe(  # type: ignore[attr-defined]
                    MetricName.HTTP_RESPONSE_SIZE_BYTES, float(length), labels={"route": route}
                )
    except Exception:  # pragma: no cover - defensive
        return


def _track_exception(exc: BaseException, *, route: str) -> None:
    """Record an exception that escaped the endpoint.

    Categorised as :attr:`~core.observability.ErrorCategory.UNHANDLED` because by
    definition nothing handled it — :mod:`core.exceptions` records the *handled*
    ones, and keeping the two categories apart is what lets an operator tell a
    platform answering 503s on purpose from one that is falling over.
    """
    try:
        get_error_tracker().record(
            category=ErrorCategory.UNHANDLED,
            component=MonitoringComponent.API,
            exception_type=type(exc).__name__,
            message=str(exc),
            location=_exception_location(exc),
            operation=route,
            status_code=500,
        )
    except Exception:  # pragma: no cover - defensive
        return


def _exception_location(exc: BaseException) -> str | None:
    """Return ``file.py:line`` for where an exception was raised, if known.

    The **deepest** frame, which is where the failure actually happened rather
    than where it was noticed. Only the file's name is kept, never its absolute
    path: a path names the deployment's filesystem layout, and the name is what
    identifies the module.
    """
    try:
        traceback = exc.__traceback__
        if traceback is None:
            return None
        while traceback.tb_next is not None:
            traceback = traceback.tb_next
        frame = traceback.tb_frame
        filename = frame.f_code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        return f"{filename}:{traceback.tb_lineno}"
    except Exception:  # pragma: no cover - defensive
        return None


def _log_completion(
    request: Request, *, route: str, status_code: int, duration_ms: float
) -> None:
    """Write the one structured line every completed request produces.

    Carries exactly the fields ``22-monitoring.md``'s Structured Logging section
    lists — timestamp, request identifier, user identifier, authenticated role,
    operation, module, duration, status — of which the first two and the last two
    come from the context bound above, and the middle ones are here.

    A request slower than ``MONITORING_SLOW_REQUEST_MS`` is logged at **warning**
    with the same fields, which is what makes a latency problem findable in a log
    aggregator rather than only visible on a metrics page nobody had open.
    """
    try:
        user = getattr(request.state, "user", None)
        fields = {
            "status_code": status_code,
            "duration_ms": duration_ms,
            "route": route,
            "component": MonitoringComponent.API.value,
        }
        if user is not None:
            fields["actor_id"] = str(getattr(user, "id", "")) or None
            role = getattr(user, "role", None)
            fields["role"] = getattr(role, "value", None)

        if duration_ms >= settings.MONITORING_SLOW_REQUEST_MS:
            logger.warning(LogEvent.SLOW_REQUEST, **fields)
        else:
            logger.info(LogEvent.REQUEST_COMPLETED, **fields)
    except Exception:  # pragma: no cover - defensive
        return


#: The name this middleware was registered under before Monitoring & Observability
#: widened it from logging to logging, tracing, and metrics.
#:
#: Kept as an alias rather than removed: the class does everything it used to do,
#: under a name that now describes it, and an alias costs one line where a
#: rename that misses a call site costs a startup failure.
RequestLoggingMiddleware = ObservabilityMiddleware
