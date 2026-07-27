"""Standardized error response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """A single field-level or contextual error detail."""

    field: str | None = Field(default=None, description="Name of the offending field, if applicable.")
    message: str = Field(description="Human-readable description of the error.")


class ErrorResponse(BaseModel):
    """Consistent error envelope returned by every exception handler."""

    error: str = Field(description="Machine-readable error code.")
    message: str = Field(description="Human-readable error message.")
    request_id: str | None = Field(default=None, description="Correlation id for this request.")
    details: list[ErrorDetail] = Field(default_factory=list, description="Optional per-field details.")
