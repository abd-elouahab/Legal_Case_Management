"""Authorization business logic.

Answers the one question authentication deliberately leaves open: *what may this
user do?* Every access decision in the platform is made here, so no endpoint,
service, or repository re-implements a permission check.

The service is stateless and pure — it derives a user's effective permissions
from their role via :mod:`core.roles` and compares. It never touches the
database or the network, which is what makes it cheap enough to call on every
request and trivial to unit-test.

Two flavours of each check:

* ``has_*`` returns a boolean, for callers that need to *branch* (shaping a
  response, deciding whether to include a field);
* ``require_*`` raises :class:`~core.exceptions.AuthorizationError`, for callers
  that need to *stop*. The FastAPI dependencies in :mod:`api.authorization` are
  built on these.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from core.exceptions import AuthorizationConfigurationError, AuthorizationError
from core.permissions import Permission, sort_permissions
from core.roles import UserRole, permissions_for_role
from models.user import User

logger = structlog.get_logger(__name__)


class AuthorizationService:
    """Evaluates role- and permission-based access rules for a user."""

    # ------------------------------------------------------ effective grants #

    def permissions_for(self, user: User) -> frozenset[Permission]:
        """Every permission ``user`` currently holds, derived from their role."""
        return permissions_for_role(user.role)

    # ------------------------------------------------------------- predicates #

    def has_role(self, user: User, roles: Iterable[UserRole]) -> bool:
        """Whether ``user`` holds one of ``roles``."""
        allowed = self._non_empty_roles(roles)
        return user.role in allowed

    def has_permission(self, user: User, permission: Permission) -> bool:
        """Whether ``user`` holds ``permission``."""
        return permission in self.permissions_for(user)

    def has_any_permission(self, user: User, permissions: Iterable[Permission]) -> bool:
        """Whether ``user`` holds at least one of ``permissions``."""
        required = self._non_empty_permissions(permissions)
        return bool(required & self.permissions_for(user))

    def has_all_permissions(self, user: User, permissions: Iterable[Permission]) -> bool:
        """Whether ``user`` holds every one of ``permissions``."""
        required = self._non_empty_permissions(permissions)
        return required <= self.permissions_for(user)

    # -------------------------------------------------------------- guards #

    def require_role(self, user: User, roles: Iterable[UserRole]) -> None:
        """Allow only holders of one of ``roles``.

        Prefer :meth:`require_permission` wherever a capability describes the
        rule: role checks hard-code policy at the call site and have to be
        revisited whenever the role model changes.

        Raises:
            AuthorizationError: ``user`` holds none of ``roles``.
        """
        allowed = self._non_empty_roles(roles)
        if user.role not in allowed:
            self._deny(user, required=[role.value for role in sorted(allowed)], rule="role")

    def require_permission(self, user: User, permission: Permission) -> None:
        """Allow only holders of ``permission``.

        Raises:
            AuthorizationError: ``user`` does not hold ``permission``.
        """
        if permission not in self.permissions_for(user):
            self._deny(user, required=[permission.value], rule="permission")

    def require_any_permission(self, user: User, permissions: Iterable[Permission]) -> None:
        """Allow holders of at least one of ``permissions``.

        Raises:
            AuthorizationError: ``user`` holds none of them.
        """
        required = self._non_empty_permissions(permissions)
        if not required & self.permissions_for(user):
            self._deny(user, required=self._values(required), rule="any_permission")

    def require_all_permissions(self, user: User, permissions: Iterable[Permission]) -> None:
        """Allow only holders of every one of ``permissions``.

        Raises:
            AuthorizationError: ``user`` is missing at least one.
        """
        required = self._non_empty_permissions(permissions)
        missing = required - self.permissions_for(user)
        if missing:
            self._deny(user, required=self._values(missing), rule="all_permissions")

    # -------------------------------------------------------------- helpers #

    def _deny(self, user: User, *, required: list[str], rule: str) -> None:
        """Log the denial with its specifics, then fail with a generic 403.

        The log records what was required so operators can diagnose a
        misconfigured role; the response does not, so a caller cannot map the
        platform's capability model by probing endpoints. Only the user's id and
        role are logged — never their email or name.
        """
        logger.warning(
            "authorization_denied",
            user_id=str(user.id),
            role=user.role.value,
            rule=rule,
            required=required,
        )
        raise AuthorizationError

    @staticmethod
    def _values(permissions: Iterable[Permission]) -> list[str]:
        return [permission.value for permission in sort_permissions(set(permissions))]

    @staticmethod
    def _non_empty_permissions(permissions: Iterable[Permission]) -> frozenset[Permission]:
        """Materialize a requirement set, rejecting an empty one.

        An empty requirement is always a bug: ``require_all_permissions([])``
        would wave everyone through and ``require_any_permission([])`` would deny
        everyone. Both are worse than a loud failure.
        """
        required = frozenset(permissions)
        if not required:
            raise AuthorizationConfigurationError(detail="An authorization rule listed no permissions.")
        return required

    @staticmethod
    def _non_empty_roles(roles: Iterable[UserRole]) -> frozenset[UserRole]:
        """Materialize a role requirement set, rejecting an empty one."""
        allowed = frozenset(roles)
        if not allowed:
            raise AuthorizationConfigurationError(detail="An authorization rule listed no roles.")
        return allowed
