"""System endpoints: liveness, readiness, and version.

These are mounted at the application root (outside the versioned API) so
orchestrators and load balancers can probe them at stable paths.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from core.config import settings
from core.readiness import probe_dependencies, probe_external_services
from schemas.health import (
    DependencyCheck,
    DependencyStatus,
    ExternalServiceCheck,
    HealthResponse,
    HealthStatus,
    ReadinessResponse,
    VersionResponse,
)

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Return 200 while the process is running (does not check dependencies)."""
    return HealthResponse(status=HealthStatus.OK)


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def ready(response: Response) -> ReadinessResponse:
    """Probe every downstream dependency and report aggregate readiness.

    Returns 200 when all dependencies are reachable, otherwise 503 with a
    per-dependency breakdown.
    """
    results = await probe_dependencies()
    all_up = all(result.healthy for result in results.values())

    dependencies = {
        name: DependencyCheck(
            status=DependencyStatus.UP if result.healthy else DependencyStatus.DOWN,
            detail=result.error,
        )
        for name, result in results.items()
    }

    # Configuration only, so this adds no latency to a probe an orchestrator runs
    # every few seconds — and it never affects the verdict, for the reason
    # `ReadinessResponse` records. Wrapped, because a readiness endpoint that
    # failed while reporting on an optional integration would be the clearest
    # possible case of monitoring becoming a dependency.
    try:
        external = {
            name: ExternalServiceCheck(enabled=item.enabled, configured=item.configured)
            for name, item in probe_external_services().items()
        }
    except Exception:  # pragma: no cover - defensive
        external = {}

    response.status_code = status.HTTP_200_OK if all_up else status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status=HealthStatus.OK if all_up else HealthStatus.DEGRADED,
        dependencies=dependencies,
        external_services=external,
    )


@router.get("/version", response_model=VersionResponse, summary="Application version")
async def version() -> VersionResponse:
    """Return the application name, version, and environment."""
    return VersionResponse(
        name=settings.PROJECT_NAME,
        version=settings.VERSION,
        environment=settings.ENVIRONMENT.value,
    )
