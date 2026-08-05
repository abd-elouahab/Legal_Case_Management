"""Document management business logic.

Owns the workflow ``07-document-management.md`` defines: uploading, listing,
retrieving, previewing, downloading, updating, replacing, and deleting a document
attached to a legal case.

Scope boundaries, kept deliberately sharp:

* **Capability authorization is not decided here.** Whether the caller may use
  the document endpoints is settled by the dependencies in
  :mod:`api.authorization` before a request reaches this service.
* **Per-resource authorization is applied here**, through
  :class:`~services.document_access.DocumentAccessPolicy`, because it needs the
  case row — a dependency cannot know whether a lawyer is assigned to a case it
  has not loaded.
* **File validation is not decided here** either: :mod:`services.document_validation`
  owns it, so upload and replacement enforce one rule set.
* **Bytes never pass through this module.** It hands a stream to
  :class:`~services.document_storage.DocumentStorageService` and receives one
  back; the only thing it writes to PostgreSQL is metadata.

What it does own are the rules nothing else can express: that a document belongs
to a case its uploader may reach, that a replacement adds a version rather than
overwriting one, that a deletion is logical, and that the ``documents`` row
always mirrors its current version.

It also **publishes to the timeline** — uploads, metadata edits, replacements,
deletions, and downloads are the document half of a case's history. As with the
case service, publication happens after the change is committed, describes rather
than decides, and can never fail the request that caused it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from core.documents import build_storage_key, is_previewable
from core.exceptions import (
    CaseNotFoundError,
    DocumentNotFoundError,
    DocumentPreviewUnavailableError,
    DocumentVersionNotFoundError,
)
from models.case import Case
from models.document import Document, DocumentVersion
from models.timeline import TimelineEventType
from models.user import User
from repositories.case import CaseRepository
from repositories.document import DocumentRepository
from schemas.document import DocumentListQuery, DocumentUpdate, DocumentUploadForm
from services.document_access import DocumentAccessPolicy
from services.document_storage import DocumentStorageService, ObjectStream
from services.document_validation import ValidatedUpload
from services.timeline import NullTimelineRecorder, TimelineRecorder

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentPageResult:
    """One page of documents together with the total matching the filters."""

    documents: list[Document]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class DocumentDownload:
    """An open file body plus what an HTTP response needs to describe it."""

    stream: ObjectStream
    filename: str
    mime_type: str
    size: int
    version: int


class DocumentService:
    """Coordinates the document lifecycle."""

    def __init__(
        self,
        documents: DocumentRepository,
        cases: CaseRepository,
        storage: DocumentStorageService | None = None,
        access: DocumentAccessPolicy | None = None,
        *,
        timeline: TimelineRecorder | None = None,
    ) -> None:
        self._documents = documents
        self._cases = cases
        self._storage = storage or DocumentStorageService()
        self._access = access or DocumentAccessPolicy()
        # See `CaseService.__init__` for why the default records nothing; the
        # application wires the real recorder in `api.deps.get_document_service`.
        self._timeline: TimelineRecorder = timeline or NullTimelineRecorder()

    # ------------------------------------------------------------- reading #

    def list_documents(self, query: DocumentListQuery, *, actor: User) -> DocumentPageResult:
        """Return one page of documents the caller is entitled to see.

        Search, filtering, sorting, pagination, **and the case scope** are all
        applied in the database, so the cost of a page does not grow with the
        number of documents and the totals describe only what the caller may
        access.
        """
        documents, total = self._documents.list_documents(
            query, visible_to=self._access.visibility_scope(actor)
        )
        return DocumentPageResult(
            documents=documents, total=total, page=query.page, page_size=query.page_size
        )

    def get_document(self, document_id: uuid.UUID, *, actor: User) -> Document:
        """Return one document the caller is entitled to see.

        Raises:
            DocumentNotFoundError: no live document has this identifier.
            DocumentAccessDeniedError: the caller is not party to its case.
        """
        document = self._require_document(document_id)
        self._access.require_view(actor, document)
        return document

    def list_versions(self, document_id: uuid.UUID, *, actor: User) -> list[DocumentVersion]:
        """Return a document's complete version history, oldest first."""
        return self.get_document(document_id, actor=actor).versions

    # ------------------------------------------------------------ creating #

    def upload_document(
        self, payload: DocumentUploadForm, upload: ValidatedUpload, *, actor: User
    ) -> Document:
        """Store an uploaded file and record it as version 1 of a new document.

        Raises:
            CaseNotFoundError: no case has the identifier in the form.
            DocumentAccessDeniedError: the caller is not party to that case.
            DocumentStorageError: MinIO was unreachable or refused the write.
        """
        legal_case = self._require_case(payload.case_id)
        self._access.require_case_access(actor, legal_case)

        # The identifier is minted before the object is written, because the
        # storage key is derived from it — the file has to know which document it
        # belongs to before the row exists.
        document_id = uuid.uuid4()
        storage_key = build_storage_key(
            case_id=legal_case.id,
            document_id=document_id,
            version=1,
            stored_filename=upload.stored_filename,
        )
        self._store(upload, key=storage_key)

        document = Document(
            id=document_id,
            case_id=legal_case.id,
            category=payload.category,
            description=payload.description,
            version=1,
            storage_bucket=self._storage.bucket,
            storage_key=storage_key,
            **self._binary_fields(upload, actor=actor),
        )
        document.versions.append(
            self._build_version(
                document_id=document_id, version=1, key=storage_key, upload=upload, actor=actor
            )
        )

        saved = self._persist(document, key=storage_key, operation="upload", is_new=True)

        logger.info("document_uploaded", **self._event(saved, actor=actor))
        self._publish(
            saved,
            TimelineEventType.DOCUMENT_UPLOADED,
            actor=actor,
            description=f'{actor.full_name} uploaded "{saved.original_filename}".',
        )
        return saved

    # ------------------------------------------------------------ updating #

    def update_document(
        self, document_id: uuid.UUID, payload: DocumentUpdate, *, actor: User
    ) -> Document:
        """Update a document's category and description.

        The binary is never touched: the update schema has no field that could
        describe one, and changing the file is a *replacement* with its own
        endpoint and its own permission.

        Raises:
            DocumentNotFoundError: no live document has this identifier.
            DocumentAccessDeniedError: the caller is not party to its case.
        """
        document = self.get_document(document_id, actor=actor)

        changes = payload.provided_fields()
        for field, value in changes.items():
            setattr(document, field, value)

        saved = self._documents.save(document)

        logger.info(
            "document_updated",
            # Field *names* only: an operator can see what an administrator
            # touched without a description — which can quote a client's
            # business — entering the log.
            fields=sorted(changes),
            **self._event(saved, actor=actor),
        )
        self._publish(
            saved,
            TimelineEventType.DOCUMENT_UPDATED,
            actor=actor,
            description=(
                f'{actor.full_name} updated the details of "{saved.original_filename}".'
            ),
            # Unlike the application log, the timeline may carry the *values*: it
            # is served only to users already entitled to the case, whereas a log
            # line goes to an operator with no such entitlement.
            extra={"fields": sorted(changes)},
        )
        return saved

    def replace_document(
        self, document_id: uuid.UUID, upload: ValidatedUpload, *, actor: User
    ) -> Document:
        """Add a new version of a document, preserving every previous one.

        The previous file is **not** overwritten and **not** removed: the new
        version gets its own storage key, and the old row stays in the history
        so it remains downloadable. That is `architecture.md` invariant 6, and it
        holds structurally rather than by convention — the key contains the
        version number, so writing over a predecessor is not expressible.

        The next version number comes from the *history*, not from
        ``documents.version``: a version row is never deleted, so a number taken
        from there can never collide with one already issued.

        Raises:
            DocumentNotFoundError: no live document has this identifier.
            DocumentAccessDeniedError: the caller is not party to its case.
            DocumentStorageError: MinIO was unreachable or refused the write.
        """
        document = self.get_document(document_id, actor=actor)

        previous_version = document.version
        next_version = self._documents.highest_version(document.id) + 1
        storage_key = build_storage_key(
            case_id=document.case_id,
            document_id=document.id,
            version=next_version,
            stored_filename=upload.stored_filename,
        )
        self._store(upload, key=storage_key)

        document.versions.append(
            self._build_version(
                document_id=document.id,
                version=next_version,
                key=storage_key,
                upload=upload,
                actor=actor,
            )
        )

        # The document row mirrors its current version — one rule, applied here
        # and at upload, with no exceptions to remember.
        document.version = next_version
        document.storage_bucket = self._storage.bucket
        document.storage_key = storage_key
        for field, value in self._binary_fields(upload, actor=actor).items():
            setattr(document, field, value)

        saved = self._persist(document, key=storage_key, operation="replace", is_new=False)

        logger.info(
            "document_replaced",
            previous_version=previous_version,
            **self._event(saved, actor=actor),
        )
        self._publish(
            saved,
            TimelineEventType.DOCUMENT_REPLACED,
            actor=actor,
            description=(
                f'{actor.full_name} replaced "{saved.original_filename}" with version '
                f"{saved.version}."
            ),
            extra={"previous_version": previous_version},
        )
        return saved

    def delete_document(self, document_id: uuid.UUID, *, actor: User) -> Document:
        """Delete a document logically.

        The row is kept and **every stored file is kept**: `code-standards.md`
        forbids permanently destroying a legal document, and the spec asks for a
        soft delete explicitly. The document leaves every list and can no longer
        be read, previewed, or downloaded — but the deletion is recoverable, and
        a future cleanup job is what eventually reclaims the storage.

        Idempotent: deleting an already-deleted document succeeds and leaves the
        original deletion time intact, so a repeated request is not an error.

        Raises:
            DocumentNotFoundError: no document has this identifier at all.
            DocumentAccessDeniedError: the caller is not party to its case.
        """
        # Deliberately includes deleted rows: a second DELETE must be a no-op
        # rather than a 404, which is what makes the operation idempotent.
        document = self._documents.get_by_id(document_id, include_deleted=True)
        if document is None:
            logger.info("document_lookup_failed", document_id=str(document_id))
            raise DocumentNotFoundError

        self._access.require_view(actor, document)

        already_deleted = document.is_deleted
        if not already_deleted:
            document.deleted_at = datetime.now(UTC)
            document = self._documents.save(document)
            # Not a physical delete — see DocumentStorageService.delete_object.
            for version in document.versions:
                self._storage.delete_object(version.storage_key, reason="document_deleted")

        logger.info(
            "document_deleted",
            already_deleted=already_deleted,
            **self._event(document, actor=actor),
        )
        # Only when something actually changed. Deletion is idempotent, and a
        # repeated request must not append a second "deleted" entry to a history
        # in which nothing happened the second time.
        if not already_deleted:
            self._publish(
                document,
                TimelineEventType.DOCUMENT_DELETED,
                actor=actor,
                description=f'{actor.full_name} deleted "{document.original_filename}".',
            )
        return document

    # ------------------------------------------------------------- serving #

    def open_download(
        self, document_id: uuid.UUID, *, actor: User, version: int | None = None
    ) -> DocumentDownload:
        """Open a document's file for download, current version by default.

        Raises:
            DocumentNotFoundError: no live document has this identifier.
            DocumentVersionNotFoundError: the document has no such version.
            DocumentAccessDeniedError: the caller is not party to its case.
            DocumentStorageError: MinIO was unreachable or the object is gone.
        """
        document = self.get_document(document_id, actor=actor)
        entry = self._resolve_version(document, version)
        download = self._open(entry)

        logger.info(
            "document_downloaded", version=entry.version, **self._event(document, actor=actor)
        )
        # Recorded because ``08-timeline.md`` lists DOCUMENT_DOWNLOADED as a
        # document event, and who took a copy of a legal document is exactly the
        # accountability the audit trail exists for. Preview is deliberately *not*
        # recorded: the spec defines no event for it, and inventing one is not
        # this feature's call.
        self._publish(
            document,
            TimelineEventType.DOCUMENT_DOWNLOADED,
            actor=actor,
            description=(
                f'{actor.full_name} downloaded "{entry.original_filename}" '
                f"(version {entry.version})."
            ),
            extra={"downloaded_version": entry.version},
        )
        return download

    def open_preview(
        self, document_id: uuid.UUID, *, actor: User, version: int | None = None
    ) -> DocumentDownload:
        """Open a document's file for inline display in a browser.

        Raises:
            DocumentPreviewUnavailableError: the file type cannot be rendered
                inline — the caller should fall back to the download endpoint.
            (Plus everything :meth:`open_download` raises.)
        """
        document = self.get_document(document_id, actor=actor)
        entry = self._resolve_version(document, version)

        if not is_previewable(entry.file_extension):
            logger.info(
                "document_preview_unavailable",
                version=entry.version,
                **self._event(document, actor=actor),
            )
            raise DocumentPreviewUnavailableError

        preview = self._open(entry)

        logger.info(
            "document_previewed", version=entry.version, **self._event(document, actor=actor)
        )
        return preview

    # ------------------------------------------------------------- helpers #

    def _require_document(self, document_id: uuid.UUID) -> Document:
        document = self._documents.get_by_id(document_id)
        if document is None:
            logger.info("document_lookup_failed", document_id=str(document_id))
            raise DocumentNotFoundError
        return document

    def _require_case(self, case_id: uuid.UUID) -> Case:
        legal_case = self._cases.get_by_id(case_id)
        if legal_case is None:
            logger.info("document_case_lookup_failed", case_id=str(case_id))
            raise CaseNotFoundError
        return legal_case

    @staticmethod
    def _resolve_version(document: Document, version: int | None) -> DocumentVersion:
        """Return the requested version, or the current one when unspecified.

        Raises:
            DocumentVersionNotFoundError: the document has no such version.
        """
        wanted = document.version if version is None else version
        entry = next((item for item in document.versions if item.version == wanted), None)

        if entry is None:
            logger.info(
                "document_version_lookup_failed",
                document_id=str(document.id),
                requested_version=wanted,
            )
            raise DocumentVersionNotFoundError
        return entry

    def _open(self, entry: DocumentVersion) -> DocumentDownload:
        return DocumentDownload(
            stream=self._storage.open_object(entry.storage_key),
            filename=entry.original_filename,
            mime_type=entry.mime_type,
            size=entry.file_size,
            version=entry.version,
        )

    def _store(self, upload: ValidatedUpload, *, key: str) -> None:
        """Write the uploaded bytes to object storage.

        Storage comes **before** the database write, deliberately. The reverse
        order would let a committed row point at an object that was never
        written — a document that exists but cannot be downloaded, which no
        retry can repair. This order can only leave an *unreferenced* object,
        which is harmless and is exactly what the cleanup job the spec
        anticipates is for.
        """
        self._storage.upload_object(
            key=key,
            stream=upload.stream,
            size=upload.size,
            content_type=upload.mime_type,
        )

    def _persist(
        self, document: Document, *, key: str, operation: str, is_new: bool
    ) -> Document:
        """Commit the metadata, leaving the stored object behind if it fails.

        The object is **not** removed on failure: the storage service has no
        physical delete by design, and an unreferenced object is a storage cost
        rather than a correctness problem. It is logged so the cleanup job — and
        an operator reading the failure — can account for it.
        """
        try:
            return self._documents.create(document) if is_new else self._documents.save(document)
        except Exception:
            self._documents.rollback()
            logger.error(
                "document_metadata_write_failed",
                operation=operation,
                document_id=str(document.id),
                case_id=str(document.case_id),
                orphaned_object_key=key,
            )
            raise

    @staticmethod
    def _binary_fields(upload: ValidatedUpload, *, actor: User) -> dict[str, Any]:
        """The columns that mirror the current version's file."""
        return {
            "original_filename": upload.original_filename,
            "stored_filename": upload.stored_filename,
            "file_extension": upload.extension,
            "mime_type": upload.mime_type,
            "file_size": upload.size,
            "uploaded_by": actor.id,
        }

    def _build_version(
        self,
        *,
        document_id: uuid.UUID,
        version: int,
        key: str,
        upload: ValidatedUpload,
        actor: User,
    ) -> DocumentVersion:
        """Build the immutable history row for one uploaded file."""
        return DocumentVersion(
            id=uuid.uuid4(),
            document_id=document_id,
            version=version,
            original_filename=upload.original_filename,
            stored_filename=upload.stored_filename,
            file_extension=upload.extension,
            mime_type=upload.mime_type,
            file_size=upload.size,
            # The injected storage service, not a fresh one: a test that swaps in
            # a fake bucket must see it recorded on the version too.
            storage_bucket=self._storage.bucket,
            storage_key=key,
            uploaded_by=actor.id,
        )

    def _publish(
        self,
        document: Document,
        event_type: TimelineEventType,
        *,
        actor: User,
        description: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Announce a document event on its case's timeline.

        Every document event carries the same identifying metadata, assembled
        here so five call sites cannot each remember a different subset of it;
        ``extra`` adds whatever is specific to one of them.

        **The filename appears here and nowhere in the application log.** That is
        the spec's separation, not an oversight: the timeline is served only to
        users already entitled to the document's case, while a log line goes to an
        operator who has no such entitlement — and a filename can name a client or
        a matter.
        """
        self._timeline.record(
            case_id=document.case_id,
            event_type=event_type,
            actor=actor,
            description=description,
            metadata={
                "document_id": document.id,
                "filename": document.original_filename,
                "category": document.category.value,
                "version": document.version,
                "file_extension": document.file_extension,
                "file_size": document.file_size,
                **(extra or {}),
            },
        )

    @staticmethod
    def _event(document: Document, *, actor: User) -> dict[str, Any]:
        """The fields every document log entry carries.

        Identifiers, the case number, the category, and the file's shape — never
        the original filename and never the description. Both can name a client
        or quote a matter, and the spec forbids logging sensitive document
        information.
        """
        return {
            "document_id": str(document.id),
            "case_id": str(document.case_id),
            "category": document.category.value,
            "document_version": document.version,
            "file_extension": document.file_extension,
            "file_size": document.file_size,
            "actor_id": str(actor.id),
        }
