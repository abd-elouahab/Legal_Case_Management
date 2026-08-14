"""Monitoring response schemas.

Every model here is **read-only**: the monitoring module writes nothing, so there
is no ``*Create`` and no ``*Update`` in this file, and its absence is the same
property :mod:`schemas.dashboard` has for the same reason.

Two rules from `code-standards.md` shape what these carry.

**A view returns keys, never prose.** Every component, state, category, event,
and metric name travels as a stable identifier; the words live in
``apps/web/messages/*.json``. That matters here even though the audience is an
administrator — a monitoring page is read in Arabic by the person who
administers an Arabic deployment, and an API response is a place a translation
cannot live.

**Never invent a figure.** A latency with no observations is ``null``, not zero,
which a client renders as an em dash; a queue whose depth could not be read is
absent with ``depths_unavailable`` beside it rather than reported as empty. The
one thing worse than a missing number on an operational page is a wrong one that
looks fine.

The `detail` fields are the deliberate exception to the platform's usual rule
that responses carry no free text: an unreachable database's driver message and a
tracked error's sample message are what make an operational view actionable. Both
are bounded and redacted before they get here (see
:func:`~core.observability.redact_text`), and both are served only to a
``monitoring:view`` holder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from core.observability import (
    AlertSeverity,
    ErrorCategory,
    HealthState,
    MetricType,
    MetricUnit,
    MonitoringComponent,
    SecurityEventType,
    SecuritySeverity,
)
from core.readiness import ExternalServiceStatus
from core.tracing import SpanKind, SpanStatus
from services.error_tracker import ErrorSnapshot
from services.metrics_registry import MetricSeries, MetricsSnapshot
from services.monitoring import (
    AlertStatus,
    DependencyHealth,
    HealthReport,
    JobQueueStatus,
    JobsReport,
    LatencyReport,
    MonitoringOverview,
    PerformanceReport,
)
from services.security_monitor import SecuritySnapshot
from services.system_metrics import SystemSnapshot, WorkerPoolStatus
from services.tracer import RecordedSpan, RecordedTrace, TraceSnapshot

__all__ = [
    "AlertRead",
    "AlertsRead",
    "DependencyHealthRead",
    "ErrorsRead",
    "ExternalServiceRead",
    "HealthReportRead",
    "JobsRead",
    "LatencyRead",
    "MetricsRead",
    "MonitoringOverviewRead",
    "PerformanceRead",
    "SecurityRead",
    "SystemRead",
    "TracedRequestRead",
    "TracesRead",
    "TrackedErrorRead",
    "WorkerPoolRead",
]


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


class SystemRead(BaseModel):
    """The process and its runtime."""

    started_at: datetime
    uptime_seconds: float
    uptime: str = Field(description="Human-readable uptime, e.g. '3d 4h 12m'.")
    process_id: int
    thread_count: int
    python_version: str
    platform: str
    environment: str
    version: str
    project_name: str

    @classmethod
    def from_snapshot(cls, snapshot: SystemSnapshot) -> SystemRead:
        """Build from the system snapshot."""
        return cls(
            started_at=snapshot.started_at,
            uptime_seconds=snapshot.uptime_seconds,
            uptime=snapshot.uptime_human,
            process_id=snapshot.process_id,
            thread_count=snapshot.thread_count,
            python_version=snapshot.python_version,
            platform=snapshot.platform,
            environment=snapshot.environment,
            version=snapshot.version,
            project_name=snapshot.project_name,
        )


class DependencyHealthRead(BaseModel):
    """One backing service's reachability."""

    name: str
    state: HealthState
    required: bool = Field(
        description="Whether the platform is unable to serve at all without this."
    )
    detail: str | None = None

    @classmethod
    def from_health(cls, health: DependencyHealth) -> DependencyHealthRead:
        """Build from the service's report."""
        return cls(
            name=health.name, state=health.state, required=health.required, detail=health.detail
        )


class ExternalServiceRead(BaseModel):
    """One outward-facing integration, as configuration describes it."""

    name: str
    enabled: bool
    configured: bool
    state: HealthState
    detail: str | None = Field(
        default=None,
        description="Which setting is missing, by name. Never a value.",
    )

    @classmethod
    def from_status(cls, status: ExternalServiceStatus) -> ExternalServiceRead:
        """Build from the readiness probe."""
        return cls(
            name=status.name,
            enabled=status.enabled,
            configured=status.configured,
            state=status.state,
            detail=status.detail,
        )


class WorkerPoolRead(BaseModel):
    """One background worker pool."""

    name: str
    running: bool
    concurrency: int
    state: HealthState

    @classmethod
    def from_status(cls, status: WorkerPoolStatus) -> WorkerPoolRead:
        """Build from the pool probe."""
        return cls(
            name=status.name,
            running=status.running,
            concurrency=status.concurrency,
            state=status.state,
        )


class HealthReportRead(BaseModel):
    """The platform's operational state."""

    state: HealthState
    checked_at: datetime
    system: SystemRead
    dependencies: list[DependencyHealthRead]
    external_services: list[ExternalServiceRead]
    workers: list[WorkerPoolRead]

    @classmethod
    def from_report(cls, report: HealthReport) -> HealthReportRead:
        """Build from the service's report."""
        return cls(
            state=report.state,
            checked_at=report.checked_at,
            system=SystemRead.from_snapshot(report.system),
            dependencies=[DependencyHealthRead.from_health(item) for item in report.dependencies],
            external_services=[
                ExternalServiceRead.from_status(item) for item in report.external_services
            ],
            workers=[WorkerPoolRead.from_status(item) for item in report.workers],
        )


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


class HistogramRead(BaseModel):
    """An accumulated distribution, with the quantiles estimated from its buckets."""

    count: int
    sum: float
    minimum: float | None
    maximum: float | None
    average: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    buckets: dict[str, int] = Field(
        description="Cumulative counts keyed by upper bound, for a client that wants to draw one."
    )


class MetricSeriesRead(BaseModel):
    """One metric at one label combination."""

    name: str
    type: MetricType
    unit: MetricUnit
    component: MonitoringComponent
    description: str
    labels: dict[str, str]
    value: float | None = None
    histogram: HistogramRead | None = None

    @classmethod
    def from_series(cls, series: MetricSeries) -> MetricSeriesRead:
        """Build from one recorded series."""
        histogram = None
        if series.histogram is not None:
            histogram = HistogramRead(
                count=series.histogram.count,
                sum=series.histogram.sum,
                minimum=series.histogram.minimum,
                maximum=series.histogram.maximum,
                average=series.histogram.average,
                p50=series.histogram.quantile(0.50),
                p95=series.histogram.quantile(0.95),
                p99=series.histogram.quantile(0.99),
                buckets={str(bound): count for bound, count in series.histogram.buckets},
            )
        return cls(
            name=series.name.value,
            type=series.type,
            unit=series.unit,
            component=series.component,
            description=series.description,
            labels=series.label_map,
            value=series.value,
            histogram=histogram,
        )


class MetricsRead(BaseModel):
    """Every series the registry holds, plus the eleven features' own figures."""

    since: datetime
    taken_at: datetime
    dropped_series: int = Field(
        description="Series refused at the cardinality ceiling. Non-zero means this is partial."
    )
    series: list[MetricSeriesRead]
    features: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description=(
            "Figures read from each feature's own recorder, keyed by feature then by figure. "
            "Read, never written: those recorders remain the source of truth."
        ),
    )

    @classmethod
    def from_snapshot(
        cls, snapshot: MetricsSnapshot, *, features: dict[str, dict[str, float]] | None = None
    ) -> MetricsRead:
        """Build from the registry snapshot."""
        return cls(
            since=snapshot.since,
            taken_at=snapshot.taken_at,
            dropped_series=snapshot.dropped_series,
            series=[MetricSeriesRead.from_series(item) for item in snapshot.series],
            features=features or {},
        )


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #


class LatencyRead(BaseModel):
    """One latency distribution.

    The percentile fields are ``null`` where the underlying recorder keeps a mean
    rather than a distribution — which is most of the feature recorders, each of
    which documents that choice. A null percentile is *"this cannot be known"*,
    and it is deliberately not the same as a zero.
    """

    name: str
    count: int
    average_ms: float | None
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    max_ms: float | None = None

    @classmethod
    def from_report(cls, report: LatencyReport) -> LatencyRead:
        """Build from the service's report."""
        return cls(
            name=report.name,
            count=report.count,
            average_ms=report.average_ms,
            p50_ms=report.p50_ms,
            p95_ms=report.p95_ms,
            p99_ms=report.p99_ms,
            max_ms=report.max_ms,
        )


class SlowRouteRead(BaseModel):
    """One route and its mean response time."""

    route: str
    average_ms: float


class PerformanceRead(BaseModel):
    """Throughput, latency, and the error rate."""

    since: datetime
    requests_total: float
    requests_by_status: dict[str, float]
    error_rate: float = Field(description="Share of requests answered 5xx, as a percentage.")
    latencies: list[LatencyRead]
    slowest_routes: list[SlowRouteRead]

    @classmethod
    def from_report(cls, report: PerformanceReport) -> PerformanceRead:
        """Build from the service's report."""
        return cls(
            since=report.since,
            requests_total=report.requests_total,
            requests_by_status=report.requests_by_status,
            error_rate=report.error_rate,
            latencies=[LatencyRead.from_report(item) for item in report.latencies],
            slowest_routes=[
                SlowRouteRead(route=route, average_ms=average)
                for route, average in report.slowest_routes
            ],
        )


# --------------------------------------------------------------------------- #
# Background jobs
# --------------------------------------------------------------------------- #


class JobQueueRead(BaseModel):
    """One background queue's depth."""

    name: str
    pending: int
    processing: int
    depth: int
    completed: int | None = None
    failed: int | None = None

    @classmethod
    def from_status(cls, status: JobQueueStatus) -> JobQueueRead:
        """Build from the service's report."""
        return cls(
            name=status.name,
            pending=status.pending,
            processing=status.processing,
            depth=status.depth,
            completed=status.completed,
            failed=status.failed,
        )


class JobsRead(BaseModel):
    """Background processing across every queue and pool."""

    queues: list[JobQueueRead]
    workers: list[WorkerPoolRead]
    total_depth: int
    depths_unavailable: bool

    @classmethod
    def from_report(cls, report: JobsReport) -> JobsRead:
        """Build from the service's report."""
        return cls(
            queues=[JobQueueRead.from_status(item) for item in report.queues],
            workers=[WorkerPoolRead.from_status(item) for item in report.workers],
            total_depth=report.total_depth,
            depths_unavailable=report.depths_unavailable,
        )


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class TrackedErrorRead(BaseModel):
    """One *class* of failure, grouped by type and location."""

    fingerprint: str
    category: ErrorCategory
    component: MonitoringComponent
    exception_type: str
    location: str | None
    operation: str | None
    sample_message: str | None
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    status_code: int | None = None
    last_trace_id: str | None = Field(
        default=None,
        description="The trace of the most recent occurrence, if it happened inside one.",
    )


class ErrorsRead(BaseModel):
    """Tracked failures and their totals."""

    since: datetime
    total_errors: int
    distinct_errors: int
    errors_by_category: dict[str, int]
    errors_by_component: dict[str, int]
    evicted_groups: int
    groups: list[TrackedErrorRead]

    @classmethod
    def from_snapshot(cls, snapshot: ErrorSnapshot, *, limit: int | None = None) -> ErrorsRead:
        """Build from the tracker's snapshot, keeping the most recent ``limit``."""
        groups = snapshot.groups[:limit] if limit else snapshot.groups
        return cls(
            since=snapshot.since,
            total_errors=snapshot.total_errors,
            distinct_errors=snapshot.distinct_errors,
            errors_by_category=snapshot.errors_by_category,
            errors_by_component=snapshot.errors_by_component,
            evicted_groups=snapshot.evicted_groups,
            groups=[TrackedErrorRead.model_validate(item, from_attributes=True) for item in groups],
        )


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


class SecurityEventRead(BaseModel):
    """One security-relevant event.

    Carries no account, no address, and no credential — see
    :class:`~services.security_monitor.SecurityEventRecord` for what each absent
    field would have cost.
    """

    occurred_at: datetime
    event: SecurityEventType
    severity: SecuritySeverity
    role: str | None = None
    reason: str | None = None
    source: str | None = Field(
        default=None,
        description=(
            "A salted digest prefix. Correlates two events without naming anyone, and is "
            "meaningless outside this process."
        ),
    )
    trace_id: str | None = None


class SecurityRead(BaseModel):
    """Security counters, windowed rates, and the recent feed."""

    since: datetime
    total_events: int
    events_by_type: dict[str, int]
    events_by_severity: dict[str, int]
    recent_rates: dict[str, dict[str, int]]
    distinct_sources: int
    sources_capped: bool
    login_attempts: int
    failed_logins: int
    login_failure_rate: float
    recent: list[SecurityEventRead]

    @classmethod
    def from_snapshot(cls, snapshot: SecuritySnapshot, *, limit: int | None = None) -> SecurityRead:
        """Build from the monitor's snapshot."""
        recent = snapshot.recent[:limit] if limit else snapshot.recent
        return cls(
            since=snapshot.since,
            total_events=snapshot.total_events,
            events_by_type=snapshot.events_by_type,
            events_by_severity=snapshot.events_by_severity,
            recent_rates=snapshot.recent_rates,
            distinct_sources=snapshot.distinct_sources,
            sources_capped=snapshot.sources_capped,
            login_attempts=snapshot.login_attempts,
            failed_logins=snapshot.failed_logins,
            login_failure_rate=snapshot.login_failure_rate,
            recent=[SecurityEventRead.model_validate(item, from_attributes=True) for item in recent],
        )


# --------------------------------------------------------------------------- #
# Traces
# --------------------------------------------------------------------------- #


class SpanRead(BaseModel):
    """One timed unit of work inside a trace."""

    name: str
    component: MonitoringComponent
    kind: SpanKind
    span_id: str
    parent_span_id: str | None
    started_at: datetime
    duration_ms: float
    status: SpanStatus
    error_type: str | None = None
    error_message: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_span(cls, span: RecordedSpan) -> SpanRead:
        """Build from a recorded span."""
        return cls(
            name=span.name,
            component=span.component,
            kind=span.kind,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            started_at=span.started_at,
            duration_ms=span.duration_ms,
            status=span.status,
            error_type=span.error_type,
            error_message=span.error_message,
            attributes=span.attributes,
        )


class TracedRequestRead(BaseModel):
    """One completed trace: a root span and everything inside it."""

    trace_id: str
    name: str
    component: MonitoringComponent
    started_at: datetime
    duration_ms: float
    status: SpanStatus
    failed: bool
    remote_parent: bool
    dropped_spans: int
    spans: list[SpanRead]

    @classmethod
    def from_trace(cls, trace: RecordedTrace) -> TracedRequestRead:
        """Build from a recorded trace."""
        return cls(
            trace_id=trace.trace_id,
            name=trace.name,
            component=trace.component,
            started_at=trace.started_at,
            duration_ms=trace.duration_ms,
            status=trace.status,
            failed=trace.failed,
            remote_parent=trace.remote_parent,
            dropped_spans=trace.dropped_spans,
            spans=[SpanRead.from_span(span) for span in trace.spans],
        )


class TracesRead(BaseModel):
    """The tracer's counters and its most recent traces."""

    since: datetime
    traces_started: int
    traces_recorded: int
    spans_started: int
    spans_dropped: int
    failed_traces: int
    traces: list[TracedRequestRead]

    @classmethod
    def from_snapshot(cls, snapshot: TraceSnapshot) -> TracesRead:
        """Build from the tracer's snapshot."""
        return cls(
            since=snapshot.since,
            traces_started=snapshot.traces_started,
            traces_recorded=snapshot.traces_recorded,
            spans_started=snapshot.spans_started,
            spans_dropped=snapshot.spans_dropped,
            failed_traces=snapshot.failed_traces,
            traces=[TracedRequestRead.from_trace(item) for item in snapshot.recent],
        )


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


class AlertRead(BaseModel):
    """One declared condition and whether it currently holds."""

    key: str
    severity: AlertSeverity
    component: MonitoringComponent
    summary: str
    firing: bool
    value: float | None = None
    threshold: float | None = None
    detail: str | None = None

    @classmethod
    def from_status(cls, status: AlertStatus) -> AlertRead:
        """Build from an evaluated rule."""
        return cls(
            key=status.rule.key,
            severity=status.rule.severity,
            component=status.rule.component,
            summary=status.rule.summary,
            firing=status.firing,
            value=status.value,
            threshold=status.threshold,
            detail=status.detail,
        )


class AlertsRead(BaseModel):
    """Every declared condition, evaluated.

    **Nothing is delivered.** ``22-monitoring.md`` puts alert delivery out of
    scope; this endpoint is the prepared infrastructure it asks for, and an
    Alertmanager, a cron job, or a person reading the page is what turns a firing
    rule into somebody's phone ringing.
    """

    evaluated_at: datetime
    firing: int
    alerts: list[AlertRead]

    @classmethod
    def from_statuses(cls, statuses: tuple[AlertStatus, ...]) -> AlertsRead:
        """Build from the evaluated rules."""
        return cls(
            evaluated_at=datetime.now(UTC),
            firing=sum(1 for status in statuses if status.firing),
            alerts=[AlertRead.from_status(status) for status in statuses],
        )


# --------------------------------------------------------------------------- #
# The overview
# --------------------------------------------------------------------------- #


class MonitoringOverviewRead(BaseModel):
    """Everything an operator's first screen needs, in one response."""

    generated_at: datetime
    state: HealthState
    health: HealthReportRead
    performance: PerformanceRead
    jobs: JobsRead
    errors: ErrorsRead
    security: SecurityRead
    traces: TracesRead
    alerts: AlertsRead
    unavailable: list[str] = Field(
        default_factory=list,
        description=(
            "Sections that could not be assembled. Empty on a healthy read; non-empty means "
            "this page is partial and says which part."
        ),
    )

    @classmethod
    def from_overview(
        cls, overview: MonitoringOverview, *, error_limit: int = 20, security_limit: int = 20
    ) -> MonitoringOverviewRead:
        """Build from the service's aggregate."""
        return cls(
            generated_at=overview.generated_at,
            state=overview.health.state,
            health=HealthReportRead.from_report(overview.health),
            performance=PerformanceRead.from_report(overview.performance),
            jobs=JobsRead.from_report(overview.jobs),
            errors=ErrorsRead.from_snapshot(overview.errors, limit=error_limit),
            security=SecurityRead.from_snapshot(overview.security, limit=security_limit),
            traces=TracesRead.from_snapshot(overview.traces),
            alerts=AlertsRead.from_statuses(overview.alerts),
            unavailable=list(overview.unavailable),
        )
