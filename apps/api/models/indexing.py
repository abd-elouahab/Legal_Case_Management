"""Document indexing ORM model.

The second stage of the AI pipeline: the record of turning the text OCR
extracted into searchable knowledge — chunks, embeddings, and vectors persisted
in Qdrant.

**One table, deliberately.** OCR needed two (the run and the text) because the
text it produces is business data with nowhere else to live. Indexing needs one:
the chunks and their embeddings live in **Qdrant**, which is where
`architecture.md` and `code-standards.md` both say document embeddings belong,
and duplicating them into PostgreSQL would mean two stores that can disagree
about what is indexed. What PostgreSQL keeps is the *run* — its lifecycle state,
what it produced, and with which model — because that is the part a lawyer polls,
an operator monitors, and a retry re-uses, and none of it belongs in a vector
database.

:class:`DocumentIndex` is therefore the exact analogue of
:class:`~models.ocr.OcrResult`: one row per **document version**, which is what
makes re-indexing idempotent. A retry re-uses the row rather than appending a
second, contradictory verdict about the same text, and a replacement gets its own
row while the previous version keeps the index built from *it*.

No authorization logic lives here. Whether a caller may use the indexing
capabilities is decided by :mod:`core.roles`; whether they may reach *this* index
is decided by :mod:`services.indexing_access`, which defers to the document,
which defers to its case.

**Nothing here knows about retrieval.** ``10-document-indexing.md`` puts semantic
search, RAG, and the AI assistant out of scope, and the boundary is structural:
this module records what was written to Qdrant and never reads it back.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.document import Document
from models.user import User


class IndexStatus(StrEnum):
    """Lifecycle state of one indexing run.

    Exactly the four states ``10-document-indexing.md`` lists under "Index
    Status". The legal moves between them are declared once, in
    :data:`~core.indexing.STATUS_TRANSITIONS`, and every write goes through
    :class:`~services.indexing.IndexingService` — so a run cannot, for example,
    arrive at ``INDEXED`` without having been ``INDEXING``.
    """

    #: Queued, not yet picked up by a worker.
    PENDING = "pending"
    #: Claimed by a worker: chunking, embedding, or writing vectors.
    INDEXING = "indexing"
    #: Vectors are in Qdrant and the metadata describes them.
    INDEXED = "indexed"
    #: The run ended without a usable index. The OCR text and the document are
    #: untouched, and the run can be retried.
    FAILED = "failed"


class DocumentIndex(Base):
    """One indexing run over one version of one document."""

    __tablename__ = "document_indexes"

    __table_args__ = (
        # One index per document version. This is the whole of the idempotency
        # guarantee the spec asks for under "Re-indexing": a retry updates this
        # row, and two simultaneous "index this" requests cannot both insert.
        # Enforced by the database rather than only by the service, because the
        # check-then-insert in between is exactly where a race lives — the same
        # reasoning as `uq_ocr_results_document_id_document_version`.
        UniqueConstraint(
            "document_id",
            "document_version",
            name="uq_document_indexes_document_id_document_version",
        ),
        # The monitoring endpoint aggregates by status over a time window, and
        # the startup requeue looks for pending work by status; both read this.
        Index("ix_document_indexes_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: The document whose text was indexed. CASCADE for the same reason as
    #: ``ocr_results.document_id``: documents are soft-deleted rather than
    #: removed, so this only guards against manual cleanup — but an index row
    #: pointing at a document that no longer exists would be unreachable by every
    #: query here.
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Which version of the document was indexed. Part of the identity of the
    #: run, not a detail of it: replacing a document produces new text, which
    #: needs its own vectors, while the previous version's index stays valid for
    #: as long as that version is readable.
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The case the document is filed under, copied onto the row.
    #:
    #: Denormalized deliberately, and it is the one denormalization here. Every
    #: vector written to Qdrant carries a ``case_id`` in its payload so a future
    #: search can filter by it, and that value has to come from somewhere the
    #: worker can read without a join. Copying it makes the vector payload and
    #: the index row provably agree; a document never changes case, so the copy
    #: cannot go stale.
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[IndexStatus] = mapped_column(
        Enum(
            IndexStatus,
            name="index_status",
            # Persist the enum *values* rather than the Python member names,
            # matching `user_role`, `case_status`, `case_priority`,
            # `document_category`, and `ocr_status`.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=IndexStatus.PENDING,
        server_default=IndexStatus.PENDING.value,
        index=True,
    )

    # ------------------------------------------------------------- the run #

    #: When the worker claimed the run.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: When the run reached a terminal state, whichever one.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Wall-clock duration in milliseconds. Stored rather than derived from the
    #: two timestamps above so the monitoring aggregate is one ``AVG`` over a
    #: numeric column, and so a run whose timestamps were written by different
    #: clocks cannot report a negative duration.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How many times indexing has been attempted, including the first. A retry
    #: increments it, which is what makes "this document keeps failing to index"
    #: visible without reading the log.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # -------------------------------------------------- what was produced #

    #: How many chunks the text was split into, and therefore how many vectors
    #: were written. The spec's "indexed chunks" metric aggregates this column.
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How many pages of OCR text the run read. Not the same as the document's
    #: page count when a page yielded no text: a blank separator sheet produces
    #: no chunk, and pretending it did would misreport coverage.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Total characters fed to the chunker. The size of the input, never the
    #: input: document contents are never stored outside `ocr_pages` and Qdrant.
    character_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Identifier of the embedding model that produced the vectors
    #: (``BAAI/bge-m3``). Recorded per run rather than read from configuration,
    #: because configuration is current and an index is historical — and because
    #: `ai-architecture.md` states that changing embedding models requires
    #: re-indexing, which is a comparison this column is what makes possible.
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    #: Dimensionality of those vectors. Stored alongside the model because a
    #: collection is created for one width and cannot accept another.
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Qdrant collection the vectors were written to. Configuration again, and
    #: historical again: a deployment that renames its collection must still be
    #: able to tell where an existing index actually lives.
    vector_collection: Mapped[str | None] = mapped_column(String(100), nullable=True)

    #: Chunk size and overlap, in characters, this run used. Both are
    #: configurable, and both change what the vectors mean, so an index built
    #: under one setting is not comparable with one built under another.
    chunk_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_overlap: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: The dominant language of the indexed text, as
    #: :func:`~core.indexing.detect_language` reads it from the script. Chunks
    #: carry their own; this is the document-level summary a status panel shows.
    detected_language: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ---------------------------------------------------------- the failure #

    #: Machine-readable reason, from :class:`~core.indexing.IndexFailureCode`.
    #: Separate from the message so a client can branch on the cause without
    #: parsing prose, and so the monitoring view can group failures.
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: Human-readable explanation, safe to show an authorized user. **Never a
    #: library's raw message**, which can echo the text it was processing.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------ the actor #

    #: Who last asked for this run. ``None`` for the automatic run a completed
    #: extraction schedules — nobody *requested* it, it is what the platform does
    #: with text it has just read.
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # -------------------------------------------------------- relationships #

    document: Mapped[Document] = relationship("Document", lazy="selectin")

    requester: Mapped[User | None] = relationship(
        "User", foreign_keys=[requested_by], lazy="selectin"
    )

    # ------------------------------------------------------------- derived #

    @property
    def is_terminal(self) -> bool:
        """Whether the run has finished, successfully or not."""
        return self.status in {IndexStatus.INDEXED, IndexStatus.FAILED}

    @property
    def is_active(self) -> bool:
        """Whether the run is queued or currently indexing."""
        return self.status in {IndexStatus.PENDING, IndexStatus.INDEXING}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never a filename and never a fragment of the indexed text: both can
        # quote a client's business, and a repr ends up in whatever log line
        # interpolates the object.
        return (
            f"<DocumentIndex id={self.id!s} document_id={self.document_id!s} "
            f"version={self.document_version} status={self.status.value!r}>"
        )
