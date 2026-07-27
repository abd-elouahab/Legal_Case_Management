"""Authorization response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.permissions import Permission, PermissionGroup
from core.roles import UserRole


class CurrentAuthorizationRead(BaseModel):
    """The caller's own role and effective permissions.

    The same information is embedded in every user payload (see
    :class:`~schemas.user.UserRead`); this endpoint exists so a client can
    re-check its grants without re-fetching the whole profile.
    """

    role: UserRole = Field(description="The caller's platform role.")
    permissions: list[Permission] = Field(description="Permissions granted by that role.")


class RolePermissionsRead(BaseModel):
    """One role and everything it may do."""

    role: UserRole = Field(description="Platform role identifier.")
    permissions: list[Permission] = Field(description="Permissions granted to the role.")


class PermissionRead(BaseModel):
    """One permission and the functional area it belongs to."""

    id: Permission = Field(description="Unique permission identifier (``group:action``).")
    group: PermissionGroup = Field(description="Functional area the permission belongs to.")


class RoleCatalogRead(BaseModel):
    """The full role and permission catalog.

    Static platform policy, not user data — useful for building administration
    screens and for verifying what a role can do before assigning it.
    """

    roles: list[RolePermissionsRead] = Field(description="Every platform role with its permissions.")
    permissions: list[PermissionRead] = Field(description="Every permission the platform defines.")
