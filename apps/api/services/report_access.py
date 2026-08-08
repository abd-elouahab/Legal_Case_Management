"""Per-resource authorization for report generation.

RBAC answers "may this user generate reports?". It cannot answer "may this user
generate one *for this case*", because that depends on data — `code-standards.md`
requires both, and ``14-ai-report-agent.md`` says to *"reuse the existing
authorization model"* rather than invent a second scheme.

**This module governs exactly one question, and that is the whole of its
design.** A report has two authorization questions and they have two different
answers, in two different places:

* *"May this caller generate a report about this case?"* — a question about the
  **case**, and therefore this module's. It owns no policy of its own; it
  delegates to :class:`~services.case_access.CaseAccessPolicy`, exactly as
  :mod:`services.document_access` and :mod:`services.timeline_access` do, so a
  report can never be generated from a case its requester cannot open. A refusal
  is a **403**, because a lawyer who follows a colleague's link to a case needs
  to know it exists and that they should ask to be assigned.
* *"May this caller read this report?"* — a question about **ownership**, and
  therefore not here at all. Every read in :mod:`repositories.report` is keyed by
  ``requested_by``, so there is no query in the platform that can return another
  user's report and nothing for a second rule to keep in step with. A report
  belonging to somebody else is a **404**, for the same reason a conversation is:
  confirming that another user's private work product exists is itself the
  disclosure ``14-ai-report-agent.md`` forbids when it says history must remain
  user-specific and generated reports must remain private.

**And the content is authorized by neither.** Every section is produced by
:meth:`~services.rag.RagService.answer`, which retrieves through
:class:`~services.search.SearchService`, which scopes every passage to the cases
the caller is party to *inside the vector query*. So the spec's *"generated
reports must never contain unauthorized information"* and *"citations never
expose unauthorized documents"* are inherited from the pipeline rather than
re-implemented here — which is also why this module has no method that looks at
a citation.
"""

from __future__ import annotations

import uuid

import structlog

from core.exceptions import CaseAccessDeniedError
from models.case import Case
from models.user import User
from services.case_access import CaseAccessPolicy

logger = structlog.get_logger(__name__)


class ReportAccessPolicy:
    """Decides which cases a user may generate a report about, by asking about the case.

    Stateless and pure — it reads the caller's role-derived permissions and the
    case's assignment columns, and touches neither the database nor the network.
    """

    def __init__(self, cases: CaseAccessPolicy | None = None) -> None:
        self._cases = cases or CaseAccessPolicy()

    def can_use_case(self, user: User, legal_case: Case) -> bool:
        """Whether ``user`` may build a report from this case's documents."""
        return self._cases.can_view(user, legal_case)

    def require_case_access(self, user: User, legal_case: Case) -> None:
        """Allow only a user entitled to this case.

        Used by both write paths — requesting a report and regenerating one —
        because both produce fresh content from the case. It is deliberately
        re-checked on **regeneration**: a lawyer unassigned from a matter since
        the first run must not be able to produce a new interpretation of it from
        a report they still hold a link to.

        Raises:
            CaseAccessDeniedError: the caller holds the report-generation
                capability but is not party to this case.
        """
        if not self.can_use_case(user, legal_case):
            self._deny(user, case_id=legal_case.id, case_number=legal_case.case_number)

    def _deny(self, user: User, *, case_id: uuid.UUID, case_number: str) -> None:
        """Log the denial with its specifics, then fail with a generic 403.

        Mirrors :meth:`~services.indexing_access.IndexingAccessPolicy._deny`: the
        log carries what was refused, correlatable with the response's
        ``request_id``, while the body says only that access was refused. The
        case *number* is logged rather than its title, which is
        client-confidential.
        """
        logger.warning(
            "report_access_denied",
            user_id=str(user.id),
            role=user.role.value,
            case_id=str(case_id),
            case_number=case_number,
            reason="not_assigned",
        )
        raise CaseAccessDeniedError


__all__ = ["ReportAccessPolicy"]
