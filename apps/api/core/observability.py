"""The observability vocabulary.

Everything monitoring names is named exactly once, here: the components a
measurement can be attributed to, the metrics the platform emits, the categories
an error falls into, the security events worth watching, and the alert
conditions an operator would want to be woken for. Nothing else in the codebase
invents one of those strings — the instrumentation imports a member, so a typo
is a static error rather than a metric nobody ever sees again.

This is :mod:`core.notifications` and :mod:`core.events` for monitoring, and the
same three properties follow from it:

* **A metric is declared before it is recorded.** :data:`METRICS` carries a name,
  a type, a unit, and a sentence per series, so an exporter can render the
  platform's metrics without any recording site telling it what they mean. That
  is what makes ``22-monitoring.md``'s *"support future metrics without
  redesign"* one dictionary entry rather than a change to an exporter.
* **A metric is provider-independent.** Nothing here mentions Prometheus,
  OpenTelemetry, Grafana, or Sentry. The names follow the widely-used
  ``subsystem_thing_unit`` convention because that convention costs nothing and
  makes a future exporter's job mechanical, but the declaration is the
  platform's own and every backend is a renderer over it.
* **Nothing here can hold a secret.** :func:`redact_mapping` is the one function
  the logging pipeline and every recorder run their structured fields through,
  and it is deliberately keyed on the *field name* rather than on the value: a
  value-based scrubber has to recognise a token, while a name-based one refuses
  ``password`` before ever seeing what was in it. That is
  ``22-monitoring.md``'s Logging Policy made a property of the pipeline rather
  than of the care taken at each call site.

**There is no `MonitoringService` decision in this module and no I/O.** It is
pure data and pure functions, so it can be imported by :mod:`core.exceptions`,
:mod:`core.middleware`, and every recorder without any risk of an import cycle —
which matters more here than anywhere else on the platform, because monitoring
attaches to modules that everything already imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "ALERT_RULES",
    "LATENCY_BUCKETS_MS",
    "METRICS",
    "REDACTED",
    "SECURITY_SEVERITIES",
    "SENSITIVE_FIELD_FRAGMENTS",
    "SIZE_BUCKETS_BYTES",
    "AlertRule",
    "AlertSeverity",
    "ErrorCategory",
    "HealthState",
    "LogEvent",
    "MetricDefinition",
    "MetricName",
    "MetricType",
    "MetricUnit",
    "MonitoringComponent",
    "SecurityEventType",
    "SecuritySeverity",
    "buckets_for",
    "error_fingerprint",
    "is_sensitive_field",
    "metric_definition",
    "redact_mapping",
    "redact_text",
    "security_severity",
    "status_class",
    "truncate",
    "worse_health",
]


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #


class MonitoringComponent(StrEnum):
    """The part of the platform an observation is attributed to.

    Deliberately **one flat vocabulary rather than a free-form string**, and it
    is the label every metric, span, tracked error, and log line carries. Two
    reasons, and the second is the load-bearing one:

    * a free-form component would make ``documents``, ``document``, and
      ``docs`` three different series on the same chart, which is how a metrics
      backend quietly stops being comparable across releases;
    * a **bounded** set is what keeps label cardinality bounded. Every other
      label the platform emits is either bounded by construction (an HTTP method,
      a status class, a metric name) or is a route *template*; this one is an
      enum, so no recording site can turn a case number into a new time series.

    The members mirror the module boundaries `architecture.md` lists, plus the
    four infrastructure ones (``api``, ``database``, ``cache``, ``storage``,
    ``vector``) that belong to no feature and are exactly where an operator looks
    first.
    """

    # --- Infrastructure ------------------------------------------------------ #
    API = "api"
    DATABASE = "database"
    CACHE = "cache"
    STORAGE = "storage"
    VECTOR = "vector"
    MONITORING = "monitoring"

    # --- Platform features --------------------------------------------------- #
    AUTH = "auth"
    AUTHORIZATION = "authorization"
    USERS = "users"
    CASES = "cases"
    DOCUMENTS = "documents"
    OCR = "ocr"
    INDEXING = "indexing"
    SEARCH = "search"
    RAG = "rag"
    ASSISTANT = "assistant"
    REPORTS = "reports"
    TIMELINE = "timeline"
    REALTIME = "realtime"
    NOTIFICATIONS = "notifications"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    DASHBOARD = "dashboard"
    SETTINGS = "settings"
    LOCALIZATION = "localization"


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


class HealthState(StrEnum):
    """How well one thing — a dependency, a subsystem, the platform — is doing.

    Three states rather than two, and the middle one is the point.
    :class:`~schemas.health.HealthStatus` has ``ok`` and ``degraded`` because a
    readiness probe is a **binary** decision an orchestrator acts on: route
    traffic here, or do not. A monitoring view is read by a person, and the
    difference between *"the vector database is unreachable, so semantic search
    is refusing"* and *"WhatsApp is switched off in this deployment"* is the
    difference between a page at three in the morning and a shrug.

    Ordered by :func:`worse_health`, never by comparison operators — a
    :class:`~enum.StrEnum` compares alphabetically, which would make ``degraded``
    worse than ``unhealthy``.
    """

    #: Working, and known to be working.
    HEALTHY = "healthy"
    #: Working, with something worth an operator's attention — an optional
    #: dependency down, an error rate above its threshold, a queue backing up.
    DEGRADED = "degraded"
    #: Not working. A required dependency is unreachable and requests that need
    #: it are failing.
    UNHEALTHY = "unhealthy"
    #: Deliberately switched off for this deployment, or never configured. **Not
    #: a fault**, and reported separately so that a platform running without
    #: WhatsApp does not look broken to somebody who did not configure it.
    DISABLED = "disabled"
    #: Could not be determined. The probe itself failed in a way that says
    #: nothing about the thing being probed — which is honest, and is what
    #: monitoring degrading gracefully looks like from the outside.
    UNKNOWN = "unknown"


#: Severity ordering for :func:`worse_health`. Higher is worse.
#:
#: ``DISABLED`` and ``UNKNOWN`` sit *below* ``DEGRADED`` deliberately: neither is
#: evidence of a fault, and letting either dominate an aggregate would make the
#: overall state of a correctly-configured deployment depend on how many optional
#: features it left off.
_HEALTH_RANK: Final[Mapping[HealthState, int]] = {
    HealthState.HEALTHY: 0,
    HealthState.DISABLED: 1,
    HealthState.UNKNOWN: 2,
    HealthState.DEGRADED: 3,
    HealthState.UNHEALTHY: 4,
}


def worse_health(*states: HealthState) -> HealthState:
    """Return the worst of ``states``, or :attr:`HealthState.HEALTHY` if empty.

    Aggregation is *max by rank* rather than by any arithmetic over the members:
    a subsystem is exactly as healthy as its least healthy part, and averaging
    would let nine healthy dependencies hide the one that is down.
    """
    if not states:
        return HealthState.HEALTHY
    return max(states, key=lambda state: _HEALTH_RANK[state])


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


class MetricType(StrEnum):
    """The shape of a metric, which decides how it may be read.

    Three kinds, and the distinction is not cosmetic: a counter may only ever be
    added to (so a fall in one means a restart, never a decrease), a gauge is a
    reading at an instant (so summing two of them across instances is
    meaningless), and a histogram accumulates a distribution (so it is the only
    one an average or a quantile may be taken from). An exporter renders each
    differently, and a chart that treats one as another is wrong in a way nobody
    notices.
    """

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class MetricUnit(StrEnum):
    """What a metric's numbers mean.

    Carried on the declaration rather than baked into the name, so a renderer can
    convert (milliseconds to seconds, for the exposition formats that insist on
    base units) without parsing an identifier.
    """

    COUNT = "count"
    MILLISECONDS = "milliseconds"
    SECONDS = "seconds"
    BYTES = "bytes"
    TOKENS = "tokens"


class MetricName(StrEnum):
    """Every metric the monitoring layer records, named once.

    **What is *not* here is as deliberate as what is.** Eleven features already
    ship a metrics recorder of their own — search, RAG, the assistant, reports,
    real-time, notifications, email, WhatsApp, the dashboard, settings, and
    localization — and this feature does **not** re-record what they count. Doing
    so would be the *"avoid duplicate metric collection"* the spec forbids, and it
    would produce two numbers for one question that drift the first time one call
    site moves. :class:`~services.monitoring.MonitoringService` *reads* those
    recorders; the names below are only for observations nobody was taking.

    So this list is the cross-cutting layer: HTTP, the database, background jobs
    seen as jobs rather than as features, errors, security, and the process
    itself.
    """

    # --- HTTP ---------------------------------------------------------------- #
    HTTP_REQUESTS_TOTAL = "http_requests_total"
    HTTP_REQUEST_DURATION_MS = "http_request_duration_milliseconds"
    HTTP_REQUESTS_IN_FLIGHT = "http_requests_in_flight"
    HTTP_RESPONSE_SIZE_BYTES = "http_response_size_bytes"

    # --- Database ------------------------------------------------------------ #
    DB_QUERIES_TOTAL = "db_queries_total"
    DB_QUERY_DURATION_MS = "db_query_duration_milliseconds"
    DB_QUERY_ERRORS_TOTAL = "db_query_errors_total"
    DB_POOL_CHECKED_OUT = "db_pool_connections_checked_out"
    DB_POOL_CHECKED_IN = "db_pool_connections_checked_in"

    # --- Errors -------------------------------------------------------------- #
    ERRORS_TOTAL = "errors_total"
    UNHANDLED_EXCEPTIONS_TOTAL = "unhandled_exceptions_total"

    # --- Security ------------------------------------------------------------ #
    SECURITY_EVENTS_TOTAL = "security_events_total"
    AUTH_LOGIN_ATTEMPTS_TOTAL = "auth_login_attempts_total"
    AUTH_LOGIN_FAILURES_TOTAL = "auth_login_failures_total"
    AUTHORIZATION_DENIALS_TOTAL = "authorization_denials_total"

    # --- Background jobs ------------------------------------------------------ #
    JOB_QUEUE_DEPTH = "job_queue_depth"
    JOBS_STARTED_TOTAL = "jobs_started_total"
    JOBS_COMPLETED_TOTAL = "jobs_completed_total"
    JOBS_FAILED_TOTAL = "jobs_failed_total"
    JOB_DURATION_MS = "job_duration_milliseconds"

    # --- External services ---------------------------------------------------- #
    EXTERNAL_CALLS_TOTAL = "external_calls_total"
    EXTERNAL_CALL_DURATION_MS = "external_call_duration_milliseconds"
    EXTERNAL_CALL_FAILURES_TOTAL = "external_call_failures_total"

    # --- Tracing -------------------------------------------------------------- #
    SPANS_STARTED_TOTAL = "spans_started_total"
    SPAN_DURATION_MS = "span_duration_milliseconds"

    # --- Feature metrics, bridged ----------------------------------------------- #
    #: **One metric name for every figure the eleven feature recorders hold**, as
    #: a gauge labelled by feature and figure.
    #:
    #: This is how ``22-monitoring.md``'s *"support future metrics without
    #: redesign"* is honoured without either duplicating those recorders or
    #: declaring a hundred names here. The feature recorders stay the source of
    #: truth — they are read, never written — and
    #: :class:`~services.monitoring.MonitoringService` copies their numeric fields
    #: into this one series immediately before a snapshot is taken, so a scraper
    #: sees ``feature_metric{feature="rag",metric="total_requests"}`` without RAG
    #: knowing an exporter exists.
    #:
    #: A **gauge** rather than a counter even for the figures that only ever
    #: increase, and that is deliberate: they are *read* from another recorder
    #: rather than accumulated here, so this registry cannot promise monotonicity
    #: across a reset of the recorder it copied from — and a counter that can fall
    #: is worse than a gauge that was never claimed to be one.
    FEATURE_METRIC = "feature_metric"

    # --- Process --------------------------------------------------------------- #
    PROCESS_UPTIME_SECONDS = "process_uptime_seconds"
    PROCESS_THREADS = "process_threads"
    #: Series the registry refused because the cardinality ceiling was reached.
    #: A metric **about** the metrics, and the one number that says whether any
    #: other number on the page is complete.
    METRIC_SERIES_DROPPED_TOTAL = "metric_series_dropped_total"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """What one metric is, independently of any backend that renders it.

    ``labels`` names the dimensions a series may carry, and it is a **closed
    list** rather than documentation: :class:`~services.metrics_registry.
    InMemoryMetricsRegistry` drops any label not declared here, which is the one
    mechanism that keeps a recording site from turning a case identifier into a
    permanent time series.
    """

    name: MetricName
    type: MetricType
    unit: MetricUnit
    description: str
    component: MonitoringComponent
    labels: tuple[str, ...] = ()


#: Buckets for a latency histogram, in milliseconds.
#:
#: Chosen around what this platform actually does rather than from a default
#: ladder: a cached read is under 25 ms, an ordinary authorized query is under
#: 250 ms, a document upload is a second or two, and anything past ten seconds is
#: an AI call or a fault. A bucket boundary in the wrong place makes a quantile a
#: guess, and these are where the interesting decisions sit.
LATENCY_BUCKETS_MS: Final[tuple[float, ...]] = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1_000.0,
    2_500.0,
    5_000.0,
    10_000.0,
    30_000.0,
    60_000.0,
)


#: Buckets for a size histogram, in bytes: 1 KiB to 32 MiB.
#:
#: The top boundary is deliberately above ``MAX_DOCUMENT_SIZE_MB``'s default, so
#: the largest upload the platform accepts still lands *inside* a bucket rather
#: than in the overflow where its size becomes unknowable.
SIZE_BUCKETS_BYTES: Final[tuple[float, ...]] = (
    1_024.0,
    8_192.0,
    65_536.0,
    262_144.0,
    1_048_576.0,
    4_194_304.0,
    16_777_216.0,
    33_554_432.0,
)


def buckets_for(definition: MetricDefinition) -> tuple[float, ...]:
    """Return the bucket boundaries a histogram of this unit should use.

    Chosen from the **unit** rather than declared per metric, deliberately: two
    latency histograms with different boundaries cannot be compared or summed,
    and the moment boundaries are a per-metric decision somebody makes one, which
    is how a dashboard ends up with two charts that disagree about what "p95"
    means.
    """
    if definition.unit is MetricUnit.BYTES:
        return SIZE_BUCKETS_BYTES
    if definition.unit is MetricUnit.SECONDS:
        return tuple(bound / 1_000.0 for bound in LATENCY_BUCKETS_MS)
    return LATENCY_BUCKETS_MS


def _definitions() -> Mapping[MetricName, MetricDefinition]:
    """Build the metric catalog once, at import."""
    entries = (
        MetricDefinition(
            MetricName.HTTP_REQUESTS_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "HTTP requests completed, by method, route template, and status class.",
            MonitoringComponent.API,
            ("method", "route", "status_class"),
        ),
        MetricDefinition(
            MetricName.HTTP_REQUEST_DURATION_MS,
            MetricType.HISTOGRAM,
            MetricUnit.MILLISECONDS,
            "Wall-clock time to produce an HTTP response.",
            MonitoringComponent.API,
            ("method", "route"),
        ),
        MetricDefinition(
            MetricName.HTTP_REQUESTS_IN_FLIGHT,
            MetricType.GAUGE,
            MetricUnit.COUNT,
            "HTTP requests currently being handled by this process.",
            MonitoringComponent.API,
        ),
        MetricDefinition(
            MetricName.HTTP_RESPONSE_SIZE_BYTES,
            MetricType.HISTOGRAM,
            MetricUnit.BYTES,
            "Declared size of an HTTP response body, when the response declares one.",
            MonitoringComponent.API,
            ("route",),
        ),
        MetricDefinition(
            MetricName.DB_QUERIES_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Database statements executed, by verb. Never by statement text.",
            MonitoringComponent.DATABASE,
            ("operation",),
        ),
        MetricDefinition(
            MetricName.DB_QUERY_DURATION_MS,
            MetricType.HISTOGRAM,
            MetricUnit.MILLISECONDS,
            "Time a database statement spent executing, by verb.",
            MonitoringComponent.DATABASE,
            ("operation",),
        ),
        MetricDefinition(
            MetricName.DB_QUERY_ERRORS_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Database statements that raised, by verb.",
            MonitoringComponent.DATABASE,
            ("operation",),
        ),
        MetricDefinition(
            MetricName.DB_POOL_CHECKED_OUT,
            MetricType.GAUGE,
            MetricUnit.COUNT,
            "Pooled database connections currently in use.",
            MonitoringComponent.DATABASE,
        ),
        MetricDefinition(
            MetricName.DB_POOL_CHECKED_IN,
            MetricType.GAUGE,
            MetricUnit.COUNT,
            "Pooled database connections currently idle.",
            MonitoringComponent.DATABASE,
        ),
        MetricDefinition(
            MetricName.ERRORS_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Failures recorded by the error tracker, by category and component.",
            MonitoringComponent.MONITORING,
            ("category", "component"),
        ),
        MetricDefinition(
            MetricName.UNHANDLED_EXCEPTIONS_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Exceptions that reached the application's last-resort handler.",
            MonitoringComponent.API,
        ),
        MetricDefinition(
            MetricName.SECURITY_EVENTS_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Security-relevant events, by type and severity.",
            MonitoringComponent.AUTH,
            ("event", "severity"),
        ),
        MetricDefinition(
            MetricName.AUTH_LOGIN_ATTEMPTS_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Sign-in attempts reaching the platform.",
            MonitoringComponent.AUTH,
        ),
        MetricDefinition(
            MetricName.AUTH_LOGIN_FAILURES_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Sign-in attempts refused, by reason.",
            MonitoringComponent.AUTH,
            ("reason",),
        ),
        MetricDefinition(
            MetricName.AUTHORIZATION_DENIALS_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Authorized-but-refused requests, by the role that was refused.",
            MonitoringComponent.AUTHORIZATION,
            ("role",),
        ),
        MetricDefinition(
            MetricName.JOB_QUEUE_DEPTH,
            MetricType.GAUGE,
            MetricUnit.COUNT,
            "Jobs awaiting or undergoing processing, by queue and state.",
            MonitoringComponent.MONITORING,
            ("queue", "state"),
        ),
        MetricDefinition(
            MetricName.JOBS_STARTED_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Background jobs picked up by a worker, by queue.",
            MonitoringComponent.MONITORING,
            ("queue",),
        ),
        MetricDefinition(
            MetricName.JOBS_COMPLETED_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Background jobs that finished successfully, by queue.",
            MonitoringComponent.MONITORING,
            ("queue",),
        ),
        MetricDefinition(
            MetricName.JOBS_FAILED_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Background jobs that failed, by queue.",
            MonitoringComponent.MONITORING,
            ("queue",),
        ),
        MetricDefinition(
            MetricName.JOB_DURATION_MS,
            MetricType.HISTOGRAM,
            MetricUnit.MILLISECONDS,
            "Wall-clock time a background job took, by queue.",
            MonitoringComponent.MONITORING,
            ("queue",),
        ),
        MetricDefinition(
            MetricName.EXTERNAL_CALLS_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Calls made to a service outside this platform, by service.",
            MonitoringComponent.MONITORING,
            ("service",),
        ),
        MetricDefinition(
            MetricName.EXTERNAL_CALL_DURATION_MS,
            MetricType.HISTOGRAM,
            MetricUnit.MILLISECONDS,
            "Time an external service took to answer, by service.",
            MonitoringComponent.MONITORING,
            ("service",),
        ),
        MetricDefinition(
            MetricName.EXTERNAL_CALL_FAILURES_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Calls to an external service that failed, by service.",
            MonitoringComponent.MONITORING,
            ("service",),
        ),
        MetricDefinition(
            MetricName.SPANS_STARTED_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Trace spans opened, by component.",
            MonitoringComponent.MONITORING,
            ("component",),
        ),
        MetricDefinition(
            MetricName.SPAN_DURATION_MS,
            MetricType.HISTOGRAM,
            MetricUnit.MILLISECONDS,
            "Duration of a completed trace span, by component.",
            MonitoringComponent.MONITORING,
            ("component",),
        ),
        MetricDefinition(
            MetricName.FEATURE_METRIC,
            MetricType.GAUGE,
            MetricUnit.COUNT,
            "A figure held by one feature's own metrics recorder, bridged for export.",
            MonitoringComponent.MONITORING,
            ("feature", "metric"),
        ),
        MetricDefinition(
            MetricName.PROCESS_UPTIME_SECONDS,
            MetricType.GAUGE,
            MetricUnit.SECONDS,
            "Seconds since this API process finished starting.",
            MonitoringComponent.MONITORING,
        ),
        MetricDefinition(
            MetricName.PROCESS_THREADS,
            MetricType.GAUGE,
            MetricUnit.COUNT,
            "Threads alive in this API process, workers included.",
            MonitoringComponent.MONITORING,
        ),
        MetricDefinition(
            MetricName.METRIC_SERIES_DROPPED_TOTAL,
            MetricType.COUNTER,
            MetricUnit.COUNT,
            "Series the registry refused after reaching its cardinality ceiling.",
            MonitoringComponent.MONITORING,
        ),
    )
    return {entry.name: entry for entry in entries}


#: Every metric the monitoring layer may record, keyed by name.
METRICS: Final[Mapping[MetricName, MetricDefinition]] = _definitions()


def metric_definition(name: MetricName) -> MetricDefinition:
    """Return the declaration for ``name``.

    Raises:
        KeyError: the metric has no declaration. Always a programming fault — a
            recording site names a :class:`MetricName` member, and every member
            is declared — so it is left to raise rather than being softened into
            a default: a metric with no unit and no description is worse than an
            obvious crash in a test.
    """
    return METRICS[name]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ErrorCategory(StrEnum):
    """What kind of failure was recorded.

    The four ``22-monitoring.md``'s Error Tracking section names, plus two the
    platform genuinely distinguishes. They are categories rather than severities:
    an unhandled exception in a request and a failed WhatsApp send are both
    ``error`` in a log, and telling them apart is the whole reason an operator
    can act on one of them.
    """

    #: Reached the last-resort handler. Always a defect.
    UNHANDLED = "unhandled_exception"
    #: A handled, expected failure the platform answered with — a 5xx from an
    #: :class:`~core.exceptions.AppException`. Counted because a spike in these is
    #: as actionable as a spike in the ones nobody caught.
    HANDLED = "handled_error"
    #: A background job that failed. Has no request, no caller, and nobody
    #: watching, which is exactly why it needs to be recorded somewhere visible.
    BACKGROUND_JOB = "background_job"
    #: A call to something outside the platform — a language model, a relay, the
    #: Cloud API, object storage.
    EXTERNAL_SERVICE = "external_service"
    #: A failure on the WebSocket channel: a frame that could not be delivered, a
    #: connection dropped mid-write.
    WEBSOCKET = "websocket"
    #: A backing service the platform requires was unreachable.
    DEPENDENCY = "dependency"


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


class SecurityEventType(StrEnum):
    """A security-relevant thing that happened, named once.

    Exactly the list ``22-monitoring.md``'s Security Monitoring section gives —
    failed logins, repeated authorization failures, suspicious authentication
    activity, excessive API requests, invalid tokens — plus the two successful
    counterparts, because a failure count with no attempt count beside it cannot
    be read: fifty failures out of fifty is an attack and fifty out of fifty
    thousand is a Monday.
    """

    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGIN_LOCKED_OUT = "login_locked_out"
    LOGOUT = "logout"
    TOKEN_INVALID = "token_invalid"
    TOKEN_EXPIRED = "token_expired"
    PERMISSION_DENIED = "permission_denied"
    RESOURCE_ACCESS_DENIED = "resource_access_denied"
    RATE_LIMITED = "rate_limited"
    ACCOUNT_DISABLED = "account_disabled"
    PASSWORD_CHANGED = "password_changed"


class SecuritySeverity(StrEnum):
    """How much attention one security event deserves on its own.

    ``INFO`` covers the successful counterparts above: they are recorded so the
    failures have a denominator, and an operator's feed should not treat a
    successful sign-in as news.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


#: Default severity per event type, so no recording site chooses one.
#:
#: A single expired token is ``INFO``, and that is deliberate: an access token
#: lives fifteen minutes, so every active session produces one roughly four times
#: an hour, and treating each as a warning would bury the events that matter under
#: the platform working exactly as designed. What is *not* routine is the
#: **rate** — which is what :class:`~services.security_monitor.SecurityMonitor`
#: counts and :data:`ALERT_RULES` watches.
SECURITY_SEVERITIES: Final[Mapping[SecurityEventType, SecuritySeverity]] = {
    SecurityEventType.LOGIN_SUCCEEDED: SecuritySeverity.INFO,
    SecurityEventType.LOGIN_FAILED: SecuritySeverity.WARNING,
    SecurityEventType.LOGIN_LOCKED_OUT: SecuritySeverity.CRITICAL,
    SecurityEventType.LOGOUT: SecuritySeverity.INFO,
    SecurityEventType.TOKEN_INVALID: SecuritySeverity.WARNING,
    SecurityEventType.TOKEN_EXPIRED: SecuritySeverity.INFO,
    SecurityEventType.PERMISSION_DENIED: SecuritySeverity.WARNING,
    SecurityEventType.RESOURCE_ACCESS_DENIED: SecuritySeverity.WARNING,
    SecurityEventType.RATE_LIMITED: SecuritySeverity.WARNING,
    SecurityEventType.ACCOUNT_DISABLED: SecuritySeverity.WARNING,
    SecurityEventType.PASSWORD_CHANGED: SecuritySeverity.INFO,
}


def security_severity(event: SecurityEventType) -> SecuritySeverity:
    """Return the default severity for ``event``.

    Unknown members fall back to ``WARNING`` rather than raising: a monitoring
    lookup must never be the thing that fails a request, and an unclassified
    security event should be visible rather than silently ``INFO``.
    """
    return SECURITY_SEVERITIES.get(event, SecuritySeverity.WARNING)


# --------------------------------------------------------------------------- #
# Log events
# --------------------------------------------------------------------------- #


class LogEvent(StrEnum):
    """Structured log event names this feature emits.

    Only the ones **monitoring itself** writes. Every module on the platform
    already logs its own events (``case_created``, ``ocr_completed``,
    ``email_delivered``, …) with names it owns, and ``22-monitoring.md``'s
    Logging section is a list of *what should be logged*, not a demand that the
    lines move here. They already exist; what this feature adds is the shared
    **context** every one of them now carries — request id, trace id, user, role,
    component, duration, status — through :mod:`core.middleware` and structlog's
    context variables.
    """

    REQUEST_STARTED = "request_started"
    REQUEST_COMPLETED = "request_completed"
    REQUEST_FAILED = "request_failed"
    APPLICATION_ERROR = "application_error"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    SECURITY_EVENT = "security_event"
    SLOW_REQUEST = "slow_request"
    SLOW_QUERY = "slow_query"
    MONITORING_STARTED = "monitoring_started"
    MONITORING_DEGRADED = "monitoring_degraded"
    CONFIGURATION_LOADED = "configuration_loaded"


# --------------------------------------------------------------------------- #
# Redaction — the Logging Policy, enforced
# --------------------------------------------------------------------------- #

#: What a redacted value is replaced with. A constant rather than an empty string
#: so a reader can tell "this was removed" from "this was absent" — the two mean
#: very different things when the field is ``authorization``.
REDACTED: Final[str] = "[redacted]"

#: A field whose name **contains** any of these is never logged.
#:
#: Substring matching rather than exact names, deliberately: the platform has
#: ``password``, ``hashed_password``, ``current_password``, ``new_password``, and
#: ``temporary_password``, and an exact-match list is a list somebody forgets to
#: extend. The cost is the occasional over-redaction (a field called
#: ``password_changed_at`` loses a timestamp), and that trade is the right way
#: round: ``22-monitoring.md``'s Logging Policy is absolute, and a missing
#: timestamp is an inconvenience where a logged credential is an incident.
SENSITIVE_FIELD_FRAGMENTS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "secret",
        "token",
        "credential",
        "authorization",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
        "cookie",
        "session_key",
        "signature",
        "otp",
    }
)

#: Fields that carry *content* rather than credentials, and are equally forbidden.
#:
#: ``22-monitoring.md``'s Logging Policy names four: uploaded document contents,
#: AI prompts containing confidential legal information, generated legal reports,
#: and — by extension of `code-standards.md` — the questions people ask about
#: their clients' matters. Each of these is the platform's *subject matter*, and
#: the platform already has correlation handles that are not: a document id, a
#: salted query fingerprint, a report id.
CONTENT_FIELD_FRAGMENTS: Final[frozenset[str]] = frozenset(
    {
        "prompt",
        "completion",
        "answer_text",
        "question_text",
        "message_body",
        "email_body",
        "document_text",
        "extracted_text",
        "page_text",
        "passage",
        "chunk_text",
        "report_body",
        "sections_text",
        "raw_response",
    }
)


def is_sensitive_field(name: str) -> bool:
    """Whether a structured-log field of this name must never carry its value.

    Case-insensitive and substring-based; see :data:`SENSITIVE_FIELD_FRAGMENTS`
    for why.
    """
    lowered = name.lower()
    return any(fragment in lowered for fragment in SENSITIVE_FIELD_FRAGMENTS) or any(
        fragment in lowered for fragment in CONTENT_FIELD_FRAGMENTS
    )


def redact_mapping(fields: Mapping[str, Any], *, max_depth: int = 4) -> dict[str, Any]:
    """Return ``fields`` with every sensitive value replaced by :data:`REDACTED`.

    Recurses into nested mappings and sequences, because a credential is at least
    as likely to arrive inside a ``payload`` dictionary as at the top level, and
    a scrubber that only looks one level down provides a guarantee it does not
    keep.

    ``max_depth`` bounds that recursion. Structured log fields are small by
    convention, and a bound is what stops a self-referential structure — or one
    somebody built out of an ORM object — from making the *logging* call the
    slowest thing in a request. Past the bound the value is summarised by type
    rather than rendered, which is honest and cheap.
    """
    return {key: _redact_value(key, value, max_depth) for key, value in fields.items()}


def _redact_value(key: str, value: Any, depth: int) -> Any:
    """Redact one field, recursing while ``depth`` allows."""
    if is_sensitive_field(key):
        return REDACTED
    if depth <= 0:
        return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        return {
            inner_key: _redact_value(str(inner_key), inner_value, depth - 1)
            for inner_key, inner_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        # The key applies to every element, so an element of a list called
        # `tokens` is redacted by the check above before ever reaching here.
        return [_redact_value(key, item, depth - 1) for item in value]
    return value


def redact_text(text: str, *, limit: int = 200) -> str:
    """Bound a free-text fragment so it can be recorded safely.

    Used for an exception's own message, which is the one piece of free text
    monitoring genuinely needs — *"errors should include sufficient diagnostic
    information"* — and the one place text from anywhere in the platform can
    arrive. Two things happen to it: it is truncated, so a driver that quoted a
    900-character statement contributes a line rather than a page, and it is
    **stripped of newlines**, so nothing written into a log-aggregation pipeline
    can forge a second log entry.

    This is deliberately *not* a content scrubber: it cannot know that a message
    quotes a case title. What keeps subject matter out of these strings is the
    platform's existing discipline — every provider boundary already translates
    a library failure into a code before it escapes (see
    :mod:`services.email_provider`, :mod:`services.whatsapp_provider`,
    :mod:`services.llm`) — and this is the bound on what survives that.
    """
    collapsed = " ".join(text.split())
    return truncate(collapsed, limit)


def truncate(text: str, limit: int) -> str:
    """Return ``text`` cut to ``limit`` characters, with an ellipsis if it was."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #


def error_fingerprint(
    *,
    category: ErrorCategory,
    component: MonitoringComponent,
    exception_type: str,
    location: str | None = None,
) -> str:
    """Return the identity of a *class* of failure, for grouping.

    Deliberately built from the exception's **type and where it was raised**, and
    never from its message: a message usually carries the identifier of whatever
    was being worked on, so fingerprinting on it would produce one group per
    request and an error list that is a log with extra steps. Grouping on type
    and location gives *"``IntegrityError`` in ``repositories/case.py:118``, 240
    times since 09:14"*, which is the sentence an operator can act on.

    Short and hex, so it is a stable handle a client can page on and a person can
    quote in a ticket.
    """
    import hashlib

    material = "|".join(
        (category.value, component.value, exception_type, location or "")
    )
    return hashlib.blake2b(material.encode("utf-8"), digest_size=8).hexdigest()


def status_class(status_code: int) -> str:
    """Return an HTTP status as its class (``2xx``, ``4xx``, …).

    A **label with five possible values instead of sixty**, and that is the whole
    reason it exists: ``http_requests_total`` is broken down by method, route,
    and this, so keeping the last dimension small is what keeps the product of
    the three from being a cardinality problem. The exact status is still in the
    log line for the request, which is where somebody investigating one goes.
    """
    if status_code < 100 or status_code > 599:
        return "unknown"
    return f"{status_code // 100}xx"


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


class AlertSeverity(StrEnum):
    """How urgent a firing alert is."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class AlertRule:
    """A condition an operator would want to know about, declared rather than coded.

    ``22-monitoring.md`` asks the platform to *"prepare the monitoring
    infrastructure for alerting"* while putting **delivery out of scope**, and
    this is exactly that line: the platform decides *what counts as a problem*
    and evaluates it; **nothing sends anything**. ``GET /monitoring/alerts``
    reports which of these are firing, and an Alertmanager, a cron job, or a
    person reading the page is the delivery mechanism — none of which needs a
    change here.

    Declared as data so that a deployment's thresholds are a
    :mod:`core.config` value rather than a release, and so the *set* of rules is
    reviewable in one screen instead of being spread across the code that
    evaluates them.
    """

    key: str
    severity: AlertSeverity
    component: MonitoringComponent
    #: What is wrong, in an operator's words. Carries no numbers — the evaluated
    #: value travels beside it, so one sentence serves every threshold.
    summary: str


#: The conditions ``22-monitoring.md``'s Alerts section names, plus the two the
#: platform's own architecture makes obvious (a queue backlog is per queue, and a
#: dead background pool is invisible from every other figure).
#:
#: **Thresholds are not here.** They live in :mod:`core.config`
#: (``MONITORING_*_THRESHOLD``), because a five-percent error rate is alarming for
#: one deployment and a quiet afternoon for another, and a rule whose threshold is
#: a constant is a rule somebody disables instead of tuning.
ALERT_RULES: Final[tuple[AlertRule, ...]] = (
    AlertRule(
        "database_unavailable",
        AlertSeverity.CRITICAL,
        MonitoringComponent.DATABASE,
        "PostgreSQL is not reachable from this API process.",
    ),
    AlertRule(
        "cache_unavailable",
        AlertSeverity.CRITICAL,
        MonitoringComponent.CACHE,
        "Redis is not reachable, so sign-out and login throttling cannot be enforced.",
    ),
    AlertRule(
        "storage_unavailable",
        AlertSeverity.CRITICAL,
        MonitoringComponent.STORAGE,
        "MinIO is not reachable, so documents cannot be stored or served.",
    ),
    AlertRule(
        "vector_unavailable",
        AlertSeverity.WARNING,
        MonitoringComponent.VECTOR,
        "Qdrant is not reachable, so semantic search and the AI pipeline are refusing.",
    ),
    AlertRule(
        "error_rate_high",
        AlertSeverity.CRITICAL,
        MonitoringComponent.API,
        "The share of requests answered with a server error is above its threshold.",
    ),
    AlertRule(
        "latency_high",
        AlertSeverity.WARNING,
        MonitoringComponent.API,
        "Average request latency is above its threshold.",
    ),
    AlertRule(
        "queue_backlog",
        AlertSeverity.WARNING,
        MonitoringComponent.MONITORING,
        "A background queue holds more work than its threshold allows.",
    ),
    AlertRule(
        "background_workers_stopped",
        AlertSeverity.CRITICAL,
        MonitoringComponent.MONITORING,
        "A background worker pool is not running, so its queue will never drain.",
    ),
    AlertRule(
        "authentication_failures_high",
        AlertSeverity.WARNING,
        MonitoringComponent.AUTH,
        "Failed sign-ins are above their threshold for this window.",
    ),
)
