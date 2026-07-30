"""Document ORM models.

A document is a file attached to a legal case: its **metadata** lives in
PostgreSQL (here) and its **binary** lives in MinIO, addressed by
``storage_bucket`` + ``storage_key``. Nothing in this module ever holds file
content — `code-standards.md`: *"Never store large files inside PostgreSQL."*

Two tables, because a document has two distinct lifetimes:

* :class:`Document` is the *current* state of a logical document — the file a
  user downloads today, plus the metadata (category, description) that survives
  a replacement.
* :class:`DocumentVersion` is one immutable record per uploaded file, including
  the current one. `architecture.md` invariant 6 says original documents are
  immutable after upload and a new version must preserve its predecessors, so a
  version row is written once and never updated.

The binary columns on :class:`Document` **mirror its current version row**. That
denormalization is deliberate and has exactly one writer
(:meth:`~services.document.DocumentService`): it lets the document list search
filenames, filter by type, and sort by size against a single table, with no join
and no "latest version per document" window function on the hot path.

No authorization logic lives here. Whether a caller may use the document
capabilities is decided by :mod:`core.roles`; whether they may reach *this*
document is decided by :mod:`services.document_access`, which defers to the
document's case.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.case import Case
from models.user import User


class DocumentCategory(StrEnum):
    """What kind of legal document this is.

    The list ``07-document-management.md`` specifies, declared once here. It is
    the single source of truth: :mod:`core.documents` derives the sort order and
    the API catalog from it, so extending the platform is one member added below
    plus a one-line ``ALTER TYPE`` migration — no call site changes.

    Declaration order is the *display* order used by "sort by category" (see
    :data:`~core.documents.CATEGORY_RANK`); it groups the substantive documents
    first and keeps :attr:`OTHER` last, which alphabetical ordering would not.
    """

    #: Agreements and their annexes.
    CONTRACT = "contract"
    #: Material submitted to support a claim.
    EVIDENCE = "evidence"
    #: A ruling, judgment, or order issued by a court.
    COURT_DECISION = "court_decision"
    #: A filing addressed to the court by a party.
    PLEADING = "pleading"
    #: Letters and exchanges with clients, opponents, or the court.
    CORRESPONDENCE = "correspondence"
    #: Fee notes and billing documents.
    INVOICE = "invoice"
    #: Proof of identity for a party or representative.
    IDENTITY_DOCUMENT = "identity_document"
    #: Anything the categories above do not describe.
    OTHER = "other"


def _category_column() -> Mapped[DocumentCategory]:
    """The shared ``category`` column definition."""
    return mapped_column(
        Enum(
            DocumentCategory,
            name="document_category",
            # Persist the enum *values* ("court_decision") rather than the Python
            # member names, matching `user_role`, `case_status`, and `case_priority`.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=DocumentCategory.OTHER,
        server_default=DocumentCategory.OTHER.value,
        index=True,
    )


class Document(Base):
    """A document attached to a case, in its current version."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Every document belongs to a case, so this is NOT NULL and cannot be
    # SET NULL on delete the way the case's own user references are. Cases are
    # archived rather than deleted, so CASCADE only guards against manual
    # cleanup — but a document row pointing at a case that no longer exists
    # would be unreachable by every query in the module.
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # -------------------------------------------------- current version file #
    #
    # Mirrors the row in `document_versions` whose `version` equals `version`
    # below. Written only by the document service, on upload and on replace.

    #: The name the file had on the uploader's machine, preserved verbatim (after
    #: sanitisation) because it is what the document is *called* to a human.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    #: The generated, collision-free name the object is stored under. Never
    #: derived from user input, so a crafted filename cannot influence the
    #: storage layout.
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Lowercase, without the leading dot ("pdf"). Stored separately so the
    #: "file type" filter is an equality test rather than a suffix match.
    file_extension: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    #: Derived from the extension, never from the client's Content-Type — a
    #: browser-supplied type is attacker-controlled and would decide how the
    #: preview endpoint renders the response.
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Size in bytes. ``BigInteger`` because a 4-byte column caps out at 2 GB,
    #: which a scanned case bundle can legitimately approach.
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    storage_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)

    # ------------------------------------------------------------- metadata #
    #
    # Survives a replacement: the classification and the summary describe the
    # document, not the particular file currently standing in for it.

    category: Mapped[DocumentCategory] = _category_column()

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Current version number, starting at 1 and incremented by every
    #: replacement. Also the join key into :class:`DocumentVersion`.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", index=True
    )

    #: Who uploaded the file that is *currently* the document — one more mirrored
    #: field, so the rule "the document row describes its current version" has no
    #: exceptions. The original uploader stays visible as version 1's uploader.
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    #: Soft delete. Set to the deletion time; the row stays, and the object stays
    #: in MinIO, because `code-standards.md` forbids permanently destroying a
    #: legal document. A future cleanup job is what eventually reclaims the
    #: storage — see ``services/document_storage.py``.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # -------------------------------------------------------- relationships #

    case: Mapped[Case] = relationship("Case", lazy="selectin")

    uploader: Mapped[User | None] = relationship(
        "User", foreign_keys=[uploaded_by], lazy="selectin"
    )

    #: Every version ever uploaded, oldest first. ``selectin`` for the same
    #: reason the case relationships use it: the version count is shown on every
    #: row of the document list, so a lazy load would be one query per document.
    versions: Mapped[list[DocumentVersion]] = relationship(
        "DocumentVersion",
        back_populates="document",
        order_by="DocumentVersion.version",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # ------------------------------------------------------------- derived #

    @property
    def is_deleted(self) -> bool:
        """Whether the document has been soft-deleted."""
        return self.deleted_at is not None

    @property
    def current_version(self) -> DocumentVersion | None:
        """The version row the document's binary columns mirror."""
        return next((entry for entry in self.versions if entry.version == self.version), None)

    @property
    def uploaded_at(self) -> datetime:
        """When the *current* version was uploaded.

        Distinct from :attr:`created_at`, which records when the document was
        first added: after a replacement the two differ, and a document list that
        showed the original date beside the current uploader would be misleading.
        Falls back to :attr:`created_at` when no version row is loaded.
        """
        current = self.current_version
        return current.created_at if current is not None else self.created_at

    @property
    def version_count(self) -> int:
        """How many versions this document has."""
        return len(self.versions)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Filenames can name a client or a matter, so they stay out of reprs and
        # therefore out of any log line that interpolates a document.
        return f"<Document id={self.id!s} case_id={self.case_id!s} version={self.version}>"


class DocumentVersion(Base):
    """One uploaded file in a document's history.

    Written once, never updated: `architecture.md` invariant 6 makes an uploaded
    document immutable, and a replacement adds a row rather than editing one. Its
    ``storage_key`` is unique to the version, which is what makes "never overwrite
    previous files" a property of the storage layout rather than a rule someone
    has to remember.
    """

    __tablename__ = "document_versions"

    # One row per (document, version). The service reads the highest version and
    # writes the next, which is a read-then-write: without this constraint two
    # simultaneous replacements could both commit version N + 1 and silently lose
    # one of the two files from the history.
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_document_versions_document_id_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: 1 for the original upload, incremented by each replacement. Unique per
    #: document — enforced by a composite index, not only by the service, because
    #: two concurrent replacements can otherwise both read version N and both
    #: write N + 1.
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_extension: Mapped[str] = mapped_column(String(20), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    storage_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: When this version was uploaded. The "Upload Date" the spec's version
    #: history displays.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship("Document", back_populates="versions")

    uploader: Mapped[User | None] = relationship(
        "User", foreign_keys=[uploaded_by], lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DocumentVersion document_id={self.document_id!s} version={self.version}>"
