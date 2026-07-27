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


class ReadinessResponse(BaseModel):
    """Readiness response — aggregates downstream dependency probes."""

    status: HealthStatus
    dependencies: dict[str, DependencyCheck]


class VersionResponse(BaseModel):
    """Application version and environment metadata."""

    name: str
    version: str
    environment: str
