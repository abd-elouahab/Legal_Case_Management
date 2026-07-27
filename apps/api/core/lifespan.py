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

    yield

    logger.info("application_shutdown")
    close_redis()
    close_qdrant()
    dispose_engine()
