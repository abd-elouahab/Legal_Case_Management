"""System monitoring: the process, its worker pools, and its runtime.

``22-monitoring.md``'s System Monitoring and Background Processing Monitoring
sections ask for the figures nobody else on the platform holds: how long this
process has been up, how many threads it is running, whether each background pool
is alive, and how much work is waiting for them.

Three decisions shape this module.

**No new dependency.** ``psutil`` would give resident memory and CPU share, and
it is not in ``requirements.txt`` and is not being added: it is a compiled
dependency per platform, the figures it adds are the ones a container runtime
already reports far more accurately than a process can report about itself, and
the spec's Performance section asks for *application* measurements rather than
host ones. What is here comes from :mod:`os`, :mod:`sys`, :mod:`platform`, and
:mod:`threading`, all of which are free and always available.

**Queue depth is read from PostgreSQL, not from a thread pool.** That is
`code-standards.md`'s rule for the dashboard — *"count persisted state, not
process state"* — and it applies with more force here: a pool's internal size is
one API instance's opinion, resets on deploy, and is zero the moment before a
worker picks a job up. The number an operator needs is *how much work does the
platform owe*, and that is a ``COUNT`` over rows in a ``pending`` status, which
:class:`~repositories.dashboard.DashboardRepository` already computes for the
dashboard and which is reused here rather than rewritten.

**Pool liveness is read from the process**, and it is the one figure that must
be. *"Is the OCR pool running?"* has no answer in the database — a stopped pool
and a busy one look identical there — and a queue with nothing draining it is the
failure mode a queue-depth chart takes hours to reveal and this reveals
immediately.
"""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog

from core.config import settings
from core.observability import HealthState, MetricName, MonitoringComponent
from services.metrics_registry import MetricsRegistry

logger = structlog.get_logger(__name__)

__all__ = [
    "PROCESS_STARTED_AT",
    "SystemSnapshot",
    "WorkerPoolStatus",
    "collect_process_gauges",
    "system_snapshot",
    "worker_pools",
]

#: When this process started, fixed at import.
#:
#: At import rather than at the first request, so uptime measures the process
#: rather than the traffic — a platform that has been up for a week with nobody
#: using it should say so, and the first-request reading would say it just
#: started.
PROCESS_STARTED_AT: Final[datetime] = datetime.now(UTC)
_STARTED_MONOTONIC: Final[float] = time.perf_counter()


@dataclass(frozen=True, slots=True)
class WorkerPoolStatus:
    """Whether one background pool is alive, and how it is configured.

    Note what is **not** here: how many jobs it is currently running. A thread
    pool's active count is a number that changes between reading it and rendering
    it, and the two figures that matter — *is anything draining this queue* and
    *how much does the platform owe* — are the two that are here and in
    :class:`~repositories.dashboard.QueueDepths` respectively.
    """

    name: str
    running: bool
    #: Configured worker count for this pool. Reported so a queue that is draining
    #: slowly can be told from one that is draining with a single thread.
    concurrency: int
    #: ``DISABLED`` when the feature it belongs to is switched off, which is not a
    #: fault and must not read as one — a deployment with WhatsApp off should not
    #: have a red pool on its monitoring page.
    state: HealthState


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    """The process and its runtime, at one instant."""

    started_at: datetime
    uptime_seconds: float
    process_id: int
    thread_count: int
    python_version: str
    platform: str
    #: The deployment's identity, so a monitoring page read out of context still
    #: says which environment and which release it describes.
    environment: str
    version: str
    project_name: str

    @property
    def uptime_human(self) -> str:
        """Uptime as ``3d 4h 12m``, for a page a person reads.

        Rendered here rather than in the client for the reason every other
        derived figure on this platform is computed server-side: two clients
        rounding a duration differently is how two screens disagree about how long
        an incident has been going on.
        """
        total = int(self.uptime_seconds)
        days, remainder = divmod(total, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes = remainder // 60
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


def system_snapshot() -> SystemSnapshot:
    """Read the process's own figures. Never raises.

    Every field has a fallback, because a monitoring page that answered 500
    because ``platform.python_version()`` was unavailable would be a page nobody
    could rely on — the rule `19-dashboard-analytics.md` states for a widget,
    applied to a process description.
    """
    try:
        thread_count = threading.active_count()
    except Exception:  # pragma: no cover - defensive
        thread_count = 0

    return SystemSnapshot(
        started_at=PROCESS_STARTED_AT,
        uptime_seconds=round(time.perf_counter() - _STARTED_MONOTONIC, 1),
        process_id=os.getpid(),
        thread_count=thread_count,
        python_version=platform.python_version(),
        platform=f"{sys.platform}",
        environment=settings.ENVIRONMENT.value,
        version=settings.VERSION,
        project_name=settings.PROJECT_NAME,
    )


def worker_pools() -> tuple[WorkerPoolStatus, ...]:
    """Report every background pool this process runs.

    Imported **inside the function** rather than at module scope, deliberately:
    each worker module constructs its pool at import, and importing all five here
    would mean that anything touching monitoring — a unit test, a script,
    :mod:`core.exceptions` — created five thread pools as a side effect. A
    monitoring module that started the platform's background processing by being
    imported would be the clearest possible violation of *"monitoring must never
    become a dependency of the application"*.

    Never raises: a pool whose module cannot be imported is reported as
    ``UNKNOWN`` rather than costing the whole page.
    """
    pools: list[WorkerPoolStatus] = []

    def _add(
        name: str,
        *,
        enabled: bool,
        concurrency: int,
        probe: object,
    ) -> None:
        if not enabled:
            pools.append(
                WorkerPoolStatus(
                    name=name, running=False, concurrency=concurrency, state=HealthState.DISABLED
                )
            )
            return
        try:
            running = bool(probe())  # type: ignore[operator]
        except Exception:  # pragma: no cover - defensive
            pools.append(
                WorkerPoolStatus(
                    name=name, running=False, concurrency=concurrency, state=HealthState.UNKNOWN
                )
            )
            return
        pools.append(
            WorkerPoolStatus(
                name=name,
                running=running,
                concurrency=concurrency,
                # A pool that should be running and is not is **unhealthy**, not
                # degraded: its queue will never drain, and every job in it is
                # work the platform has accepted and will not do.
                state=HealthState.HEALTHY if running else HealthState.UNHEALTHY,
            )
        )

    try:
        from services.email_worker import email_queue
        from services.indexing_worker import index_queue
        from services.ocr_worker import ocr_queue
        from services.report_worker import report_queue
        from services.whatsapp_worker import whatsapp_queue

        _add(
            "ocr",
            enabled=settings.OCR_ENABLED,
            concurrency=settings.OCR_WORKER_CONCURRENCY,
            probe=lambda: ocr_queue.is_running,
        )
        _add(
            "indexing",
            enabled=settings.INDEXING_ENABLED,
            concurrency=settings.INDEXING_WORKER_CONCURRENCY,
            probe=lambda: index_queue.is_running,
        )
        _add(
            "reports",
            enabled=settings.REPORTS_ENABLED,
            concurrency=settings.REPORT_WORKER_CONCURRENCY,
            probe=lambda: report_queue.is_running,
        )
        _add(
            "email",
            enabled=settings.EMAIL_ENABLED,
            concurrency=settings.EMAIL_WORKER_CONCURRENCY,
            probe=lambda: email_queue.is_running,
        )
        _add(
            "whatsapp",
            enabled=settings.WHATSAPP_ENABLED,
            concurrency=settings.WHATSAPP_WORKER_CONCURRENCY,
            probe=lambda: whatsapp_queue.is_running,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("worker_pool_probe_failed")

    try:
        from services.notification_events import get_notification_subscriber

        _add(
            "notifications",
            enabled=settings.NOTIFICATIONS_ENABLED,
            concurrency=settings.NOTIFICATION_WORKER_CONCURRENCY,
            probe=lambda: get_notification_subscriber().is_running,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("notification_worker_probe_failed")

    return tuple(pools)


def collect_process_gauges(metrics: MetricsRegistry) -> None:
    """Write the process gauges into the registry. Never raises.

    Called when a snapshot is taken rather than on a timer — pull rather than
    push, for the reason :func:`~services.database_metrics.record_pool_gauges`
    gives: a gauge is meaningful at the instant it is read, and a background
    thread sampling one every few seconds is a thread the platform runs for a page
    that may never be opened.
    """
    try:
        snapshot = system_snapshot()
        metrics.set_gauge(MetricName.PROCESS_UPTIME_SECONDS, snapshot.uptime_seconds)
        metrics.set_gauge(MetricName.PROCESS_THREADS, float(snapshot.thread_count))
    except Exception:  # pragma: no cover - defensive
        logger.debug("process_gauges_unavailable", component=MonitoringComponent.MONITORING.value)
