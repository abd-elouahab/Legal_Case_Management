"""Application lifespan management.

Runs startup and shutdown routines: configures logging, probes downstream
dependencies (logging warnings without blocking startup so the app can report
readiness via ``/ready``), and releases client resources on shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from core.cache import close_redis
from core.config import settings
from core.logging import configure_logging
from core.observability import LogEvent
from core.readiness import probe_dependencies
from core.vector import close_qdrant
from db.session import dispose_engine, engine
from services.database_metrics import instrument_engine, uninstrument_engine
from services.email_worker import start_email_workers, stop_email_workers
from services.error_tracker import get_error_tracker
from services.events import get_event_dispatcher
from services.indexing_worker import start_index_workers, stop_index_workers
from services.metrics_registry import get_metrics_registry
from services.notification_events import get_notification_subscriber
from services.ocr_worker import start_ocr_workers, stop_ocr_workers
from services.report_worker import start_report_workers, stop_report_workers
from services.tracer import get_tracer
from services.whatsapp_worker import start_whatsapp_workers, stop_whatsapp_workers
from websocket.manager import get_connection_manager

logger = structlog.get_logger(__name__)


async def _log_dependency_status() -> None:
    """Probe dependencies once at startup and log their status.

    A downstream outage is logged as a warning but does not abort startup, so
    the app can come up and report readiness via ``/ready``.
    """
    for name, result in (await probe_dependencies()).items():
        if result.healthy:
            logger.info("dependency_connected", dependency=name)
        else:
            logger.warning("dependency_unavailable", dependency=name, error=result.error)


def _start_realtime() -> None:
    """Register the WebSocket manager on the dispatcher and start it.

    **This is the one place producers and consumers are joined**, and it is
    deliberately three lines in a startup routine rather than a wiring decision
    inside either half: the business services publish to a dispatcher that knows
    no consumers, the manager consumes events it did not ask any module for, and
    this function is what makes the two the same channel. A future notification,
    email, WhatsApp, or analytics consumer is one more ``subscribe`` call here.

    Never aborts startup, for the same reason the worker pools do not: an API that
    refuses to come up because a live-update channel could not be created would
    take authentication, cases, and documents down over a feature every screen is
    built to work without.
    """
    if not settings.REALTIME_ENABLED:
        logger.info("realtime_disabled")
        return

    try:
        manager = get_connection_manager()
        manager.start(asyncio.get_running_loop())
        get_event_dispatcher().subscribe(manager)
    except Exception:
        logger.exception("realtime_startup_failed")


def _stop_realtime() -> None:
    """Unsubscribe the manager and close every connection. Never raises."""
    try:
        manager = get_connection_manager()
        get_event_dispatcher().unsubscribe(manager.name)
        manager.stop()
    except Exception:  # pragma: no cover - defensive
        logger.exception("realtime_shutdown_failed")


def _start_notifications() -> None:
    """Register the notification subscriber on the dispatcher and start its workers.

    **The second consumer**, and the whole of what adding one took: three lines
    here, exactly as :func:`_start_realtime` predicted. No business module
    changed, no publisher learned anything, and nothing about the dispatcher
    moved — they hold ``EventPublisher``, which has one method and no way to ask
    who is listening.

    Registered **after** the WebSocket manager, and the order is load-bearing
    rather than cosmetic. The notification service delivers by *publishing*
    ``notification.created`` back onto the same dispatcher, so the manager has to
    be subscribed before the first notification is created — otherwise that
    announcement would go into an empty room and the recipient's badge would sit
    still until their next poll.

    Never aborts startup, for the same reason the worker pools and the channel do
    not: an API that refuses to come up because notifications could not be
    started would take authentication, cases, and documents down over a feature
    every screen works without.
    """
    if not settings.NOTIFICATIONS_ENABLED:
        logger.info("notifications_disabled")
        return

    try:
        subscriber = get_notification_subscriber()
        subscriber.start()
        get_event_dispatcher().subscribe(subscriber)
    except Exception:
        logger.exception("notifications_startup_failed")


def _stop_notifications() -> None:
    """Unsubscribe the notification consumer and drain its queue. Never raises.

    Drains rather than abandoning, unlike the WebSocket manager — and the
    difference is the difference between the two features. An undelivered *event*
    is nothing: the client reconnects and refetches. An uncreated *notification*
    is a person never being told something happened, and the row that would have
    said so does not exist anywhere else.
    """
    try:
        subscriber = get_notification_subscriber()
        get_event_dispatcher().unsubscribe(subscriber.name)
        subscriber.stop()
    except Exception:  # pragma: no cover - defensive
        logger.exception("notifications_shutdown_failed")


def _start_monitoring() -> None:
    """Attach the engine instrumentation and record that observability is up.

    **Three lines, and nothing else in the platform learns anything** — which is
    the property this feature was built to have. There is no subscriber to
    register and no consumer to join, because monitoring does not consume the
    platform's events: it attaches to the edges the platform already has (this
    lifespan, the HTTP middleware, the exception handlers, and the SQLAlchemy
    engine), and every one of those was going to run anyway.

    Never aborts startup, for the reason :func:`_start_realtime` does not — only
    more so. An API that refused to come up because it could not attach a metrics
    listener would have made monitoring a dependency of the platform, which is the
    one thing ``22-monitoring.md`` says it must never become.
    """
    if not settings.MONITORING_ENABLED:
        logger.info("monitoring_disabled")
        return

    try:
        instrumented = instrument_engine(
            engine,
            metrics=get_metrics_registry(),
            tracer=get_tracer(),
            errors=get_error_tracker(),
        )
        logger.info(
            LogEvent.MONITORING_STARTED,
            metrics=settings.MONITORING_METRICS_ENABLED,
            tracing=settings.MONITORING_TRACING_ENABLED,
            database_instrumented=instrumented,
            exporter=settings.MONITORING_PROMETHEUS_ENABLED,
            log_level=settings.LOG_LEVEL,
        )
    except Exception:
        # Degraded rather than absent: the middleware and the exception handlers
        # are still recording, and only the per-statement database timings are
        # missing. Logged as such so an operator reading a page with no query
        # latency on it knows why.
        logger.exception(LogEvent.MONITORING_DEGRADED)


def _log_configuration() -> None:
    """Record which features this deployment is running with.

    ``22-monitoring.md``'s Logging section asks for *"configuration loaded"* to be
    logged at startup, and this is the whole of it. **What it logs is which
    switches are on, never what any of them is set to** — the Logging Policy
    forbids secrets, and a startup line listing every value would be the one place
    in the platform where a credential is written out by design. The switches are
    booleans and enum members, so the line answers *"why is this deployment not
    sending email?"* without carrying anything worth protecting.
    """
    try:
        logger.info(
            LogEvent.CONFIGURATION_LOADED,
            environment=settings.ENVIRONMENT.value,
            version=settings.VERSION,
            default_language=settings.DEFAULT_LANGUAGE,
            ocr=settings.OCR_ENABLED,
            indexing=settings.INDEXING_ENABLED,
            search=settings.SEARCH_ENABLED,
            rag=settings.RAG_ENABLED,
            assistant=settings.ASSISTANT_ENABLED,
            reports=settings.REPORTS_ENABLED,
            realtime=settings.REALTIME_ENABLED,
            notifications=settings.NOTIFICATIONS_ENABLED,
            email=settings.EMAIL_ENABLED,
            whatsapp=settings.WHATSAPP_ENABLED,
            dashboard=settings.DASHBOARD_ENABLED,
            monitoring=settings.MONITORING_ENABLED,
        )
    except Exception:  # pragma: no cover - defensive
        return


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan context: startup then shutdown."""
    configure_logging()
    logger.info(
        "application_startup",
        project=settings.PROJECT_NAME,
        version=settings.VERSION,
        environment=settings.ENVIRONMENT.value,
    )
    # First, and before anything else runs: the dependency probes, the worker
    # pools, and the event channel below are all things worth having timed and
    # counted, and instrumentation attached after them would miss the startup
    # sequence — which is exactly the part of a deployment's life an operator most
    # often wants to see afterwards.
    _start_monitoring()
    _log_configuration()
    await _log_dependency_status()
    # Starts the OCR worker pool and re-queues runs left `pending` by a previous
    # process. Never aborts startup: an API that refuses to come up because a
    # background pool could not be created would take authentication, cases, and
    # documents down over a background feature.
    start_ocr_workers()
    # Same contract, same reasoning, and deliberately its own pool: indexing and
    # extraction fail differently and are sized differently, so a backlog of
    # scans must not delay every index and a slow index must not stall every
    # upload's extraction. The embedding model is *not* loaded here — it is
    # fetched on first use, so a deployment without it still starts.
    start_index_workers()
    # Same contract again, and a third pool for the third kind of failure: a
    # report is a burst of calls to a metered language model rather than CPU-bound
    # extraction or embedding, so it is sized by an API quota instead of by a core
    # count. Sharing a pool with either would make one report's wait the other
    # feature's backlog.
    start_report_workers()
    # Last, and after the three pools, because they publish: a worker that
    # completed an extraction before the manager was subscribed would have
    # published into a dispatcher with no consumers, and the client waiting on
    # that update would sit still until its next poll.
    #
    # The running loop is handed over rather than looked up later, because the
    # manager's dispatch thread — which is where authorization queries run, off
    # the loop — cannot ask for it: `get_running_loop` fails on any thread that is
    # not the loop's, which that one is not, by design.
    _start_realtime()
    # Before notifications, and the order is load-bearing in the same way the
    # channel's is: the notification service hands each created batch to the email
    # channel, which enqueues onto this pool. Starting it afterwards would mean the
    # very first notification of a process's life queued a delivery into a pool
    # that had not been created — recoverable, since the sweeper's startup pass
    # would find the row, but only after an interval nobody should have to wait.
    #
    # It also runs the **retry sweep** once, synchronously, which is the recovery
    # for deliveries a previous process left queued: their schedule lived in that
    # process's memory and nothing else would ever pick them up.
    start_email_workers()
    # The same contract and the same ordering reason as the mail pool above, and
    # deliberately its own: the Cloud API rate-limits per business phone number
    # while a relay greylists per sender, so sharing a pool would make a throttled
    # WhatsApp number slow down password-reset mail and a greylisting relay occupy
    # the threads that deliver hearing updates.
    #
    # It also runs the **retry sweep** once, synchronously, and validates the
    # provider's configuration — logging any missing setting by *name*, which is
    # how a deployment that switched the channel on and forgot a token finds out at
    # boot rather than from a metrics page nobody was watching.
    start_whatsapp_workers()
    # After the channel and after both delivery pools, because notifications
    # *deliver over all of them*: the service persists a notification, publishes
    # `notification.created` back onto the same dispatcher, and hands the batch to
    # every channel. The manager must already be subscribed or the first badge
    # would not move until its owner's next poll.
    _start_notifications()

    yield

    logger.info("application_shutdown")
    # Before the connections are closed, and this is the one shutdown step that
    # deliberately *waits*: a notification still in the queue has been decided and
    # not yet written, and unlike an event — which a reconnecting client refetches
    # past — there is nowhere else it survives.
    _stop_notifications()
    # After the notification queue has drained, and the mirror of starting before
    # it: the last notifications to be created are the last to hand a delivery to
    # this pool, so draining the pool first would strand exactly those. Draining
    # rather than cancelling, because a send stopped mid-flight leaves its row at
    # `sending` — the one state no other worker will claim until the stale reclaim
    # finds it.
    stop_email_workers()
    # Beside the mail pool and for the same reason: the last notifications to be
    # created are the last to hand a delivery to this pool, so draining it before
    # the notification queue would strand exactly those. Draining rather than
    # cancelling, because a send stopped mid-flight leaves its row at `sending` —
    # the one state no other worker will claim until the stale reclaim finds it.
    stop_whatsapp_workers()
    # First on the way out, and the mirror of being last in: connections are
    # closed before the workers that publish to them are drained, so a report
    # finishing during shutdown does not queue an event onto sockets that are
    # already going away.
    _stop_realtime()
    # Before the engine is disposed: draining workers still need their sessions.
    # Indexing first: an OCR worker that is still finishing can schedule an
    # indexing job, so draining indexing last would leave that job unaccepted.
    # Reports first: a report in flight holds a database session and is waiting on
    # a language model, and draining it after the engine is disposed would strand
    # its row at `processing` — the one state no other worker will claim.
    stop_report_workers()
    stop_ocr_workers()
    stop_index_workers()
    close_redis()
    close_qdrant()
    # Last, and after every worker has drained: their final statements are worth
    # timing too, and a shutdown is one of the few moments a slow query is both
    # likely and interesting. Removed before the engine is disposed so the
    # listeners do not outlive the object they were attached to.
    uninstrument_engine(engine)
    dispose_engine()
