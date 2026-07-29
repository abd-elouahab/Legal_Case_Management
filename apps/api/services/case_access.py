"""Per-resource authorization for cases.

RBAC answers "may this user use the case-viewing capability?". It cannot answer
"may this user see *this* case", because that depends on data — and
`code-standards.md` requires both: *"Lawyers may only access assigned cases.
Court representatives may only access authorized cases. Verify case ownership or
assignment before exposing resources."*

This module is the second half. It sits on top of
:class:`~services.authorization.AuthorizationService` and never replaces it: a
caller must first hold the capability, and only then is the row checked.

Two rules, both expressed as *capabilities* rather than as role names, so a
future role is admitted by editing policy rather than code:

* **Scope** — a caller holding ``cases:view-all`` sees every case. Everyone else
  sees only the cases they are assigned to, as lawyer or as court representative.
* **Field-level write access** — ``cases:update`` covers the whole case;
  ``cases:update-hearing`` covers only the court-facing fields; ``cases:assign``
  covers the two assignment fields. A caller may write exactly the fields their
  permissions reach, which is what turns the spec's "update hearing-related
  information where permitted" into an enforceable rule.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from types import MappingProxyType

import structlog

from core.exceptions import CaseAccessDeniedError
from core.permissions import Permission
from models.case import Case
from models.user import User
from services.authorization import AuthorizationService

logger = structlog.get_logger(__name__)

#: Fields recording what the court is doing, writable with either the narrow
#: ``cases:update-hearing`` or the full ``cases:update``.
#:
#: ``status`` belongs here because `architecture.md` gives court representatives
#: "trigger case status updates" — a hearing outcome is precisely what moves a
#: case between *waiting for hearing*, *in progress*, and *closed*.
HEARING_FIELDS: frozenset[str] = frozenset(
    {"court_name", "filing_date", "next_hearing_date", "status"}
)

#: Fields that decide who works on the case.
ASSIGNMENT_FIELDS: frozenset[str] = frozenset(
    {"assigned_lawyer_id", "assigned_court_representative_id"}
)

#: Field → the permissions that allow writing it, any one of which suffices.
#:
#: A field absent from this map requires plain ``cases:update``; the mapping only
#: records the *exceptions*, so adding an ordinary field to ``CaseUpdate`` cannot
#: accidentally arrive ungoverned.
FIELD_PERMISSIONS: Mapping[str, frozenset[Permission]] = MappingProxyType(
    {
        **{
            field: frozenset({Permission.CASES_UPDATE, Permission.CASES_UPDATE_HEARING})
            for field in HEARING_FIELDS
        },
        **{field: frozenset({Permission.CASES_ASSIGN}) for field in ASSIGNMENT_FIELDS},
    }
)


class CaseAccessPolicy:
    """Decides which cases a user may reach, and which of their fields they may write.

    Stateless and pure — it reads the caller's role-derived permissions and the
    case's assignment columns, and touches neither the database nor the network.
    """

    def __init__(self, authorization: AuthorizationService | None = None) -> None:
        self._authorization = authorization or AuthorizationService()

    # --------------------------------------------------------------- scope #

    def sees_all_cases(self, user: User) -> bool:
        """Whether ``user`` reads every case rather than only their own."""
        return self._authorization.has_permission(user, Permission.CASES_VIEW_ALL)

    def visibility_scope(self, user: User) -> uuid.UUID | None:
        """The user id the case list must be restricted to, or ``None`` for no limit.

        Returned rather than applied here so the restriction can be pushed into
        the SQL query: filtering in Python would mean fetching the whole caseload
        to hide most of it, and would make the page totals count cases the caller
        is not entitled to know about.
        """
        return None if self.sees_all_cases(user) else user.id

    def can_view(self, user: User, legal_case: Case) -> bool:
        """Whether ``user`` may read this particular case."""
        return self.sees_all_cases(user) or legal_case.is_assigned_to(user.id)

    def require_view(self, user: User, legal_case: Case) -> None:
        """Allow only a user entitled to this case.

        Raises:
            CaseAccessDeniedError: the caller holds ``cases:view`` but is not
                assigned to this case.
        """
        if not self.can_view(user, legal_case):
            self._deny(user, legal_case, reason="not_assigned")

    # -------------------------------------------------------------- fields #

    def writable_fields(self, user: User) -> frozenset[str]:
        """Every governed field ``user`` may write, plus a marker for the rest.

        Used to describe a caller's reach; :meth:`require_writable` is what
        enforces it.
        """
        granted = self._authorization.permissions_for(user)
        return frozenset(
            field
            for field, permissions in FIELD_PERMISSIONS.items()
            if permissions & granted
        )

    def require_writable(self, user: User, fields: Iterable[str], *, legal_case: Case) -> None:
        """Allow only a caller whose permissions cover every field they are writing.

        A partial write is never performed: if any single field is out of reach
        the whole request is refused, so a court representative who submits a
        full case form does not silently have half of it applied.

        Raises:
            CaseAccessDeniedError: at least one field is beyond the caller's
                permissions.
        """
        granted = self._authorization.permissions_for(user)

        refused = sorted(
            field
            for field in fields
            if not (FIELD_PERMISSIONS.get(field, frozenset({Permission.CASES_UPDATE})) & granted)
        )
        if refused:
            self._deny(user, legal_case, reason="field_not_permitted", fields=refused)

    # ------------------------------------------------------------- helpers #

    def _deny(
        self,
        user: User,
        legal_case: Case,
        *,
        reason: str,
        fields: list[str] | None = None,
    ) -> None:
        """Log the denial with its specifics, then fail with a generic 403.

        Mirrors :meth:`AuthorizationService._deny`: the log carries what was
        refused and why, correlatable with the response's ``request_id``, while
        the body says only that access was refused. The case *number* is logged
        rather than its title, which is client-confidential.
        """
        logger.warning(
            "case_access_denied",
            user_id=str(user.id),
            role=user.role.value,
            case_id=str(legal_case.id),
            case_number=legal_case.case_number,
            reason=reason,
            fields=fields,
        )
        raise CaseAccessDeniedError
