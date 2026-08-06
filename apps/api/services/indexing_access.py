"""Per-resource authorization for document indexes.

RBAC answers "may this user use the indexing capabilities?". It cannot answer
"may this user reach *this* index", because that depends on data —
`code-standards.md` requires both, and ``10-document-indexing.md`` says to reuse
the existing authorization rather than invent a second scheme.

The rule is one line long, and that is the point: **an index is reachable exactly
when its document is**, which is in turn exactly when the document's case is.
This module therefore owns no policy of its own — it delegates to
:class:`~services.document_access.DocumentAccessPolicy`, which delegates to
:class:`~services.case_access.CaseAccessPolicy`. The chain is stated once rather
than restated at each link, and it is the same chain
:mod:`services.ocr_access` uses, so an index cannot become more visible than the
extracted text it was built from, which cannot become more visible than the file
that text was read from.

It exists as its own module, rather than as a few calls scattered through the
indexing service, so the delegation can be unit-tested as the invariant it is.

**This is also where the spec's "future search results must inherit document
permissions" is prepared.** Every vector's payload carries its ``case_id`` and
``document_id`` (see :func:`~services.vector_store.build_payload`), and
:meth:`IndexingAccessPolicy.visibility_scope` is the same scope a future search
will need to translate into a Qdrant filter. Nothing about retrieval is
implemented here — but the metadata and the policy it will need both exist, which
is what "prepares the platform" means.
"""

from __future__ import annotations

import uuid

import structlog

from core.exceptions import IndexAccessDeniedError
from models.document import Document
from models.indexing import DocumentIndex
from models.user import User
from services.document_access import DocumentAccessPolicy

logger = structlog.get_logger(__name__)


class IndexingAccessPolicy:
    """Decides which document indexes a user may reach, by asking about the document.

    Stateless and pure — it reads the caller's role-derived permissions and the
    case's assignment columns, and touches neither the database nor the network.
    """

    def __init__(self, documents: DocumentAccessPolicy | None = None) -> None:
        self._documents = documents or DocumentAccessPolicy()

    # --------------------------------------------------------------- scope #

    def visibility_scope(self, user: User) -> uuid.UUID | None:
        """The user id the index list must be restricted to, or ``None``.

        Returned rather than applied here so the restriction can be pushed into
        the SQL query — filtering in Python would mean fetching every index to
        hide most of them, and would make the page totals count documents the
        caller is not entitled to know about.
        """
        return self._documents.visibility_scope(user)

    # ------------------------------------------------------------ decisions #

    def can_view_document(self, user: User, document: Document) -> bool:
        """Whether ``user`` may reach indexing information for this document."""
        return self._documents.can_view(user, document)

    def require_document_access(self, user: User, document: Document) -> None:
        """Allow only a user entitled to this document.

        Used by every indexing path, because each of them starts from a document:
        a status read, a history read, and a re-index all name one in the URL.

        Raises:
            IndexAccessDeniedError: the caller holds the indexing capability but
                is not party to the case the document belongs to.
        """
        if not self.can_view_document(user, document):
            self._deny(user, document_id=document.id, case_id=document.case_id)

    def can_view(self, user: User, index: DocumentIndex) -> bool:
        """Whether ``user`` may reach this particular index."""
        return self.can_view_document(user, index.document)

    def require_view(self, user: User, index: DocumentIndex) -> None:
        """Allow only a user entitled to this index's document.

        Raises:
            IndexAccessDeniedError: the caller is not party to the case the
                index's document belongs to and does not hold ``cases:view-all``.
        """
        if not self.can_view(user, index):
            self._deny(
                user,
                document_id=index.document_id,
                case_id=index.case_id,
                index_id=index.id,
            )

    # ------------------------------------------------------------- helpers #

    def _deny(
        self,
        user: User,
        *,
        document_id: uuid.UUID,
        case_id: uuid.UUID,
        index_id: uuid.UUID | None = None,
    ) -> None:
        """Log the denial with its specifics, then fail with a generic 403.

        Mirrors :meth:`OcrAccessPolicy._deny`: the log carries what was refused,
        correlatable with the response's ``request_id``, while the body says only
        that access was refused. Identifiers only — never a filename and never a
        fragment of indexed text, both of which can name a client or quote a
        matter.
        """
        logger.warning(
            "indexing_access_denied",
            user_id=str(user.id),
            role=user.role.value,
            document_id=str(document_id),
            case_id=str(case_id),
            index_id=str(index_id) if index_id is not None else None,
            reason="not_assigned",
        )
        raise IndexAccessDeniedError
