"""Per-resource authorization for OCR results.

RBAC answers "may this user use the OCR capabilities?". It cannot answer "may
this user reach *this* extraction", because that depends on data —
`code-standards.md` requires both, and the spec states the rule directly: *"users
may only access OCR information for documents they are already authorized to
access"* and *"extracted text inherits document permissions"*.

The rule is one line long, and that is the point: **an OCR result is reachable
exactly when its document is**, which is in turn exactly when the document's case
is. This module therefore owns no policy of its own — it delegates to
:class:`~services.document_access.DocumentAccessPolicy`, which delegates to
:class:`~services.case_access.CaseAccessPolicy`. Extracted text cannot become
more visible than the file it was read from, and the chain is stated once rather
than restated at each link.

It exists as its own module, rather than as a few calls scattered through the OCR
service, so the delegation can be unit-tested as the invariant it is — the same
reason :mod:`services.document_access` exists.
"""

from __future__ import annotations

import uuid

import structlog

from core.exceptions import OcrAccessDeniedError
from models.document import Document
from models.ocr import OcrResult
from models.user import User
from services.document_access import DocumentAccessPolicy

logger = structlog.get_logger(__name__)


class OcrAccessPolicy:
    """Decides which OCR results a user may reach, by asking about the document.

    Stateless and pure — it reads the caller's role-derived permissions and the
    case's assignment columns, and touches neither the database nor the network.
    """

    def __init__(self, documents: DocumentAccessPolicy | None = None) -> None:
        self._documents = documents or DocumentAccessPolicy()

    # --------------------------------------------------------------- scope #

    def visibility_scope(self, user: User) -> uuid.UUID | None:
        """The user id the OCR list must be restricted to, or ``None``.

        Returned rather than applied here so the restriction can be pushed into
        the SQL query — filtering in Python would mean fetching every run to hide
        most of them, and would make the page totals count runs the caller is not
        entitled to know about.
        """
        return self._documents.visibility_scope(user)

    # ------------------------------------------------------------ decisions #

    def can_view_document(self, user: User, document: Document) -> bool:
        """Whether ``user`` may reach OCR information for this document."""
        return self._documents.can_view(user, document)

    def require_document_access(self, user: User, document: Document) -> None:
        """Allow only a user entitled to this document.

        Used by every OCR path, because each of them starts from a document: a
        status read, a text read, and a retry all name one in the URL.

        Raises:
            OcrAccessDeniedError: the caller holds the OCR capability but is not
                party to the case the document belongs to.
        """
        if not self.can_view_document(user, document):
            self._deny(user, document_id=document.id, case_id=document.case_id)

    def can_view(self, user: User, result: OcrResult) -> bool:
        """Whether ``user`` may reach this particular run."""
        return self.can_view_document(user, result.document)

    def require_view(self, user: User, result: OcrResult) -> None:
        """Allow only a user entitled to this run's document.

        Raises:
            OcrAccessDeniedError: the caller is not party to the case the run's
                document belongs to and does not hold ``cases:view-all``.
        """
        if not self.can_view(user, result):
            self._deny(
                user,
                document_id=result.document_id,
                case_id=result.document.case_id,
                ocr_result_id=result.id,
            )

    # ------------------------------------------------------------- helpers #

    def _deny(
        self,
        user: User,
        *,
        document_id: uuid.UUID,
        case_id: uuid.UUID,
        ocr_result_id: uuid.UUID | None = None,
    ) -> None:
        """Log the denial with its specifics, then fail with a generic 403.

        Mirrors :meth:`DocumentAccessPolicy._deny`: the log carries what was
        refused, correlatable with the response's ``request_id``, while the body
        says only that access was refused. Identifiers only — never a filename
        and never a fragment of extracted text, both of which can name a client
        or quote a matter.
        """
        logger.warning(
            "ocr_access_denied",
            user_id=str(user.id),
            role=user.role.value,
            document_id=str(document_id),
            case_id=str(case_id),
            ocr_result_id=str(ocr_result_id) if ocr_result_id is not None else None,
            reason="not_assigned",
        )
        raise OcrAccessDeniedError
