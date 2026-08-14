"""Monitoring endpoints.

Routes are deliberately thin: they authorize, delegate to
:class:`~services.monitoring.MonitoringService`, and shape the response. No
business logic lives here — and **no route here writes anything**, which is the
property that makes this module safe to reason about: monitoring owns no table, so
there is nothing it could corrupt, and every endpoint is a ``GET``.

**Nine reads, and the shape of the set is the spec's own.**
``22-monitoring.md``'s Goals list ten things; ``/monitoring/overview`` answers the
first-screen question and the eight below it answer one each, so a client that
wants everything makes one request and a client refreshing one panel makes one
small one. That is the same *"a unit of work is a widget, never a page"* rule
:mod:`api.v1.dashboard.router` follows, and for the same reason: the aggregate
loops over the very loaders the narrow endpoints call, so the two can never
disagree.

**Everything here requires ``monitoring:view``, held by administrators alone.**
``22-monitoring.md``: *"regular users must never access monitoring endpoints or
operational metrics."* There is no per-resource policy and no scoping, because
there is nothing to scope — an uptime and a p95 belong to the platform, not to
anybody's cases. The one exception is ``/monitoring/export``, which requires
``monitoring:export`` instead so a deployment can give a scraper a credential that
reads counters and cannot read the security feed, the error list, or the traces.

**The liveness and readiness probes are deliberately not here.** They live at
``/health`` and ``/ready``, outside the versioned API and outside authentication,
because an orchestrator has no credentials and a load balancer will not learn to
authenticate. What is here is the *operator's* view of the same information, with
the detail a probe must not carry.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, Response, status

from api.authorization import require_permission
from api.deps import MonitoringServiceDep
from core.config import settings
from core.permissions import Permission
from core.readiness import probe_dependencies
from db.session import engine
from models.user import User
from schemas.monitoring import (
    AlertsRead,
    ErrorsRead,
    HealthReportRead,
    JobsRead,
    MetricsRead,
    MonitoringOverviewRead,
    PerformanceRead,
    SecurityRead,
    TracesRead,
)
from services.metrics_export import PROMETHEUS_CONTENT_TYPE, render_prometheus
from services.monitoring import feature_metrics

logger = structlog.get_logger(__name__)

#: Mounted under ``/monitoring``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

MonitoringViewer = Annotated[User, Depends(require_permission(Permission.MONITORING_VIEW))]
MetricsScraper = Annotated[User, Depends(require_permission(Permission.MONITORING_EXPORT))]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "Missing, invalid, or expired access token."}
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": "The account is disabled or lacks the required permission."
    }
}

#: How many rows a list endpoint returns.
PageLimit = Annotated[
    int,
    Query(ge=1, le=settings.MONITORING_MAX_PAGE_SIZE, description="Rows to return."),
]


# --------------------------------------------------------------------------- #
# The overview
# --------------------------------------------------------------------------- #


@router.get(
    "/overview",
    response_model=MonitoringOverviewRead,
    status_code=status.HTTP_200_OK,
    summary="The platform's operational state, in one response",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
async def read_overview(
    actor: MonitoringViewer,
    monitoring: MonitoringServiceDep,
) -> MonitoringOverviewRead:
    """Assemble health, performance, jobs, errors, security, traces, and alerts.

    **Answers 200 even when parts of it could not be assembled.** Each section is
    loaded inside its own ``try`` and a failure marks it in ``unavailable``
    rather than failing the response — the rule ``19-dashboard-analytics.md``
    states for a widget, and it matters more here than there: the moment this page
    is most needed is the moment some of what it reads from is broken.

    The dependency probes run concurrently off the event loop, so the whole
    response costs roughly the slowest single probe rather than their sum.
    """
    dependencies = await probe_dependencies()
    overview = monitoring.overview(dependencies, engine=engine)
    logger.debug(
        "monitoring_overview_read",
        actor_id=str(actor.id),
        state=overview.health.state.value,
        firing_alerts=len(overview.firing_alerts),
    )
    return MonitoringOverviewRead.from_overview(overview)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


@router.get(
    "/health",
    response_model=HealthReportRead,
    status_code=status.HTTP_200_OK,
    summary="Dependency, external-service, and worker health",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
async def read_health(
    actor: MonitoringViewer,
    monitoring: MonitoringServiceDep,
) -> HealthReportRead:
    """Report every dependency, integration, and background pool.

    **Always 200, whatever it finds** — unlike ``/ready``, which answers 503 when
    a dependency is down because an orchestrator reads a status line and nothing
    else. This is read by a person, and a page that returns an error when the
    thing it describes is unhealthy is a page that goes blank exactly when it
    matters.

    Carries detail ``/ready`` deliberately does not: which dependency is required,
    which external services are configured, and which setting is missing where one
    is. That is why this one is authorized and that one is not.
    """
    dependencies = await probe_dependencies()
    report = monitoring.health(dependencies)
    logger.debug("monitoring_health_read", actor_id=str(actor.id), state=report.state.value)
    return HealthReportRead.from_report(report)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


@router.get(
    "/metrics",
    response_model=MetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Every metric series, and the eleven features' own figures",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_metrics(actor: MonitoringViewer, monitoring: MonitoringServiceDep) -> MetricsRead:
    """Serve the metric registry as JSON, for a dashboard rather than a scraper.

    The same numbers ``/monitoring/export`` renders in Prometheus's text format,
    in the shape a browser can use directly. Both are renderings of one snapshot —
    there is no second collection path, so the two can never disagree about a
    figure.
    """
    snapshot = monitoring.metrics_snapshot(engine=engine)
    logger.debug("monitoring_metrics_read", actor_id=str(actor.id), series=len(snapshot.series))
    return MetricsRead.from_snapshot(snapshot, features=feature_metrics())


@router.get(
    "/export",
    status_code=status.HTTP_200_OK,
    summary="Metrics in the Prometheus text exposition format",
    response_class=Response,
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        status.HTTP_200_OK: {
            "content": {"text/plain": {}},
            "description": "Metric families in the text exposition format.",
        },
        status.HTTP_404_NOT_FOUND: {"description": "Metrics export is disabled on this deployment."},
    },
)
def export_metrics(actor: MetricsScraper, monitoring: MonitoringServiceDep) -> Response:
    """Render the metric registry for a scraper.

    Gated on ``monitoring:export`` rather than ``monitoring:view``, so the
    credential a deployment gives Prometheus can read counters and nothing else —
    see :attr:`~core.permissions.Permission.MONITORING_EXPORT`.

    **404 rather than 403 when the exporter is switched off**, deliberately: the
    endpoint does not exist on that deployment, and answering 403 would tell a
    scraper to keep trying with better credentials when no credential would help.
    """
    if not settings.MONITORING_PROMETHEUS_ENABLED:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    snapshot = monitoring.metrics_snapshot(engine=engine)
    body = render_prometheus(snapshot, prefix=settings.MONITORING_PROMETHEUS_PREFIX)
    logger.debug("monitoring_metrics_exported", actor_id=str(actor.id), series=len(snapshot.series))
    return Response(content=body, media_type=PROMETHEUS_CONTENT_TYPE)


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #


@router.get(
    "/performance",
    response_model=PerformanceRead,
    status_code=status.HTTP_200_OK,
    summary="Latency, throughput, and the error rate",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_performance(
    actor: MonitoringViewer, monitoring: MonitoringServiceDep
) -> PerformanceRead:
    """Report the seven measurements ``22-monitoring.md``'s Performance section names.

    Five of them were already being taken by the features that own the work — OCR,
    indexing, search, the AI pipeline, and report generation — and are read from
    their recorders rather than measured a second time. Two are this feature's:
    API response time and database query duration, which belonged to no feature.
    """
    report = monitoring.performance()
    logger.debug("monitoring_performance_read", actor_id=str(actor.id))
    return PerformanceRead.from_report(report)


# --------------------------------------------------------------------------- #
# Background jobs
# --------------------------------------------------------------------------- #


@router.get(
    "/jobs",
    response_model=JobsRead,
    status_code=status.HTTP_200_OK,
    summary="Background queue depths and worker pool liveness",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_jobs(actor: MonitoringViewer, monitoring: MonitoringServiceDep) -> JobsRead:
    """Report what the platform owes, and whether anything is draining it.

    Depths are counted from **persisted rows** rather than from a thread pool, so
    they survive a restart and are the same figure the dashboard reports.
    Liveness is read from the process, because it is the one thing the database
    cannot answer — a stopped pool and a busy one look identical there, and a
    queue with nothing draining it is the failure a depth chart takes hours to
    reveal.
    """
    report = monitoring.jobs()
    logger.debug("monitoring_jobs_read", actor_id=str(actor.id), depth=report.total_depth)
    return JobsRead.from_report(report)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


@router.get(
    "/errors",
    response_model=ErrorsRead,
    status_code=status.HTTP_200_OK,
    summary="Tracked failures, grouped by type and location",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_errors(
    actor: MonitoringViewer,
    monitoring: MonitoringServiceDep,
    limit: PageLimit = settings.MONITORING_PAGE_SIZE,
) -> ErrorsRead:
    """Report what is failing, how often, and since when.

    **Groups rather than occurrences**: a list of every exception would be a log
    with extra steps, and the question an operator has is *"what is broken and is
    it getting worse?"* Each group carries a bounded, redacted sample message and
    the trace identifier of its most recent occurrence — never a traceback, which
    is in the log beside the request that produced it.
    """
    snapshot = monitoring.errors()
    logger.debug("monitoring_errors_read", actor_id=str(actor.id), total=snapshot.total_errors)
    return ErrorsRead.from_snapshot(snapshot, limit=limit)


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


@router.get(
    "/security",
    response_model=SecurityRead,
    status_code=status.HTTP_200_OK,
    summary="Failed sign-ins, denials, invalid tokens, and rate limits",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_security(
    actor: MonitoringViewer,
    monitoring: MonitoringServiceDep,
    limit: PageLimit = settings.MONITORING_PAGE_SIZE,
) -> SecurityRead:
    """Report the five things the spec's Security Monitoring section names.

    **No account is named and no address is stored.** A source is a salted digest
    whose only readable property is its cardinality, so *"forty-one failures from
    three sources"* is available and *"forty-one failures from 203.0.113.7"* is
    not — see :mod:`services.security_monitor` for why that trade is the right way
    round.
    """
    snapshot = monitoring.security()
    logger.debug(
        "monitoring_security_read", actor_id=str(actor.id), events=snapshot.total_events
    )
    return SecurityRead.from_snapshot(snapshot, limit=limit)


# --------------------------------------------------------------------------- #
# Traces
# --------------------------------------------------------------------------- #


@router.get(
    "/traces",
    response_model=TracesRead,
    status_code=status.HTTP_200_OK,
    summary="Recent request traces, most recent first",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_traces(
    actor: MonitoringViewer,
    monitoring: MonitoringServiceDep,
    limit: PageLimit = settings.MONITORING_PAGE_SIZE,
) -> TracesRead:
    """Answer *"that request took nine seconds — where did it go?"*.

    A bounded ring of the most recent traces, each a root span and the spans
    inside it. Deliberately small: keeping everything would make a tracer a leak
    proportional to traffic, and a week of traces needs the backend this feature
    prepares for rather than the buffer it ships.
    """
    snapshot = monitoring.traces(limit=limit)
    logger.debug("monitoring_traces_read", actor_id=str(actor.id), traces=len(snapshot.recent))
    return TracesRead.from_snapshot(snapshot)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


@router.get(
    "/alerts",
    response_model=AlertsRead,
    status_code=status.HTTP_200_OK,
    summary="Declared alert conditions, evaluated",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
async def read_alerts(actor: MonitoringViewer, monitoring: MonitoringServiceDep) -> AlertsRead:
    """Evaluate every rule in :data:`~core.observability.ALERT_RULES`.

    **Evaluated, never delivered.** ``22-monitoring.md`` puts alert delivery out
    of scope and asks only that the infrastructure be prepared for it; this
    endpoint is that preparation, and an Alertmanager, a cron job, or a person
    reading it is what turns a firing rule into somebody's phone ringing.

    Each rule reports the measurement that decided it and the threshold it was
    compared against, because *"error rate is high"* is not actionable and *"7.2 %
    against a 5 % threshold"* is.
    """
    dependencies = await probe_dependencies()
    health = monitoring.health(dependencies)
    performance = monitoring.performance()
    jobs = monitoring.jobs()
    statuses = monitoring.alerts(
        health=health,
        performance=performance,
        jobs=jobs,
        security=monitoring.security(),
    )
    logger.debug(
        "monitoring_alerts_read",
        actor_id=str(actor.id),
        firing=sum(1 for item in statuses if item.firing),
    )
    return AlertsRead.from_statuses(statuses)
