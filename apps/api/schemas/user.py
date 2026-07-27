"""User response schemas.

Only non-sensitive identity fields are exposed. The password hash is never part
of any schema, so it cannot leak through a response model.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from core.permissions import Permission, sort_permissions
from core.roles import permissions_for_role
from models.user import UserRole


class UserRead(BaseModel):
    """A user as returned to authenticated clients (e.g. ``GET /auth/me``)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique user identifier.")
    email: EmailStr = Field(description="Login email address.")
    full_name: str = Field(description="Display name.")
    role: UserRole = Field(description="Platform role, which determines the permissions below.")
    is_active: bool = Field(description="Whether the account is enabled.")
    last_login_at: datetime | None = Field(default=None, description="Timestamp of the previous successful login.")
    created_at: datetime = Field(description="When the account was created.")

    @computed_field(  # type: ignore[prop-decorator]
        description="Effective permissions granted by the user's role.",
    )
    @property
    def permissions(self) -> list[Permission]:
        """The caller's effective permissions.

        Derived from the role rather than stored, so the payload cannot drift
        from the policy in :mod:`core.roles`. Every response carrying a user —
        login, refresh, ``/auth/me``, password change — therefore exposes the
        current role *and* its permissions, which is what lets the frontend hide
        inaccessible navigation without a second round trip.
        """
        return sort_permissions(permissions_for_role(self.role))
