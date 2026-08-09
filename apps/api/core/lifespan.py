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
from core.readiness import probe_dependencies
from core.vector import close_qdrant
from db.session import dispose_engine
from services.events import get_event_dispatcher
from services.indexing_worker import start_index_workers, stop_index_workers
from services.notification_events import get_notification_subscriber
from services.ocr_worker import start_ocr_workers, stop_ocr_workers
from services.report_worker import start_report_workers, stop_report_workers
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
    # After the channel, because notifications *deliver over it*: the service
    # persists a notification and then publishes `notification.created` back onto
    # the same dispatcher, so the manager must already be subscribed or the first
    # badge would not move until its owner's next poll.
    _start_notifications()

    yield

    logger.info("application_shutdown")
    # Before the connections are closed, and this is the one shutdown step that
    # deliberately *waits*: a notification still in the queue has been decided and
    # not yet written, and unlike an event — which a reconnecting client refetches
    # past — there is nowhere else it survives.
    _stop_notifications()
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
    dispose_engine()
