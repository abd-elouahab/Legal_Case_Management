"""Health, readiness, and version response schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    """Overall status values for health/readiness responses."""

    OK = "ok"
    DEGRADED = "degraded"


class DependencyStatus(StrEnum):
    """Status of an individual downstream dependency."""

    UP = "up"
    DOWN = "down"


class HealthResponse(BaseModel):
    """Liveness response — indicates the process is running."""

    status: HealthStatus = HealthStatus.OK


class DependencyCheck(BaseModel):
    """Result of a single dependency readiness probe."""

    status: DependencyStatus
    detail: str | None = Field(default=None, description="Error detail when the dependency is down.")


class ExternalServiceCheck(BaseModel):
    """Configured state of one outward-facing integration.

    Deliberately *not* a connectivity check. ``22-monitoring.md`` asks for
    external-service readiness to be exposed *"when applicable"* while insisting
    the implementation *"avoid expensive network operations"* — so what is
    reported is whether the integration is switched on and configured, which is
    instantaneous and is the question a deployment check actually has. Whether the
    relay is up is answered by the delivery rows every send already writes.
    """

    enabled: bool
    configured: bool


class ReadinessResponse(BaseModel):
    """Readiness response — aggregates downstream dependency probes.

    The overall status is decided by the **backing services** alone. External
    services never make a deployment *un*ready: a platform with no relay
    configured serves every request it has except the ones that would have sent
    mail, and answering 503 for it would take a working deployment out of
    rotation. They are reported so a deployment check can see them, and they are
    excluded from the verdict so an orchestrator cannot act on them.
    """

    status: HealthStatus
    dependencies: dict[str, DependencyCheck]
    external_services: dict[str, ExternalServiceCheck] = Field(
        default_factory=dict,
        description="Configured state of optional integrations. Never affects `status`.",
    )


class VersionResponse(BaseModel):
    """Application version and environment metadata."""

    name: str
    version: str
    environment: str
