"""Authentication request and response schemas.

Every request body is validated here before it reaches the service layer:
email format, required fields, and password length are all enforced by Pydantic
so routes stay thin and validation errors are consistent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from core.users import normalize_email
from schemas.password import MIN_PASSWORD_LENGTH, NewPassword, validate_password_bytes
from schemas.user import UserRead

#: Re-exported so existing importers (``scripts.create_user``, the test suite)
#: keep a single, stable import site for the password policy.
__all__ = [
    "MIN_PASSWORD_LENGTH",
    "ChangePasswordRequest",
    "ChangePasswordResponse",
    "LoginRequest",
    "MessageResponse",
    "NewPassword",
    "RefreshRequest",
    "TokenResponse",
]


class LoginRequest(BaseModel):
    """Credentials submitted to ``POST /auth/login``."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(description="Registered email address.")
    # Only presence is required here — rejecting a short password at login would
    # leak the platform's policy and provide no security benefit.
    password: str = Field(min_length=1, description="Account password.")

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        """Lowercase and trim so logins are case-insensitive."""
        return normalize_email(value)


class RefreshRequest(BaseModel):
    """Optional body for ``POST /auth/refresh``.

    Browser clients send nothing: the refresh token travels in an httpOnly
    cookie. Non-browser clients (scripts, mobile) may pass the token explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    refresh_token: str | None = Field(
        default=None,
        description="Refresh token. Omit to use the httpOnly cookie instead.",
    )


class ChangePasswordRequest(BaseModel):
    """Body for ``PATCH /auth/change-password``."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, description="The password currently in use.")
    new_password: NewPassword

    @field_validator("new_password")
    @classmethod
    def _check_new_password_bytes(cls, value: str) -> str:
        return validate_password_bytes(value)

    @model_validator(mode="after")
    def _require_new_password_differs(self) -> ChangePasswordRequest:
        if self.new_password == self.current_password:
            raise ValueError("New password must be different from the current password.")
        return self


class TokenResponse(BaseModel):
    """Token pair returned by ``POST /auth/login`` and ``POST /auth/refresh``.

    The refresh token is *also* set as an httpOnly cookie; browser clients
    should rely on that cookie and keep only the access token in memory.
    """

    access_token: str = Field(description="Bearer token for authenticating API requests.")
    refresh_token: str = Field(description="Token used to obtain a new access token.")
    token_type: Literal["bearer"] = Field(default="bearer", description="Authentication scheme.")
    expires_in: int = Field(description="Access token lifetime in seconds.")
    user: UserRead = Field(description="The authenticated user.")


class ChangePasswordResponse(TokenResponse):
    """Result of a successful password change.

    Changing a password revokes every existing session, so the response carries a
    **replacement token pair**: the device that made the change stays signed in,
    while every other device must authenticate again.
    """

    message: str = Field(description="Human-readable result of the operation.")
    sessions_revoked: bool = Field(
        default=True,
        description="Always true — all other sessions were invalidated.",
    )


class MessageResponse(BaseModel):
    """Simple acknowledgement for operations with no resource payload."""

    message: str = Field(description="Human-readable result of the operation.")
