"""Per-resource authorization for documents.

RBAC answers "may this user use the document capabilities?". It cannot answer
"may this user reach *this* document", because that depends on data —
`code-standards.md` requires both, and the spec states the rule directly:
lawyers upload to and view documents on *assigned* cases, court representatives
on *authorized* ones.

The rule is one line long, and that is the point: **a document is reachable
exactly when its case is.** This module therefore owns no policy of its own — it
delegates every decision to :class:`~services.case_access.CaseAccessPolicy`, so
document visibility cannot drift from case visibility when the case rules are
refined. It exists as its own module rather than as a few calls scattered through
the document service so that the delegation is stated once, in one place, and can
be unit-tested as the invariant it is.
"""

from __future__ import annotations

import uuid

import structlog

from core.exceptions import DocumentAccessDeniedError
from models.case import Case
from models.document import Document
from models.user import User
from services.case_access import CaseAccessPolicy

logger = structlog.get_logger(__name__)


class DocumentAccessPolicy:
    """Decides which documents a user may reach, by asking about their case.

    Stateless and pure — it reads the caller's role-derived permissions and the
    case's assignment columns, and touches neither the database nor the network.
    """

    def __init__(self, cases: CaseAccessPolicy | None = None) -> None:
        self._cases = cases or CaseAccessPolicy()

    # --------------------------------------------------------------- scope #

    def visibility_scope(self, user: User) -> uuid.UUID | None:
        """The user id the document list must be restricted to, or ``None``.

        Returned rather than applied here so the restriction can be pushed into
        the SQL query — filtering in Python would mean fetching every document to
        hide most of them, and would make the page totals count documents the
        caller is not entitled to know about.
        """
        return self._cases.visibility_scope(user)

    # ------------------------------------------------------------ decisions #

    def can_use_case(self, user: User, legal_case: Case) -> bool:
        """Whether ``user`` may work with documents on this case."""
        return self._cases.can_view(user, legal_case)

    def require_case_access(self, user: User, legal_case: Case) -> None:
        """Allow only a user entitled to this case.

        Used by upload, which has a case but no document yet.

        Raises:
            DocumentAccessDeniedError: the caller holds the document capability
                but is not assigned to the case.
        """
        if not self.can_use_case(user, legal_case):
            self._deny(user, case_id=legal_case.id, case_number=legal_case.case_number)

    def can_view(self, user: User, document: Document) -> bool:
        """Whether ``user`` may reach this particular document."""
        return self.can_use_case(user, document.case)

    def require_view(self, user: User, document: Document) -> None:
        """Allow only a user entitled to this document's case.

        Raises:
            DocumentAccessDeniedError: the caller is not assigned to the case the
                document belongs to and does not hold ``cases:view-all``.
        """
        if not self.can_view(user, document):
            self._deny(
                user,
                case_id=document.case_id,
                case_number=document.case.case_number,
                document_id=document.id,
            )

    # ------------------------------------------------------------- helpers #

    def _deny(
        self,
        user: User,
        *,
        case_id: uuid.UUID,
        case_number: str,
        document_id: uuid.UUID | None = None,
    ) -> None:
        """Log the denial with its specifics, then fail with a generic 403.

        Mirrors :meth:`CaseAccessPolicy._deny`: the log carries what was refused,
        correlatable with the response's ``request_id``, while the body says only
        that access was refused. Identifiers and the case number only — never a
        filename, which can name a client or a matter.
        """
        logger.warning(
            "document_access_denied",
            user_id=str(user.id),
            role=user.role.value,
            case_id=str(case_id),
            case_number=case_number,
            document_id=str(document_id) if document_id is not None else None,
            reason="not_assigned",
        )
        raise DocumentAccessDeniedError
