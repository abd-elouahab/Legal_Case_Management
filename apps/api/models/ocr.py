"""OCR ORM models.

The first stage of the AI pipeline: the machine-readable text extracted from an
uploaded document, and the record of the extraction that produced it.

Two tables, because an OCR run has two distinct things to say:

* :class:`OcrResult` is the *run* — its lifecycle state, when it started and
  finished, which engine did the work, and what that engine reported about the
  document (page count, language, confidence). One row per **document version**,
  which is what makes retrying idempotent: a retry re-uses the row rather than
  appending a second, contradictory verdict about the same bytes.
* :class:`OcrPage` is the *text*, one immutable row per page. Storing pages
  rather than one concatenated blob is what preserves the page order and the
  page boundaries ``09-ocr-processing.md`` requires — a boundary encoded as a
  separator inside a single string is a convention a later reader has to know
  about, whereas a row per page is one the database enforces.

The extracted text lives here, in PostgreSQL, **separately from the original
file**, which stays untouched in MinIO. Nothing in this module holds file
content: OCR reads the stored object through
:class:`~services.document_storage.DocumentStorageService` and writes back only
text.

No authorization logic lives here. Whether a caller may use the OCR capabilities
is decided by :mod:`core.roles`; whether they may reach *this* result is decided
by :mod:`services.ocr_access`, which defers to the document, which defers to its
case.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
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


class OcrStatus(StrEnum):
    """Lifecycle state of one OCR run.

    Exactly the four states ``09-ocr-processing.md`` lists. The legal moves
    between them are declared once, in :data:`~core.ocr.STATUS_TRANSITIONS`, and
    every write goes through :meth:`~services.ocr.OcrService` — so a run cannot,
    for example, arrive at ``COMPLETED`` without having been ``PROCESSING``.
    """

    #: Queued, not yet picked up by a worker.
    PENDING = "pending"
    #: Claimed by a worker and currently extracting.
    PROCESSING = "processing"
    #: Text was extracted and persisted.
    COMPLETED = "completed"
    #: The run ended without usable text. The document and its metadata are
    #: untouched, and the run can be retried.
    FAILED = "failed"


class OcrResult(Base):
    """One OCR run over one version of one document."""

    __tablename__ = "ocr_results"

    __table_args__ = (
        # One run per document version. This is the whole of the idempotency
        # guarantee the spec asks for: a retry updates this row, and two
        # simultaneous "start OCR" requests cannot both insert. Enforced by the
        # database rather than only by the service, because the check-then-insert
        # in between is exactly where a race lives.
        UniqueConstraint(
            "document_id", "document_version", name="uq_ocr_results_document_id_document_version"
        ),
        # The monitoring endpoint aggregates by status over a time window, and
        # the worker looks for pending work by status; both read this index.
        Index("ix_ocr_results_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: The document whose file was read. CASCADE for the same reason as
    #: ``documents.case_id``: documents are soft-deleted rather than removed, so
    #: this only guards against manual cleanup — but an OCR result pointing at a
    #: document that no longer exists would be unreachable by every query here.
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Which version of the document was read. Part of the identity of the run,
    #: not a detail of it: replacing a document produces a *new* file, which
    #: needs its own extraction, while the previous version's text stays valid
    #: and readable.
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[OcrStatus] = mapped_column(
        Enum(
            OcrStatus,
            name="ocr_status",
            # Persist the enum *values* ("in_progress"-style identifiers) rather
            # than the Python member names, matching `user_role`, `case_status`,
            # `case_priority`, and `document_category`.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=OcrStatus.PENDING,
        server_default=OcrStatus.PENDING.value,
        index=True,
    )

    # ------------------------------------------------------------- the run #

    #: Identifier of the engine that produced the text ("tesseract"). Recorded
    #: per run rather than read from configuration, because configuration is
    #: current and a result is historical — a document processed last year was
    #: processed by whatever was configured then.
    engine: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: The engine's own version string, when it reports one.
    engine_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: When the worker claimed the run.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: When the run reached a terminal state, whichever one.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Wall-clock duration in milliseconds. Stored rather than derived from the
    #: two timestamps above so the monitoring aggregate is one ``AVG`` over a
    #: numeric column instead of an interval subtraction the database has to
    #: compute per row — and so a run whose timestamps were written by different
    #: clocks cannot report a negative duration.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How many times extraction has been attempted, including the first. A
    #: retry increments it, which is what makes "this document keeps failing"
    #: visible without reading the log.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # -------------------------------------------------- what the engine saw #

    #: Language the engine detected, when it can report one. Nullable because
    #: "if available" is exactly how the spec qualifies it.
    detected_language: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: Pages processed. For an image this is 1; for a PDF it is the number of
    #: rendered pages, which may be fewer than the file's if ``OCR_MAX_PAGES``
    #: capped the run.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Mean confidence over the recognised words, 0-100, when the engine reports
    #: one. ``Float`` rather than an integer percentage because the engine's own
    #: value is fractional and rounding it here would lose the only signal a
    #: reviewer has for "this scan is marginal".
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ---------------------------------------------------------- the failure #

    #: Machine-readable reason, from :class:`~core.ocr.OcrFailureCode`. Separate
    #: from the message so a client can branch on the cause without parsing
    #: prose, and so the monitoring view can group failures.
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: Human-readable explanation, safe to show an authorized user. **Never the
    #: engine's raw output**, which can quote the document's contents.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------ the actor #

    #: Who last asked for this run. ``None`` for the automatic run an upload
    #: schedules — nobody *requested* it, it is what the platform does with an
    #: uploaded document.
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

    #: The extracted text, in page order. ``selectin`` and ``order_by`` together
    #: are what make "preserve page order" a property of the mapping rather than
    #: something every reader has to remember to sort.
    pages: Mapped[list[OcrPage]] = relationship(
        "OcrPage",
        back_populates="result",
        order_by="OcrPage.page_number",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # ------------------------------------------------------------- derived #

    @property
    def is_terminal(self) -> bool:
        """Whether the run has finished, successfully or not."""
        return self.status in {OcrStatus.COMPLETED, OcrStatus.FAILED}

    @property
    def is_active(self) -> bool:
        """Whether the run is queued or currently extracting."""
        return self.status in {OcrStatus.PENDING, OcrStatus.PROCESSING}

    @property
    def character_count(self) -> int:
        """Total characters extracted across every page."""
        return sum(len(page.text) for page in self.pages)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the extracted text, and never a filename: both can quote a
        # client's business, and a repr ends up in whatever log line
        # interpolates the object.
        return (
            f"<OcrResult id={self.id!s} document_id={self.document_id!s} "
            f"version={self.document_version} status={self.status.value!r}>"
        )


class OcrPage(Base):
    """The text of one page of one OCR run.

    Written once per run and replaced wholesale when the run is retried — a page
    is not edited, because it is a machine's reading of a fixed set of bytes and
    nothing about those bytes changes between attempts.
    """

    __tablename__ = "ocr_pages"

    # One row per (run, page). Without it a retry that failed part-way through
    # could leave two rows claiming to be page 3, and "preserve page order"
    # would silently become "preserve some order".
    __table_args__ = (
        UniqueConstraint(
            "ocr_result_id", "page_number", name="uq_ocr_pages_ocr_result_id_page_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    ocr_result_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ocr_results.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: 1-based, matching how a reader numbers the pages of the document.
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The recognised text of this page, Unicode as the engine produced it (NFC
    #: normalised, see :func:`~core.ocr.normalize_extracted_text`). ``Text``
    #: rather than a capped ``String``: a dense page of Arabic legal prose is
    #: several thousand characters, and truncating it would corrupt the very
    #: thing indexing will later consume.
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    #: Mean confidence for this page, 0-100, when the engine reports one.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    result: Mapped[OcrResult] = relationship("OcrResult", back_populates="pages")

    @property
    def character_count(self) -> int:
        """How many characters this page yielded."""
        return len(self.text)

    @property
    def is_empty(self) -> bool:
        """Whether the page yielded no text at all.

        A legitimate outcome, not a failure: a blank separator sheet or a page
        holding only a photograph has nothing to read.
        """
        return not self.text.strip()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # The text itself never appears here — see OcrResult.__repr__.
        return (
            f"<OcrPage result_id={self.ocr_result_id!s} page={self.page_number} "
            f"chars={self.character_count}>"
        )
