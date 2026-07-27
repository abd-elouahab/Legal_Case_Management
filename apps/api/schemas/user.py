"""User response schemas.

Only non-sensitive identity fields are exposed. The password hash is never part
of any schema, so it cannot leak through a response model.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from models.user import UserRole


class UserRead(BaseModel):
    """A user as returned to authenticated clients (e.g. ``GET /auth/me``)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique user identifier.")
    email: EmailStr = Field(description="Login email address.")
    full_name: str = Field(description="Display name.")
    role: UserRole = Field(description="Platform role (identity metadata; not a permission grant).")
    is_active: bool = Field(description="Whether the account is enabled.")
    last_login_at: datetime | None = Field(default=None, description="Timestamp of the previous successful login.")
    created_at: datetime = Field(description="When the account was created.")
