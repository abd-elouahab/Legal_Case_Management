"""User management endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.user.UserService`, and shape the HTTP response. No business
logic lives here.

Every route is guarded by a ``users:*`` permission through the reusable
dependencies in :mod:`api.authorization`, so authorization is declared next to
the route and appears in the OpenAPI schema. Because ``CurrentUser`` resolves
first, an anonymous caller gets **401** and only an authenticated-but-unentitled
one gets **403**.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from api.authorization import require_permission
from api.deps import UserServiceDep
from core.permissions import Permission
from models.user import User
from schemas.errors import ErrorResponse
from schemas.user import (
    PasswordResetResponse,
    UserCreate,
    UserListQuery,
    UserPage,
    UserRead,
    UserUpdate,
)

router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
#
# Each dependency yields the *authorized* user, so an endpoint that needs to
# record who acted does not have to depend on CurrentUser as well.
# --------------------------------------------------------------------------- #

UserViewer = Annotated[User, Depends(require_permission(Permission.USERS_VIEW))]
UserCreator = Annotated[User, Depends(require_permission(Permission.USERS_CREATE))]
UserEditor = Annotated[User, Depends(require_permission(Permission.USERS_UPDATE))]
UserDeleter = Annotated[User, Depends(require_permission(Permission.USERS_DELETE))]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "The account is disabled, or lacks the required permission.",
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No user has this identifier."}
}
_CONFLICT: dict[int | str, dict[str, object]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "Another account already uses this email address.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "Request validation failed; `details` lists the offending fields.",
    }
}
_SELF_MODIFICATION: dict[int | str, dict[str, object]] = {
    status.HTTP_400_BAD_REQUEST: {
        "model": ErrorResponse,
        "description": "The caller tried to change their own role or account status.",
    }
}


@router.get(
    "",
    response_model=UserPage,
    status_code=status.HTTP_200_OK,
    summary="List users",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def list_users(
    _viewer: UserViewer,
    users: UserServiceDep,
    query: Annotated[UserListQuery, Query()],
) -> UserPage:
    """Return a page of the user directory.

    Supports **search** (case-insensitive, across first name, last name, and
    email), **filtering** by role and status, **sorting** by name, email, created
    date, or last login in either direction, and **pagination**. Filters combine,
    so `?role=lawyer&status=active&search=ben` is a valid narrowing.

    The response carries `total_records`, `page`, `page_size`, and `total_pages`,
    so pagination controls can be rendered without a second request.
    """
    result = users.list_users(query)
    return UserPage.build(
        [UserRead.model_validate(user) for user in result.users],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get(
    "/{user_id}",
    response_model=UserRead,
    status_code=status.HTTP_200_OK,
    summary="Get a user",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_user(_viewer: UserViewer, users: UserServiceDep, user_id: uuid.UUID) -> UserRead:
    """Return the complete record for one user.

    Includes profile details, role and the permissions it grants, account status,
    audit fields, and the last sign-in timestamp. The password hash is not part of
    the schema and therefore cannot be returned.
    """
    return UserRead.model_validate(users.get_user(user_id))


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_CONFLICT, **_VALIDATION},
)
def create_user(actor: UserCreator, users: UserServiceDep, payload: UserCreate) -> UserRead:
    """Provision a new account.

    The email must be unique (409 otherwise), the password is hashed with bcrypt
    before storage, and the role and status supplied are assigned as given. The
    audit fields (`created_by`, `updated_by`) are populated from the authenticated
    caller and cannot be set by the request.

    There is no self-registration: this endpoint is the only way an account comes
    into existence through the API.
    """
    return UserRead.model_validate(users.create_user(payload, actor=actor))


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    status_code=status.HTTP_200_OK,
    summary="Update a user",
    responses={
        **_SELF_MODIFICATION,
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_VALIDATION,
    },
)
def update_user(
    actor: UserEditor,
    users: UserServiceDep,
    user_id: uuid.UUID,
    payload: UserUpdate,
) -> UserRead:
    """Apply a partial update to an account.

    Accepts personal information, phone, profile image, role, and status. Only
    the fields present in the body are written, so an omitted field is left
    untouched while an explicit `null` clears an optional one.

    The password cannot be changed here — use `POST /users/{id}/reset-password`
    (administrator) or `PATCH /auth/change-password` (the user themselves), both
    of which revoke sessions. A caller may not change their **own** role or
    status, which would let them lock themselves out irreversibly.
    """
    return UserRead.model_validate(users.update_user(user_id, payload, actor=actor))


@router.delete(
    "/{user_id}",
    response_model=UserRead,
    status_code=status.HTTP_200_OK,
    summary="Deactivate a user (soft delete)",
    responses={**_SELF_MODIFICATION, **_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def deactivate_user(actor: UserDeleter, users: UserServiceDep, user_id: uuid.UUID) -> UserRead:
    """Deactivate an account.

    A **soft delete**: the row is never removed, because audit logs and future
    case assignments reference it. The account is marked `inactive`, which stops
    it authenticating, and all of its existing sessions are revoked immediately.

    Returns the updated user, so a client can refresh its view without a second
    request. Reactivation is a normal update (`PATCH` with `status: "active"`).
    A caller cannot deactivate their own account.
    """
    return UserRead.model_validate(users.deactivate_user(user_id, actor=actor))


@router.post(
    "/{user_id}/reset-password",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_200_OK,
    summary="Reset a user's password",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def reset_user_password(
    actor: UserEditor,
    users: UserServiceDep,
    user_id: uuid.UUID,
) -> PasswordResetResponse:
    """Generate a temporary password for a user and require them to change it.

    The password is generated with a cryptographic RNG, hashed before storage, and
    returned **once** in this response so the administrator can pass it on through
    a trusted channel — it is never logged and cannot be retrieved again. The
    account is flagged `must_change_password`, and every session the user
    currently holds is revoked.
    """
    result = users.reset_password(user_id, actor=actor)
    return PasswordResetResponse(
        user=UserRead.model_validate(result.user),
        temporary_password=result.temporary_password,
        message=(
            "Temporary password generated. Share it with the user through a trusted "
            "channel — it is not shown again, and they must change it before continuing."
        ),
    )
