"""OCR request and response schemas.

Two responsibilities, the same two the case and document schemas carry:

* **Validation** — the list query's page bounds, filters, and sort are enforced
  here, so routes stay thin and every rejection comes back in the standard
  envelope with a per-field message.
* **Serialization** — a run is returned with the derived values a client would
  otherwise compute itself and get subtly wrong: whether it is still in flight,
  how long it took in seconds, how much text it produced.

**Status and the text are two different responses**, deliberately. A document
list polling for "has extraction finished?" must not drag a hundred pages of
recognised prose across the wire on every tick, and a client rendering the text
does not need it re-serialised on each poll. :class:`OcrResultRead` carries no
text at all; :class:`OcrTextRead` is the separate, explicit request for it.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field

from core.ocr import PAGE_SEPARATOR, join_pages, sorted_supported_extensions
from models.ocr import OcrStatus
from schemas.case import SortOrder

#: Default and maximum page sizes for the OCR run list. The ceiling exists so a
#: single request cannot be used to enumerate every run on the platform.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "OcrDocumentSummary",
    "OcrListQuery",
    "OcrMetricsQuery",
    "OcrMetricsRead",
    "OcrPageRead",
    "OcrResultPage",
    "OcrResultRead",
    "OcrSortField",
    "OcrTextRead",
    "SortOrder",
]


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class OcrDocumentSummary(BaseModel):
    """The document a run belongs to, as shown beside the run.

    Narrow on purpose, for the same reason as
    :class:`~schemas.document.DocumentCaseSummary`: an OCR payload should not
    become a second way to read a document record, complete with its storage
    location, when what a status panel needs is a name and a case to link to.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique document identifier.")
    case_id: uuid.UUID = Field(description="Case the document is filed under.")
    original_filename: str = Field(description="Name the document is known by.")
    file_extension: str = Field(description="Lowercase file extension, without the dot.")


class OcrPageRead(BaseModel):
    """One page of extracted text."""

    model_config = ConfigDict(from_attributes=True)

    page_number: int = Field(description="1-based page number, matching the document.")
    text: str = Field(description="Recognised text of this page, Unicode, NFC-normalised.")
    confidence: float | None = Field(
        default=None, description="Mean recognition confidence for this page, 0-100, if reported."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="How many characters this page yielded.",
    )
    @property
    def character_count(self) -> int:
        """Derived, so a client's progress indicator cannot disagree with the text."""
        return len(self.text)

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Whether the page yielded no text. A legitimate outcome — a separator sheet or a "
            "page holding only a photograph has nothing to read — not a failure."
        ),
    )
    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class OcrResultRead(BaseModel):
    """One OCR run's status and metadata, **without** the extracted text.

    This is what a status panel polls. Everything the spec's "OCR Metadata"
    section lists is here — status, start and finish times, duration, engine,
    detected language, page count, confidence — and the text is one request
    further on, at ``GET /documents/{id}/ocr/text``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique identifier of this extraction run.")
    document_id: uuid.UUID = Field(description="Document whose file was read.")
    document_version: int = Field(
        description=(
            "Version of the document that was read. A replacement produces a new version and "
            "therefore its own extraction; earlier versions keep theirs."
        )
    )
    document: OcrDocumentSummary | None = Field(
        default=None, description="The document this run belongs to."
    )

    status: OcrStatus = Field(description="Lifecycle state of the run.")

    engine: str | None = Field(
        default=None, description="Identifier of the engine that produced the text."
    )
    engine_version: str | None = Field(
        default=None, description="Version the engine reported, if it reports one."
    )
    detected_language: str | None = Field(
        default=None, description="Language the engine worked in, if available."
    )
    page_count: int | None = Field(
        default=None, description="Pages processed. May be capped by the platform's page limit."
    )
    confidence: float | None = Field(
        default=None, description="Mean recognition confidence across the document, 0-100."
    )

    started_at: datetime | None = Field(default=None, description="When extraction began.")
    finished_at: datetime | None = Field(
        default=None, description="When the run reached a terminal state, whichever one."
    )
    duration_ms: int | None = Field(
        default=None, description="Wall-clock duration of the run, in milliseconds."
    )
    attempt_count: int = Field(
        description="How many times extraction has been attempted, including the first."
    )

    error_code: str | None = Field(
        default=None, description="Machine-readable failure cause, when the run failed."
    )
    error_message: str | None = Field(
        default=None, description="Human-readable explanation of the failure."
    )

    requested_by: uuid.UUID | None = Field(
        default=None,
        description="Who last requested this run. Absent for the run an upload schedules.",
    )

    created_at: datetime = Field(description="When the run was first queued.")
    updated_at: datetime = Field(description="When the run was last modified.")

    @computed_field(  # type: ignore[prop-decorator]
        description="Whether the run has finished, successfully or not.",
    )
    @property
    def is_terminal(self) -> bool:
        """Derived rather than stored, so it cannot disagree with `status`."""
        return self.status in {OcrStatus.COMPLETED, OcrStatus.FAILED}

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Whether the run is queued or extracting. Clients should keep polling while true."
        ),
    )
    @property
    def is_active(self) -> bool:
        return self.status in {OcrStatus.PENDING, OcrStatus.PROCESSING}

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Whether this run can be retried now. False while it is queued or extracting — "
            "so a client never offers a Retry the API would answer with 409."
        ),
    )
    @property
    def can_retry(self) -> bool:
        """Computed from the same transition table the service enforces."""
        from core.ocr import can_retry

        return can_retry(self.status)

    @computed_field(  # type: ignore[prop-decorator]
        description="Duration in seconds, rounded to two decimals, for display.",
    )
    @property
    def duration_seconds(self) -> float | None:
        """Formatted once, server-side, so every surface expresses it identically."""
        if self.duration_ms is None:
            return None
        return round(self.duration_ms / 1000, 2)


class OcrTextRead(BaseModel):
    """The text one OCR run extracted, page by page.

    Requested explicitly rather than folded into :class:`OcrResultRead`, because
    it is the large half of the payload and the half a polling client does not
    want.
    """

    model_config = ConfigDict(from_attributes=True)

    ocr_result_id: uuid.UUID = Field(description="The run that produced this text.")
    document_id: uuid.UUID = Field(description="Document the text was extracted from.")
    document_version: int = Field(description="Version of the document that was read.")
    status: OcrStatus = Field(description="Lifecycle state of the run.")
    detected_language: str | None = Field(
        default=None, description="Language the engine worked in, if available."
    )
    pages: list[OcrPageRead] = Field(
        default_factory=list,
        description="Extracted text in page order, one entry per page of the document.",
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="How many pages of text this run produced.",
    )
    @property
    def page_count(self) -> int:
        return len(self.pages)

    @computed_field(  # type: ignore[prop-decorator]
        description="Total characters extracted across every page.",
    )
    @property
    def character_count(self) -> int:
        return sum(len(page.text) for page in self.pages)

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Every page joined with U+000C FORM FEED, the conventional plain-text page break. "
            "Splitting on that character recovers exactly the `pages` array, so the joined form "
            "loses no boundary."
        ),
    )
    @property
    def full_text(self) -> str:
        """The whole document as one string, boundaries intact.

        Offered alongside the pages rather than instead of them: copying or
        searching wants one string, while indexing and citation want the page a
        passage came from. Deriving it here means the two can never disagree.
        """
        return join_pages(page.text for page in self.pages)

    @computed_field(  # type: ignore[prop-decorator]
        description="The character `full_text` uses to separate pages.",
    )
    @property
    def page_separator(self) -> str:
        """Published rather than assumed, so a client can split without hardcoding it."""
        return PAGE_SEPARATOR


class OcrResultPage(BaseModel):
    """One page of the OCR run list."""

    items: list[OcrResultRead] = Field(description="Runs on this page.")
    total_records: int = Field(description="Total runs matching the filters, across all pages.")
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of runs per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(
        cls, items: list[OcrResultRead], *, total: int, page: int, page_size: int
    ) -> OcrResultPage:
        """Assemble a page, deriving ``total_pages`` from the total and size.

        An empty result still reports one page, so a client never renders
        "page 1 of 0".
        """
        return cls(
            items=items,
            total_records=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)) if page_size else 1,
        )


class OcrMetricsRead(BaseModel):
    """Platform-wide OCR health, as the spec's "Monitoring" section describes it.

    Rates are computed over **finished** runs only. Including queued and in-flight
    work would make the success rate dip on every upload and recover as it
    processed, which measures traffic rather than quality.
    """

    window_days: int | None = Field(
        default=None,
        description="Size of the window these figures cover. Absent means the whole history.",
    )

    total_runs: int = Field(description="Every recorded run in the window.")
    pending: int = Field(description="Runs queued but not yet started.")
    processing: int = Field(description="Runs currently extracting.")
    completed: int = Field(description="Runs that produced text.")
    failed: int = Field(description="Runs that ended without usable text.")

    success_rate: float = Field(
        description="Percentage of finished runs that completed, 0-100. `0` when none have finished."
    )
    failure_rate: float = Field(
        description="Percentage of finished runs that failed, 0-100. Complements `success_rate`."
    )
    average_duration_ms: float | None = Field(
        default=None,
        description=(
            "Mean wall-clock duration of completed runs, in milliseconds. Failed runs are "
            "excluded: a timeout would answer a different question."
        ),
    )
    failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Failed runs grouped by cause. A failure rate says something is wrong; this says "
            "what — a missing engine and a stack of unreadable scans read identically otherwise."
        ),
    )

    engine: str = Field(description="Engine identifier this deployment is configured to use.")
    engine_available: bool = Field(
        description="Whether that engine can actually run here. False means new work will fail."
    )
    enabled: bool = Field(description="Whether new extraction work is being scheduled at all.")
    supported_extensions: list[str] = Field(
        default_factory=sorted_supported_extensions,
        description="File types extraction applies to.",
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Runs that reached a terminal state — the denominator of both rates.",
    )
    @property
    def finished_runs(self) -> int:
        return self.completed + self.failed

    @computed_field(  # type: ignore[prop-decorator]
        description="Mean duration of completed runs in seconds, for display.",
    )
    @property
    def average_duration_seconds(self) -> float | None:
        if self.average_duration_ms is None:
            return None
        return round(self.average_duration_ms / 1000, 2)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class OcrSortField(StrEnum):
    """Sortable columns of the OCR run list."""

    CREATED_AT = "created_at"
    STARTED_AT = "started_at"
    FINISHED_AT = "finished_at"
    DURATION_MS = "duration_ms"
    STATUS = "status"


class OcrListQuery(BaseModel):
    """Validated query parameters for ``GET /ocr``.

    Every filter is optional and they combine with AND, exactly as the case and
    document lists do.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Runs per page (max {MAX_PAGE_SIZE}).",
    )
    status: OcrStatus | None = Field(default=None, description="Only runs in this state.")
    document_id: uuid.UUID | None = Field(default=None, description="Only runs for this document.")
    case_id: uuid.UUID | None = Field(
        default=None, description="Only runs for documents filed under this case."
    )
    error_code: str | None = Field(
        default=None, max_length=50, description="Only failed runs with this cause."
    )
    sort_by: OcrSortField = Field(
        default=OcrSortField.CREATED_AT, description="Column to sort by."
    )
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="Sort direction.")

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size


class OcrMetricsQuery(BaseModel):
    """Validated query parameters for ``GET /ocr/metrics``."""

    model_config = ConfigDict(extra="forbid")

    window_days: Annotated[int | None, Field(ge=1, le=365)] = Field(
        default=None,
        description=(
            "Restrict the figures to runs queued in the last N days. Omitted covers the whole "
            "history, which is the right default for a platform that has just started recording."
        ),
    )
