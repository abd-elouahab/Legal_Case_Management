"""Role definitions and the default role → permission policy.

The three platform roles are defined once, as :class:`~models.user.UserRole` (it
is persisted on the user record, so the storage enum *is* the canonical
definition). This module re-exports it so authorization code has a single,
intention-revealing import site and never spells a role name as a string.

The policy below is the only place that decides what a role may do. Enforcement
code asks :func:`permissions_for_role`; it never branches on the role itself,
which is what allows the policy to be refined by later features (case
assignment, hearings, reports) without touching a single call site.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from core.exceptions import AuthorizationConfigurationError
from core.permissions import ALL_PERMISSIONS, Permission
from models.user import UserRole

__all__ = [
    "BASE_PERMISSIONS",
    "ROLE_PERMISSIONS",
    "UserRole",
    "permissions_for_role",
    "role_from_value",
]


#: Granted to every authenticated role, whatever their specialisation.
#:
#: Notifications and settings are platform-wide rather than role-specific:
#: `architecture.md` invariant 3 says every important event produces a
#: notification for its authorized users, and `ui-context.md` shows Notifications
#: and Settings in the sidebar for all roles. Withholding these would hide the
#: user's own alerts and preferences from them, which no role description asks
#: for. Managing *other* users' notification configuration is a separate
#: permission (``notifications:manage``) and is not part of this baseline.
BASE_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.NOTIFICATIONS_VIEW,
        Permission.SETTINGS_VIEW,
    }
)


#: Lawyers work inside cases assigned to them: they read and update the case,
#: contribute documents and timeline entries, and use the AI assistant to
#: summarise and report on that material. They do not create, re-assign, or
#: delete cases, and they do not manage users.
#:
#: Note that a permission grants access to a *capability*, not to every row.
#: Lawyers deliberately do **not** hold ``cases:view-all``, so Case Management
#: scopes both ``cases:view`` and ``cases:update`` to the cases they are assigned
#: to (see :mod:`services.case_access`). That is the "only assigned cases" half
#: of the rule, and it is enforced per resource rather than by this list.
_LAWYER_PERMISSIONS: frozenset[Permission] = BASE_PERMISSIONS | {
    Permission.CASES_VIEW,
    Permission.CASES_UPDATE,
    Permission.DOCUMENTS_VIEW,
    Permission.DOCUMENTS_UPLOAD,
    Permission.OCR_VIEW,
    Permission.OCR_RETRY,
    Permission.TIMELINE_VIEW,
    Permission.TIMELINE_CREATE,
    Permission.REPORTS_VIEW,
    Permission.REPORTS_GENERATE,
    Permission.AI_CHAT,
    Permission.AI_GENERATE_REPORT,
}


#: Court representatives record what the court does: hearings, decisions, and the
#: resulting case status changes, plus the official documents behind them. They
#: get no reporting and no AI capabilities — the role descriptions in
#: `project-overview.md` and `architecture.md` assign neither.
#:
#: They hold ``cases:update-hearing`` rather than ``cases:update``, which narrows
#: them to the court-facing fields (court name, filing date, next hearing date,
#: and the resulting status change) and matches the role description exactly.
#: Case Management introduced that permission; before it existed, hearing work
#: rode on the full ``cases:update``, which also let a court representative
#: rewrite a case's title and description.
_COURT_PERMISSIONS: frozenset[Permission] = BASE_PERMISSIONS | {
    Permission.CASES_VIEW,
    Permission.CASES_UPDATE_HEARING,
    Permission.DOCUMENTS_VIEW,
    Permission.DOCUMENTS_UPLOAD,
    # Read the extracted text of the documents they can already read, but not
    # re-run extraction: a retry consumes processing capacity, and the court
    # role's description ("update hearings and decisions, upload official
    # documents, view authorized case information") does not extend to operating
    # the platform's pipeline. `documents:update` and `documents:delete` are
    # withheld from this role for the same reason.
    Permission.OCR_VIEW,
    Permission.TIMELINE_VIEW,
    Permission.TIMELINE_CREATE,
}


#: Default permissions per role.
#:
#: Read-only at runtime (``MappingProxyType`` over immutable ``frozenset``s) so a
#: bug elsewhere cannot widen someone's access by mutating the policy in place.
#: Administrators are granted :data:`~core.permissions.ALL_PERMISSIONS` by
#: reference, so a newly defined permission reaches them without an edit here.
ROLE_PERMISSIONS: Mapping[UserRole, frozenset[Permission]] = MappingProxyType(
    {
        UserRole.ADMINISTRATOR: ALL_PERMISSIONS,
        UserRole.LAWYER: _LAWYER_PERMISSIONS,
        UserRole.COURT_REPRESENTATIVE: _COURT_PERMISSIONS,
    }
)


def permissions_for_role(role: UserRole) -> frozenset[Permission]:
    """Return the permissions granted to ``role``.

    Raises:
        AuthorizationConfigurationError: the role has no policy entry. Failing
            closed with a 500 is deliberate — silently returning an empty set
            would turn a missing policy into a confusing 403, and returning a
            default set would grant access nobody configured.
    """
    try:
        return ROLE_PERMISSIONS[role]
    except KeyError as exc:
        raise AuthorizationConfigurationError(
            detail=f"Role {role!r} has no entry in ROLE_PERMISSIONS."
        ) from exc


def role_from_value(value: str) -> UserRole:
    """Resolve a role identifier, rejecting unknown ones.

    Raises:
        AuthorizationConfigurationError: ``value`` is not a defined role.
    """
    try:
        return UserRole(value)
    except ValueError as exc:
        raise AuthorizationConfigurationError(detail=f"Unknown role {value!r}.") from exc
