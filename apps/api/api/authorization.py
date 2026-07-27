"""Reusable authorization dependencies.

The bridge between :class:`~services.authorization.AuthorizationService` and
FastAPI. Every protected endpoint in the platform — present and future — is
guarded by one of the factories below rather than by an inline role check:

.. code-block:: python

    @router.get("/cases", dependencies=[Depends(require_permission(Permission.CASES_VIEW))])
    def list_cases() -> list[CaseRead]: ...

    # …or bind the authorized user directly:
    def create_case(user: Annotated[User, Depends(require_permission(Permission.CASES_CREATE))]): ...

Each factory returns a dependency, which is the FastAPI equivalent of a
permission decorator: it composes with the rest of the dependency graph, is
visible in the OpenAPI schema, and can be applied to a single route, a router,
or the whole application.

Status codes follow from the dependency order, which is not accidental:
``CurrentUser`` resolves first and raises **401** when no valid token is
presented, so an anonymous caller never reaches the permission check that would
return **403**. Authenticated-but-unauthorized is the only path to a 403.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends

from api.deps import CurrentUser
from core.permissions import Permission
from core.roles import UserRole
from models.user import User
from services.authorization import AuthorizationService


def get_authorization_service() -> AuthorizationService:
    """Provide the authorization service.

    Stateless, so a fresh instance per request costs nothing and keeps the
    dependency overridable in tests.
    """
    return AuthorizationService()


AuthorizationServiceDep = Annotated[AuthorizationService, Depends(get_authorization_service)]

#: A dependency that resolves to the authorized :class:`~models.user.User`.
AuthorizationDependency = Callable[[User, AuthorizationService], User]


def get_current_permissions(
    current_user: CurrentUser,
    authorization: AuthorizationServiceDep,
) -> frozenset[Permission]:
    """The effective permissions of the authenticated caller."""
    return authorization.permissions_for(current_user)


CurrentPermissions = Annotated[frozenset[Permission], Depends(get_current_permissions)]


def require_role(*roles: UserRole) -> AuthorizationDependency:
    """Require the caller to hold one of ``roles``.

    Prefer :func:`require_permission` when a capability describes the rule —
    roles change, capabilities are stable.

    Returns:
        A dependency yielding the authorized user (401 unauthenticated,
        403 unauthorized).
    """

    def dependency(current_user: CurrentUser, authorization: AuthorizationServiceDep) -> User:
        authorization.require_role(current_user, roles)
        return current_user

    return dependency


def require_permission(permission: Permission) -> AuthorizationDependency:
    """Require the caller to hold ``permission``.

    Returns:
        A dependency yielding the authorized user (401 unauthenticated,
        403 unauthorized).
    """

    def dependency(current_user: CurrentUser, authorization: AuthorizationServiceDep) -> User:
        authorization.require_permission(current_user, permission)
        return current_user

    return dependency


def require_any_permission(*permissions: Permission) -> AuthorizationDependency:
    """Require the caller to hold **at least one** of ``permissions``.

    For endpoints reachable through more than one capability — a case timeline
    readable via ``timeline:view`` or ``cases:view``, say.

    Returns:
        A dependency yielding the authorized user (401 unauthenticated,
        403 unauthorized).
    """

    def dependency(current_user: CurrentUser, authorization: AuthorizationServiceDep) -> User:
        authorization.require_any_permission(current_user, permissions)
        return current_user

    return dependency


def require_all_permissions(*permissions: Permission) -> AuthorizationDependency:
    """Require the caller to hold **every** one of ``permissions``.

    For actions that combine capabilities — generating a report *from* case
    documents needs both ``reports:generate`` and ``documents:view``.

    Returns:
        A dependency yielding the authorized user (401 unauthenticated,
        403 unauthorized).
    """

    def dependency(current_user: CurrentUser, authorization: AuthorizationServiceDep) -> User:
        authorization.require_all_permissions(current_user, permissions)
        return current_user

    return dependency
