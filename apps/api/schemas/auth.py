"""Authentication request and response schemas.

Every request body is validated here before it reaches the service layer:
email format, required fields, and password length are all enforced by Pydantic
so routes stay thin and validation errors are consistent.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from core.security import BCRYPT_MAX_PASSWORD_BYTES
from schemas.user import UserRead

#: Minimum password length. Applies to new passwords; login only checks presence
#: so that an existing (possibly shorter, legacy) password can still sign in.
MIN_PASSWORD_LENGTH = 8


def _validate_password_bytes(value: str) -> str:
    """Reject passwords bcrypt cannot hash without silently truncating them.

    ``max_length`` counts characters, but bcrypt's limit is 72 *bytes*, so a
    short string of multi-byte characters can still exceed it.
    """
    if len(value.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must not exceed {BCRYPT_MAX_PASSWORD_BYTES} bytes when UTF-8 encoded.")
    return value


#: A password being *set* — length-checked at both ends.
NewPassword = Annotated[
    str,
    Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description=f"At least {MIN_PASSWORD_LENGTH} characters.",
    ),
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
        return value.strip().lower()


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
        return _validate_password_bytes(value)

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
