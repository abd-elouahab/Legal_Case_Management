"""Shared dependency readiness probing.

Defines the single registry of downstream dependency connectivity checks and a
helper that runs them concurrently off the event loop. Used by both the
readiness endpoint and the startup lifespan so the probe set never diverges.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import anyio

from core.cache import check_redis_connection
from core.storage import check_minio_connection
from core.vector import check_qdrant_connection
from db.session import check_database_connection

# Dependency name -> synchronous, blocking connectivity probe.
DEPENDENCY_CHECKS: dict[str, Callable[[], None]] = {
    "postgres": check_database_connection,
    "redis": check_redis_connection,
    "minio": check_minio_connection,
    "qdrant": check_qdrant_connection,
}


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single dependency probe."""

    healthy: bool
    error: str | None = None


async def probe_dependencies() -> dict[str, ProbeResult]:
    """Run every dependency check concurrently in worker threads.

    Total latency approximates the slowest single probe rather than their sum.
    """
    results: dict[str, ProbeResult] = {}

    async def _run(name: str, check: Callable[[], None]) -> None:
        try:
            await anyio.to_thread.run_sync(check)
            results[name] = ProbeResult(healthy=True)
        except Exception as exc:  # surface any failure as an unhealthy dependency
            results[name] = ProbeResult(healthy=False, error=str(exc))

    async with anyio.create_task_group() as task_group:
        for name, check in DEPENDENCY_CHECKS.items():
            task_group.start_soon(_run, name, check)

    return results
