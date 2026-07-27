"""Authorization endpoints.

Two read-only routes that expose the RBAC model itself. No business resources
live here — those arrive with their own features and guard themselves using the
dependencies in :mod:`api.authorization`.

Together they demonstrate the contract every future endpoint inherits: **401**
when no valid token is presented, **403** when a valid token lacks the required
permission.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from api.authorization import CurrentPermissions, require_permission
from api.deps import CurrentUser
from core.permissions import ALL_PERMISSIONS, Permission, sort_permissions
from core.roles import ROLE_PERMISSIONS
from schemas.authorization import (
    CurrentAuthorizationRead,
    PermissionRead,
    RoleCatalogRead,
    RolePermissionsRead,
)
from schemas.errors import ErrorResponse

router = APIRouter()

_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "Missing, invalid, or expired token."}
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "The account is disabled, or lacks the required permission.",
    }
}


@router.get(
    "/me",
    response_model=CurrentAuthorizationRead,
    status_code=status.HTTP_200_OK,
    summary="Get the caller's role and effective permissions",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_current_authorization(
    current_user: CurrentUser,
    permissions: CurrentPermissions,
) -> CurrentAuthorizationRead:
    """Return what the authenticated caller is allowed to do.

    Available to every authenticated user: it only ever describes the caller's
    own grants, so it reveals nothing about the platform they could not already
    determine by using it.
    """
    return CurrentAuthorizationRead(
        role=current_user.role,
        permissions=sort_permissions(permissions),
    )


@router.get(
    "/roles",
    response_model=RoleCatalogRead,
    status_code=status.HTTP_200_OK,
    summary="Get the role and permission catalog",
    # Reading the platform's access model is an administrative capability, so it
    # rides on the same permission that gates seeing user accounts.
    dependencies=[Depends(require_permission(Permission.USERS_VIEW))],
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_role_catalog() -> RoleCatalogRead:
    """Return every role with its permissions, plus the full permission list."""
    return RoleCatalogRead(
        roles=[
            RolePermissionsRead(role=role, permissions=sort_permissions(permissions))
            for role, permissions in ROLE_PERMISSIONS.items()
        ],
        permissions=[
            PermissionRead(id=permission, group=permission.group)
            for permission in sort_permissions(ALL_PERMISSIONS)
        ],
    )
