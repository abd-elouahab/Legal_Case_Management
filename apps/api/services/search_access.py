"""Per-resource authorization for semantic search.

RBAC answers "may this user search?". It cannot answer "may this user see *this*
passage", because that depends on data — `code-standards.md` requires both, and
``11-semantic-search.md`` says to *"reuse the existing authorization system"*
rather than invent a second scheme.

The rule is one line long, and that is the point: **a passage is reachable
exactly when its document is**, which is in turn exactly when the document's case
is. This module therefore owns no policy of its own — it delegates to
:class:`~services.document_access.DocumentAccessPolicy`, which delegates to
:class:`~services.case_access.CaseAccessPolicy`. The chain is **search →
document → case**, the same chain :mod:`services.ocr_access` and
:mod:`services.indexing_access` use, so a search result can never be more visible
than the index it came from, which can never be more visible than the extracted
text, which can never be more visible than the file.

**What is different here, and it is the only thing that is.** Every other feature
applies that scope as a predicate inside the SQL query it was already running.
Search cannot: the rows it filters live in Qdrant, which cannot join against
``cases``. So the scope crosses the boundary as a *set of case identifiers*
(:meth:`accessible_case_ids`) and becomes one more ``must`` condition on the
vector query — which is exactly what Document Indexing put ``case_id`` in every
payload for.

Three properties make that safe, and each is asserted by a test rather than
assumed:

* the scope is computed from the caller alone and can never be supplied by the
  request;
* it is **ANDed** with every user filter, so no combination of filters can widen
  it (the spec's *"metadata filtering cannot bypass permissions"*);
* "assigned to no cases" is an **empty set that matches nothing**, never an
  absent filter that matches everything. That distinction is the one mistake in
  this feature that would be catastrophic and silent, so it is named in three
  places: here, in :class:`~services.vector_search.SearchFilters`, and in the
  service's short circuit.
"""

from __future__ import annotations

import uuid

import structlog

from core.exceptions import SearchAccessDeniedError
from models.case import Case
from models.document import Document
from models.user import User
from services.document_access import DocumentAccessPolicy

logger = structlog.get_logger(__name__)


class SearchAccessPolicy:
    """Decides which passages a user may retrieve, by asking about their case.

    Stateless and pure — it reads the caller's role-derived permissions and the
    case's assignment columns, and touches neither the database nor the network.
    Enumerating the caller's cases is a *repository* concern
    (:meth:`~repositories.search.SearchRepository.accessible_case_ids`), kept out
    of here so this class stays testable without a session, exactly as
    :class:`~services.indexing_access.IndexingAccessPolicy` is.
    """

    def __init__(self, documents: DocumentAccessPolicy | None = None) -> None:
        self._documents = documents or DocumentAccessPolicy()

    # --------------------------------------------------------------- scope #

    def visibility_scope(self, user: User) -> uuid.UUID | None:
        """The user id search results must be restricted to, or ``None``.

        ``None`` means the caller holds ``cases:view-all`` and the case filter is
        lifted entirely. Anything else is the identifier whose assignments define
        the searchable corpus.

        Identical in shape and meaning to the scope every other module returns,
        so a reviewer comparing the five sees one rule rather than five.
        """
        return self._documents.visibility_scope(user)

    def searches_everything(self, user: User) -> bool:
        """Whether this caller's search is unrestricted by case."""
        return self.visibility_scope(user) is None

    # ------------------------------------------------------------ decisions #

    def can_search_case(self, user: User, legal_case: Case) -> bool:
        """Whether ``user`` may retrieve passages from this case's documents."""
        return self._documents.can_use_case(user, legal_case)

    def require_case_access(self, user: User, legal_case: Case) -> None:
        """Allow only a user entitled to this case.

        Used when a request **filters by case**. Refused with a 403 rather than
        answered with an empty result set, for the reason
        :mod:`services.timeline_access` gives: an inaccessible case and a case
        with no matching passages must not be indistinguishable, or a caller can
        probe for the existence of matters they are not on.

        Note the asymmetry, which is deliberate: an *unfiltered* search by the
        same caller is never refused — it simply searches the cases they can
        reach. Refusing is only possible when the caller named something.

        Raises:
            SearchAccessDeniedError: the caller holds ``search:query`` but is not
                party to the case they filtered by.
        """
        if not self.can_search_case(user, legal_case):
            self._deny(user, case_id=legal_case.id, reason="case_not_assigned")

    def can_search_document(self, user: User, document: Document) -> bool:
        """Whether ``user`` may retrieve passages from this document."""
        return self._documents.can_view(user, document)

    def require_document_access(self, user: User, document: Document) -> None:
        """Allow only a user entitled to this document.

        Used when a request filters by document.

        Raises:
            SearchAccessDeniedError: the caller is not party to the case the
                document belongs to and does not hold ``cases:view-all``.
        """
        if not self.can_search_document(user, document):
            self._deny(
                user,
                case_id=document.case_id,
                document_id=document.id,
                reason="document_not_assigned",
            )

    # ------------------------------------------------------------- helpers #

    def _deny(
        self,
        user: User,
        *,
        case_id: uuid.UUID,
        reason: str,
        document_id: uuid.UUID | None = None,
    ) -> None:
        """Log the denial with its specifics, then fail with a generic 403.

        Mirrors :meth:`IndexingAccessPolicy._deny`: the log carries what was
        refused, correlatable with the response's ``request_id``, while the body
        says only that access was refused. Identifiers only — **never the query**,
        which names what the caller was looking for, and never a passage, which
        is the document's contents.
        """
        logger.warning(
            "search_access_denied",
            user_id=str(user.id),
            role=user.role.value,
            case_id=str(case_id),
            document_id=str(document_id) if document_id is not None else None,
            reason=reason,
        )
        raise SearchAccessDeniedError


__all__ = ["SearchAccessPolicy"]
