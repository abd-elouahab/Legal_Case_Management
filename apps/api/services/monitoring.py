"""The monitoring service: one read across every observation the platform takes.

This is the **second view on the platform**, after the dashboard, and it is worth
saying what it inherits from that and what it deliberately does not.

It inherits the shape. `code-standards.md`'s *"Dashboards and read-only views"*
rules were written for :mod:`services.dashboard` and every one of them applies
here: **a view owns no data** (no table, no migration, no event, no worker),
**every unit fails alone** (each section is assembled inside its own ``try`` and
reports itself unavailable rather than costing the response), and **a view returns
keys, never prose**.

It does *not* inherit the authorization model, and the difference is the whole
reason this is a separate module. Every dashboard figure is scoped to the caller
— that is what ``dashboard_access.py`` exists for. **Nothing here is scoped to
anybody**, because there is nothing to scope: an uptime, a queue depth, and a
p95 latency are properties of the *platform*, not of anyone's cases. So there is
no ``monitoring_access.py``, and its absence is the design rather than an
omission — the question a per-resource policy would answer cannot be asked here.
What replaces it is a permission (``monitoring:view``) held by administrators
alone, which is ``22-monitoring.md``'s *"regular users must never access
monitoring endpoints or operational metrics"* enforced at the only place there is
to enforce it.

**It reads eleven other recorders and writes to none of them.** The features that
shipped with their own metrics keep them; this service copies their figures into
one snapshot so an operator has a page rather than eleven, and bridges them into
the metric registry so a scraper has one endpoint rather than eleven. The one
recorder it deliberately does **not** read is
:mod:`services.event_metrics`: its snapshot needs the live connection count, which
only :mod:`websocket.manager` holds, and `code-standards.md` says nothing outside
that package, the lifespan, and the endpoint may import it. Real-time metrics stay
on ``GET /realtime/metrics``, and the overview links to that rather than
duplicating it — a rule this feature had every opportunity to break and did not.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from core.config import settings
from core.observability import (
    ALERT_RULES,
    AlertRule,
    HealthState,
    MetricName,
    status_class,
    worse_health,
)
from core.readiness import (
    REQUIRED_DEPENDENCIES,
    ExternalServiceStatus,
    ProbeResult,
    probe_external_services,
)
from repositories.dashboard import DashboardRepository
from repositories.email import EmailDeliveryRepository
from repositories.whatsapp import WhatsAppDeliveryRepository
from services.database_metrics import record_pool_gauges
from services.error_tracker import ErrorSnapshot, ErrorTracker
from services.metrics_registry import MetricsRegistry, MetricsSnapshot
from services.security_monitor import SecurityMonitor, SecuritySnapshot
from services.system_metrics import (
    SystemSnapshot,
    WorkerPoolStatus,
    collect_process_gauges,
    system_snapshot,
    worker_pools,
)
from services.tracer import Tracer, TraceSnapshot

logger = structlog.get_logger(__name__)

__all__ = [
    "AlertStatus",
    "DependencyHealth",
    "HealthReport",
    "JobQueueStatus",
    "JobsReport",
    "MonitoringOverview",
    "MonitoringService",
    "PerformanceReport",
]


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    """One backing service's reachability, as this process last observed it."""

    name: str
    state: HealthState
    required: bool
    #: The failure, when there was one. Carries a driver's message, which is why
    #: it is only ever served to a ``monitoring:view`` holder — an unreachable
    #: PostgreSQL names a host, a port, and a database.
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The platform's operational state, assembled from four sources."""

    state: HealthState
    checked_at: datetime
    dependencies: tuple[DependencyHealth, ...]
    external_services: tuple[ExternalServiceStatus, ...]
    workers: tuple[WorkerPoolStatus, ...]
    system: SystemSnapshot


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """One latency distribution, in the shape a page renders."""

    name: str
    count: int
    average_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    """The Performance Metrics section of ``22-monitoring.md``, answered.

    Six of the seven figures it lists come from a recorder that already had them
    — OCR, indexing, search, AI latency, and report generation each measured
    their own long before this feature existed — and two are new here: API
    response time and database query duration, which belonged to no feature and
    are why the middleware and the engine listener exist.
    """

    since: datetime
    latencies: tuple[LatencyReport, ...]
    requests_total: float
    requests_by_status: dict[str, float]
    error_rate: float
    slowest_routes: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class JobQueueStatus:
    """One background queue's depth, counted from persisted rows."""

    name: str
    pending: int
    processing: int
    #: Terminal counts, when the queue's rows record them. ``None`` where the
    #: platform does not keep a completed count — an OCR run's completion is its
    #: row's status, and counting every completed run ever is a full scan to
    #: render a number nobody acts on.
    completed: int | None = None
    failed: int | None = None

    @property
    def depth(self) -> int:
        """Work the platform owes on this queue: pending plus in flight."""
        return self.pending + self.processing


@dataclass(frozen=True, slots=True)
class JobsReport:
    """Background processing, as ``22-monitoring.md``'s section of that name asks."""

    queues: tuple[JobQueueStatus, ...]
    workers: tuple[WorkerPoolStatus, ...]
    #: True when the depths could not be read. The queues are then empty rather
    #: than zero, and the flag is what stops a reader mistaking one for the other.
    depths_unavailable: bool = False

    @property
    def total_depth(self) -> int:
        """Work outstanding across every queue."""
        return sum(queue.depth for queue in self.queues)


@dataclass(frozen=True, slots=True)
class AlertStatus:
    """Whether one declared condition is currently true.

    **Evaluated, never delivered.** ``22-monitoring.md`` puts alert delivery out
    of scope and asks only that the infrastructure be prepared for it; this is
    exactly that line. Nothing in the platform reads this — an Alertmanager, a
    cron job, or a person looking at the page is the delivery mechanism, and none
    of them needs a change here.
    """

    rule: AlertRule
    firing: bool
    #: The measurement that decided it, and what it was compared against. Both
    #: reported, because *"error rate is high"* is not actionable and *"7.2 %
    #: against a 5 % threshold"* is.
    value: float | None = None
    threshold: float | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class MonitoringOverview:
    """One page's worth of operational state.

    The aggregate a dashboard opens with, and the only endpoint an operator needs
    bookmarked. Every field here is also reachable through a narrower endpoint —
    the same *"one unit of work is a widget, never a page"* rule the dashboard
    follows, so the overview and the individual reads can never disagree.
    """

    generated_at: datetime
    health: HealthReport
    performance: PerformanceReport
    jobs: JobsReport
    errors: ErrorSnapshot
    security: SecuritySnapshot
    traces: TraceSnapshot
    alerts: tuple[AlertStatus, ...]
    #: Sections that could not be assembled, by name. Empty on a healthy read;
    #: non-empty means the page is partial and says which part.
    unavailable: tuple[str, ...] = ()

    @property
    def firing_alerts(self) -> tuple[AlertStatus, ...]:
        """Only the conditions that are currently true."""
        return tuple(alert for alert in self.alerts if alert.firing)


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class MonitoringService:
    """Assemble the platform's operational state from every recorder it keeps.

    Constructed per request with a repository, and with the process-wide
    recorders injected — the same shape every service on this platform has, and
    it is what lets a test hand it null recorders and assert that the endpoints
    still answer.
    """

    def __init__(
        self,
        *,
        dashboard: DashboardRepository,
        emails: EmailDeliveryRepository,
        whatsapp: WhatsAppDeliveryRepository,
        metrics: MetricsRegistry,
        tracer: Tracer,
        errors: ErrorTracker,
        security: SecurityMonitor,
    ) -> None:
        self._dashboard = dashboard
        self._emails = emails
        self._whatsapp = whatsapp
        self._metrics = metrics
        self._tracer = tracer
        self._errors = errors
        self._security = security

    # ------------------------------------------------------------------ health #

    def health(self, dependencies: dict[str, ProbeResult]) -> HealthReport:
        """Aggregate dependency, external-service, and worker health.

        The dependency probes are passed **in** rather than run here, because
        they are the one part of this that is genuinely asynchronous — the
        endpoint awaits :func:`~core.readiness.probe_dependencies` and hands the
        results over. That keeps this service synchronous, which is what lets it
        be called from anywhere including a worker.
        """
        checks = tuple(
            DependencyHealth(
                name=name,
                state=HealthState.HEALTHY if result.healthy else HealthState.UNHEALTHY,
                required=name in REQUIRED_DEPENDENCIES,
                detail=result.error,
            )
            for name, result in sorted(dependencies.items())
        )

        external = tuple(probe_external_services().values())
        pools = worker_pools()

        # A **required** dependency being down makes the platform unhealthy; an
        # optional one makes it degraded. Without that distinction a deployment
        # with Qdrant down — where cases, documents, users, and notifications all
        # work — would report itself as broken, and whoever is on call would spend
        # the first ten minutes looking in the wrong place.
        states = [
            check.state
            if check.required
            else (HealthState.DEGRADED if check.state is HealthState.UNHEALTHY else check.state)
            for check in checks
        ]
        states.extend(service.state for service in external)
        states.extend(pool.state for pool in pools)

        return HealthReport(
            state=worse_health(*states),
            checked_at=datetime.now(UTC),
            dependencies=checks,
            external_services=external,
            workers=pools,
            system=system_snapshot(),
        )

    # ----------------------------------------------------------------- metrics #

    def metrics_snapshot(self, *, engine: Any | None = None) -> MetricsSnapshot:
        """Collect the pull-mode gauges, then read every series.

        The order is the point: process uptime, thread count, connection-pool
        occupancy, queue depths, and the eleven features' figures are all
        *collected* immediately before the snapshot is taken, so a scrape and a
        page describe the same instant. Sampling them on a timer would make the
        two disagree by however long the timer had been sleeping.
        """
        self._collect(engine=engine)
        return self._metrics.snapshot()

    def _collect(self, *, engine: Any | None = None) -> None:
        """Write every pull-mode gauge into the registry. Never raises."""
        try:
            collect_process_gauges(self._metrics)
        except Exception:  # pragma: no cover - defensive
            logger.debug("process_gauges_failed")

        if engine is not None:
            try:
                record_pool_gauges(engine, self._metrics)
            except Exception:  # pragma: no cover - defensive
                logger.debug("pool_gauges_failed")

        try:
            for queue in self.jobs().queues:
                self._metrics.set_gauge(
                    MetricName.JOB_QUEUE_DEPTH,
                    float(queue.pending),
                    labels={"queue": queue.name, "state": "pending"},
                )
                self._metrics.set_gauge(
                    MetricName.JOB_QUEUE_DEPTH,
                    float(queue.processing),
                    labels={"queue": queue.name, "state": "processing"},
                )
        except Exception:  # pragma: no cover - defensive
            logger.debug("queue_gauges_failed")

        try:
            for feature, figures in feature_metrics().items():
                for figure, value in figures.items():
                    self._metrics.set_gauge(
                        MetricName.FEATURE_METRIC,
                        value,
                        labels={"feature": feature, "metric": figure},
                    )
        except Exception:  # pragma: no cover - defensive
            logger.debug("feature_metric_bridge_failed")

    # ------------------------------------------------------------- performance #

    def performance(self, snapshot: MetricsSnapshot | None = None) -> PerformanceReport:
        """Derive the latency and throughput figures from the metric registry.

        Everything here is computed from the snapshot rather than measured
        separately, which is what makes the page and the exposition endpoint
        arithmetically consistent: two independent calculations of an error rate
        is how a dashboard and an alert end up disagreeing about whether an
        incident is happening.
        """
        current = snapshot or self._metrics.snapshot()

        latencies: list[LatencyReport] = []
        for name, label in (
            (MetricName.HTTP_REQUEST_DURATION_MS, "api_response"),
            (MetricName.DB_QUERY_DURATION_MS, "database_query"),
            (MetricName.SPAN_DURATION_MS, "span"),
            (MetricName.JOB_DURATION_MS, "background_job"),
            (MetricName.EXTERNAL_CALL_DURATION_MS, "external_call"),
        ):
            histogram = current.histogram(name)
            if histogram is None or histogram.count == 0:
                continue
            latencies.append(
                LatencyReport(
                    name=label,
                    count=histogram.count,
                    average_ms=histogram.average,
                    p50_ms=histogram.quantile(0.50),
                    p95_ms=histogram.quantile(0.95),
                    p99_ms=histogram.quantile(0.99),
                    max_ms=histogram.maximum,
                )
            )

        latencies.extend(self._feature_latencies())

        by_status: dict[str, float] = {}
        for series in current.by_name(MetricName.HTTP_REQUESTS_TOTAL):
            key = series.label_map.get("status_class", "unknown")
            by_status[key] = round(by_status.get(key, 0.0) + (series.value or 0.0), 3)

        total = round(sum(by_status.values()), 3)
        failures = by_status.get(status_class(500), 0.0)

        return PerformanceReport(
            since=current.since,
            latencies=tuple(latencies),
            requests_total=total,
            requests_by_status=by_status,
            error_rate=round(failures / total * 100, 2) if total > 0 else 0.0,
            slowest_routes=self._slowest_routes(current),
        )

    @staticmethod
    def _slowest_routes(snapshot: MetricsSnapshot, *, limit: int = 5) -> tuple[tuple[str, float], ...]:
        """The routes with the highest mean latency.

        Mean rather than p95 per route, deliberately: a per-route histogram holds
        a distribution *per label combination*, and estimating a quantile from a
        route that has been called four times produces a number with a bucket's
        worth of error and a page's worth of authority. The mean is honest at
        small counts, and the p95 of the whole surface is reported beside it.
        """
        means: list[tuple[str, float]] = []
        for series in snapshot.by_name(MetricName.HTTP_REQUEST_DURATION_MS):
            histogram = series.histogram
            if histogram is None or histogram.count == 0 or histogram.average is None:
                continue
            means.append((series.label_map.get("route", "unknown"), histogram.average))
        means.sort(key=lambda item: item[1], reverse=True)
        return tuple(means[:limit])

    @staticmethod
    def _feature_latencies() -> list[LatencyReport]:
        """Pull the average-duration figure each feature recorder already holds.

        Only the average, and only where the recorder has one: those recorders
        accumulate a sum and a count rather than a distribution — a deliberate
        choice each of them documents — so a percentile is a number they cannot
        honestly produce and this does not invent one.
        """
        reports: list[LatencyReport] = []
        for feature, figures in feature_metrics().items():
            for key, value in figures.items():
                if not key.startswith("average_") or not key.endswith(("_ms", "_time")):
                    continue
                reports.append(
                    LatencyReport(
                        name=f"{feature}.{key}",
                        count=int(figures.get("total_requests", 0) or 0),
                        average_ms=value,
                        p50_ms=None,
                        p95_ms=None,
                        p99_ms=None,
                        max_ms=None,
                    )
                )
        return reports

    # -------------------------------------------------------------------- jobs #

    def jobs(self) -> JobsReport:
        """Report every background queue's depth and every pool's liveness.

        Depths are counted from **persisted rows** — `code-standards.md`'s *"count
        persisted state, not process state"* — through the same repository the
        dashboard uses, so the two pages cannot report different backlogs.
        """
        pools = worker_pools()
        try:
            depths = self._dashboard.queue_depths()
            queues = [
                JobQueueStatus("ocr", depths.ocr_pending, depths.ocr_processing),
                JobQueueStatus("indexing", depths.indexing_pending, depths.indexing_processing),
                JobQueueStatus("reports", depths.report_pending, depths.report_processing),
            ]
        except Exception:
            logger.exception("queue_depths_unavailable")
            return JobsReport(queues=(), workers=pools, depths_unavailable=True)

        try:
            email = self._emails.statistics()
            queues.append(
                JobQueueStatus(
                    "email", email.pending, email.sending, completed=email.sent, failed=email.failed
                )
            )
        except Exception:
            logger.exception("email_queue_depth_unavailable")

        try:
            whatsapp = self._whatsapp.statistics()
            queues.append(
                JobQueueStatus(
                    "whatsapp",
                    whatsapp.pending,
                    whatsapp.sending,
                    completed=whatsapp.delivered,
                    failed=whatsapp.failed,
                )
            )
        except Exception:
            logger.exception("whatsapp_queue_depth_unavailable")

        return JobsReport(queues=tuple(queues), workers=pools)

    # ------------------------------------------- errors, security, and traces #
    #
    # Three one-line delegations, and they exist rather than the router reaching
    # for the recorders directly for one reason: the router is then written
    # against **one** collaborator. When a Redis-backed error tracker or an
    # OpenTelemetry exporter replaces one of these, it is swapped in
    # :mod:`api.deps` and no endpoint changes — which is the same argument every
    # protocol on this platform makes, applied to the seam between an HTTP surface
    # and the process's recorders.

    def errors(self) -> ErrorSnapshot:
        """Read the tracked failure groups."""
        return self._errors.snapshot()

    def security(self) -> SecuritySnapshot:
        """Read the security counters, rates, and feed."""
        return self._security.snapshot()

    def traces(self, *, limit: int | None = None) -> TraceSnapshot:
        """Read the most recent traces."""
        return self._tracer.snapshot(limit=limit)

    # ------------------------------------------------------------------ alerts #

    def alerts(
        self,
        *,
        health: HealthReport,
        performance: PerformanceReport,
        jobs: JobsReport,
        security: SecuritySnapshot,
    ) -> tuple[AlertStatus, ...]:
        """Evaluate every declared rule against the state just assembled.

        Takes the reports rather than re-reading them, so an alert can never fire
        on a figure different from the one displayed beside it — which is the
        single most confusing thing an operational page can do.
        """
        by_key = {rule.key: rule for rule in ALERT_RULES}
        statuses: list[AlertStatus] = []

        dependency_rules = {
            "postgres": "database_unavailable",
            "redis": "cache_unavailable",
            "minio": "storage_unavailable",
            "qdrant": "vector_unavailable",
        }
        for check in health.dependencies:
            key = dependency_rules.get(check.name)
            if key is None or key not in by_key:
                continue
            statuses.append(
                AlertStatus(
                    rule=by_key[key],
                    firing=check.state is HealthState.UNHEALTHY,
                    detail=check.detail,
                )
            )

        # Rate alerts require a floor of observations. Three requests of which one
        # failed is a 33 % error rate and means nothing; without the floor every
        # deployment pages somebody the first time a probe races a restart.
        enough = performance.requests_total >= settings.MONITORING_ALERT_MIN_SAMPLES
        statuses.append(
            AlertStatus(
                rule=by_key["error_rate_high"],
                firing=enough and performance.error_rate > settings.MONITORING_ERROR_RATE_THRESHOLD,
                value=performance.error_rate,
                threshold=settings.MONITORING_ERROR_RATE_THRESHOLD,
            )
        )

        api_latency = next(
            (item for item in performance.latencies if item.name == "api_response"), None
        )
        average = api_latency.average_ms if api_latency else None
        statuses.append(
            AlertStatus(
                rule=by_key["latency_high"],
                firing=(
                    enough
                    and average is not None
                    and average > settings.MONITORING_LATENCY_THRESHOLD_MS
                ),
                value=average,
                threshold=settings.MONITORING_LATENCY_THRESHOLD_MS,
            )
        )

        deepest = max((queue.depth for queue in jobs.queues), default=0)
        statuses.append(
            AlertStatus(
                rule=by_key["queue_backlog"],
                firing=deepest > settings.MONITORING_QUEUE_BACKLOG_THRESHOLD,
                value=float(deepest),
                threshold=float(settings.MONITORING_QUEUE_BACKLOG_THRESHOLD),
            )
        )

        stopped = [pool.name for pool in jobs.workers if pool.state is HealthState.UNHEALTHY]
        statuses.append(
            AlertStatus(
                rule=by_key["background_workers_stopped"],
                firing=bool(stopped),
                value=float(len(stopped)),
                detail=", ".join(stopped) or None,
            )
        )

        failures = security.recent_rates.get("login_failed", {}).get("15m", 0)
        statuses.append(
            AlertStatus(
                rule=by_key["authentication_failures_high"],
                firing=failures > settings.MONITORING_LOGIN_FAILURE_THRESHOLD,
                value=float(failures),
                threshold=float(settings.MONITORING_LOGIN_FAILURE_THRESHOLD),
            )
        )

        return tuple(statuses)

    # ---------------------------------------------------------------- overview #

    def overview(
        self,
        dependencies: dict[str, ProbeResult],
        *,
        engine: Any | None = None,
        trace_limit: int = 10,
    ) -> MonitoringOverview:
        """Assemble every section into one response.

        **Each section is assembled inside its own ``try``**, exactly as a
        dashboard widget is: a failure marks that section unavailable, is logged
        with its traceback server-side, and the response is still a 200. A
        monitoring page that answered 500 because one of its eight parts could not
        be read would be the least useful thing to have during an incident.
        """
        unavailable: list[str] = []

        health = self._section("health", lambda: self.health(dependencies), unavailable)
        snapshot = self._section(
            "metrics", lambda: self.metrics_snapshot(engine=engine), unavailable
        )
        performance = self._section(
            "performance", lambda: self.performance(snapshot), unavailable
        )
        jobs = self._section("jobs", self.jobs, unavailable)
        errors = self._section("errors", self._errors.snapshot, unavailable)
        security = self._section("security", self._security.snapshot, unavailable)
        traces = self._section(
            "traces", lambda: self._tracer.snapshot(limit=trace_limit), unavailable
        )

        if health is None or performance is None or jobs is None:
            # The three the alert evaluation needs. Without them there is nothing
            # to evaluate against, and reporting every rule as "not firing" would
            # be a claim the platform cannot support.
            alerts: tuple[AlertStatus, ...] = ()
        else:
            alerts = self._section(
                "alerts",
                lambda: self.alerts(
                    health=health,
                    performance=performance,
                    jobs=jobs,
                    security=security or self._security.snapshot(),
                ),
                unavailable,
            ) or ()

        return MonitoringOverview(
            generated_at=datetime.now(UTC),
            health=health or self.health({}),
            performance=performance or PerformanceReport(
                since=datetime.now(UTC),
                latencies=(),
                requests_total=0.0,
                requests_by_status={},
                error_rate=0.0,
                slowest_routes=(),
            ),
            jobs=jobs or JobsReport(queues=(), workers=(), depths_unavailable=True),
            errors=errors or self._errors.snapshot(),
            security=security or self._security.snapshot(),
            traces=traces or self._tracer.snapshot(),
            alerts=alerts,
            unavailable=tuple(unavailable),
        )

    @staticmethod
    def _section[T](name: str, loader: Any, unavailable: list[str]) -> T | None:
        """Run one section's loader, recording rather than raising on failure."""
        try:
            return loader()  # type: ignore[no-any-return]
        except Exception:
            logger.exception("monitoring_section_unavailable", section=name)
            unavailable.append(name)
            return None


# --------------------------------------------------------------------------- #
# Bridging the eleven feature recorders
# --------------------------------------------------------------------------- #

#: The recorders whose snapshots this service reads, by the name they are reported
#: under.
#:
#: ``realtime`` is deliberately absent — see the module docstring. Its snapshot
#: requires the live connection count, which only the WebSocket manager holds, and
#: importing that here would break the one boundary `code-standards.md` states
#: about this platform's event channel.
_FEATURE_RECORDERS: tuple[tuple[str, str, str], ...] = (
    ("search", "services.search_metrics", "get_search_metrics"),
    ("rag", "services.rag_metrics", "get_rag_metrics"),
    ("assistant", "services.assistant_metrics", "get_assistant_metrics"),
    ("notifications", "services.notification_metrics", "get_notification_metrics"),
    ("email", "services.email_metrics", "get_email_metrics"),
    ("whatsapp", "services.whatsapp_metrics", "get_whatsapp_metrics"),
    ("dashboard", "services.dashboard_metrics", "get_dashboard_metrics"),
    ("settings", "services.settings_metrics", "get_settings_metrics"),
    ("localization", "services.localization_metrics", "get_localization_metrics"),
)


def feature_metrics() -> dict[str, dict[str, float]]:
    """Read every feature recorder's numeric figures. Never raises.

    **Reflective rather than hand-mapped**, and that is what makes
    ``22-monitoring.md``'s *"support future metrics without redesign"* true here:
    a figure added to :class:`~services.rag_metrics.RagMetricsSnapshot` next month
    appears on this page and in the exposition endpoint with no change to either.
    A hand-written mapping would be nine lists to keep in step with nine
    dataclasses, and the day one of them drifted is the day a number silently
    stopped being reported.

    Numeric fields and numeric **properties** both, because the derived figures —
    ``success_rate``, ``grounding_rate``, ``delivery_rate`` — are exactly the ones
    an operator reads first, and each is a property on its snapshot. Dictionaries
    (``failures_by_code`` and its siblings) are skipped: they are breakdowns whose
    keys are unbounded by design, and turning one into a labelled series is how a
    metric registry acquires a cardinality problem.
    """
    import importlib

    collected: dict[str, dict[str, float]] = {}
    for feature, module_name, getter_name in _FEATURE_RECORDERS:
        try:
            module = importlib.import_module(module_name)
            snapshot = getattr(module, getter_name)().snapshot()
            figures = _numeric_fields(snapshot)
            if figures:
                collected[feature] = figures
        except Exception:  # pragma: no cover - defensive
            logger.debug("feature_recorder_unavailable", feature=feature)
            continue
    return collected


def _numeric_fields(snapshot: object) -> dict[str, float]:
    """Extract every numeric field and property from a snapshot dataclass."""
    figures: dict[str, float] = {}

    if dataclasses.is_dataclass(snapshot) and not isinstance(snapshot, type):
        for field in dataclasses.fields(snapshot):
            value = getattr(snapshot, field.name, None)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                figures[field.name] = float(value)

    for name, attribute in type(snapshot).__dict__.items():
        if not isinstance(attribute, property) or name.startswith("_"):
            continue
        try:
            value = getattr(snapshot, name)
        except Exception:  # pragma: no cover - a property that raises is skipped
            continue
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            figures[name] = float(value)

    return figures
