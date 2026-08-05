"""Per-resource authorization for timeline events.

RBAC answers "may this user use the timeline capability?". It cannot answer "may
this user read *this* case's history", because that depends on data — and
``08-timeline.md`` states the rule directly: *"Users should only be able to view
timeline events for cases they have permission to access. Do not expose timeline
events belonging to unauthorized cases."*

The rule is one line long, and that is the point: **an event is reachable exactly
when its case is.** This module therefore owns no policy of its own — it delegates
every decision to :class:`~services.case_access.CaseAccessPolicy`, exactly as
:mod:`services.document_access` does, so timeline visibility cannot drift from
case visibility when the case rules are refined. It exists as its own module
rather than as a few calls scattered through the timeline service so that the
delegation is stated once and can be unit-tested as the invariant it is.
"""

from __future__ import annotations

import uuid

import structlog

from core.exceptions import TimelineAccessDeniedError
from models.case import Case
from models.timeline import TimelineEvent
from models.user import User
from services.case_access import CaseAccessPolicy

logger = structlog.get_logger(__name__)


class TimelineAccessPolicy:
    """Decides which timeline events a user may reach, by asking about their case.

    Stateless and pure — it reads the caller's role-derived permissions and the
    case's assignment columns, and touches neither the database nor the network.
    """

    def __init__(self, cases: CaseAccessPolicy | None = None) -> None:
        self._cases = cases or CaseAccessPolicy()

    # --------------------------------------------------------------- scope #

    def visibility_scope(self, user: User) -> uuid.UUID | None:
        """The user id the timeline query must be restricted to, or ``None``.

        Returned rather than applied here so the restriction can be pushed into
        the SQL query — filtering in Python would mean fetching every event to
        hide most of them, and would make the page totals count events the caller
        is not entitled to know about.
        """
        return self._cases.visibility_scope(user)

    # ------------------------------------------------------------ decisions #

    def can_view_case(self, user: User, legal_case: Case) -> bool:
        """Whether ``user`` may read this case's timeline."""
        return self._cases.can_view(user, legal_case)

    def require_case_access(self, user: User, legal_case: Case) -> None:
        """Allow only a user entitled to this case's history.

        Used by the case timeline endpoint, which has a case but no event yet.

        Raises:
            TimelineAccessDeniedError: the caller holds ``timeline:view`` but is
                not assigned to the case.
        """
        if not self.can_view_case(user, legal_case):
            self._deny(user, case_id=legal_case.id, case_number=legal_case.case_number)

    def can_view(self, user: User, event: TimelineEvent) -> bool:
        """Whether ``user`` may reach this particular event."""
        return self.can_view_case(user, event.case)

    def require_view(self, user: User, event: TimelineEvent) -> None:
        """Allow only a user entitled to this event's case.

        Raises:
            TimelineAccessDeniedError: the caller is not assigned to the case the
                event belongs to and does not hold ``cases:view-all``.
        """
        if not self.can_view(user, event):
            self._deny(
                user,
                case_id=event.case_id,
                case_number=event.case.case_number,
                event_id=event.id,
            )

    # ------------------------------------------------------------- helpers #

    def _deny(
        self,
        user: User,
        *,
        case_id: uuid.UUID,
        case_number: str,
        event_id: uuid.UUID | None = None,
    ) -> None:
        """Log the denial with its specifics, then fail with a generic 403.

        Mirrors :meth:`CaseAccessPolicy._deny`: the log carries what was refused,
        correlatable with the response's ``request_id``, while the body says only
        that access was refused. Identifiers and the case number only — never an
        event title or description, either of which can name a client or quote a
        filename.
        """
        logger.warning(
            "timeline_access_denied",
            user_id=str(user.id),
            role=user.role.value,
            case_id=str(case_id),
            case_number=case_number,
            event_id=str(event_id) if event_id is not None else None,
            reason="not_assigned",
        )
        raise TimelineAccessDeniedError
