"""Application lifespan management.

Runs startup and shutdown routines: configures logging, probes downstream
dependencies (logging warnings without blocking startup so the app can report
readiness via ``/ready``), and releases client resources on shutdown.
"""

from __future__ import annotations

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
from services.indexing_worker import start_index_workers, stop_index_workers
from services.ocr_worker import start_ocr_workers, stop_ocr_workers

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

    yield

    logger.info("application_shutdown")
    # Before the engine is disposed: draining workers still need their sessions.
    # Indexing first: an OCR worker that is still finishing can schedule an
    # indexing job, so draining indexing last would leave that job unaccepted.
    stop_ocr_workers()
    stop_index_workers()
    close_redis()
    close_qdrant()
    dispose_engine()
