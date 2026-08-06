"""Document-indexing request and response schemas.

Two responsibilities, the same two the case, document, and OCR schemas carry:

* **Validation** — the list query's page bounds, filters, and sort are enforced
  here, so routes stay thin and every rejection comes back in the standard
  envelope with a per-field message.
* **Serialization** — a run is returned with the derived values a client would
  otherwise compute itself and get subtly wrong: whether it is still in flight,
  how long it took in seconds, whether a re-index would be accepted.

**No schema here carries a chunk, a vector, or a passage of text.** That is the
spec's scope boundary made concrete: this feature reports *that* a document was
indexed and with what, and reading the index back is Semantic Search's job. A
payload that returned chunks would be a retrieval API with a different name.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field

from models.indexing import IndexStatus
from schemas.case import SortOrder

#: Default and maximum page sizes for the index list. The ceiling exists so a
#: single request cannot be used to enumerate every index on the platform.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "IndexDocumentSummary",
    "IndexListQuery",
    "IndexMetricsQuery",
    "IndexMetricsRead",
    "IndexRead",
    "IndexResultPage",
    "IndexSortField",
    "SortOrder",
]


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class IndexDocumentSummary(BaseModel):
    """The document a run belongs to, as shown beside the run.

    Narrow on purpose, for the same reason as
    :class:`~schemas.ocr.OcrDocumentSummary`: an indexing payload should not
    become a second way to read a document record, complete with its storage
    location, when what a status panel needs is a name and a case to link to.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique document identifier.")
    case_id: uuid.UUID = Field(description="Case the document is filed under.")
    original_filename: str = Field(description="Name the document is known by.")
    file_extension: str = Field(description="Lowercase file extension, without the dot.")


class IndexRead(BaseModel):
    """One indexing run's status and metadata.

    Everything the run recorded — status, start and finish times, duration, how
    many chunks and pages it produced, which model and which collection, the
    chunking settings it used — and **no chunk text and no vectors**: those live
    in Qdrant, and reading them back is Semantic Search's responsibility rather
    than this feature's.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique identifier of this indexing run.")
    document_id: uuid.UUID = Field(description="Document whose text was indexed.")
    document_version: int = Field(
        description=(
            "Version of the document that was indexed. A replacement produces a new version and "
            "therefore its own index; earlier versions keep theirs."
        )
    )
    case_id: uuid.UUID = Field(description="Case the document is filed under.")
    document: IndexDocumentSummary | None = Field(
        default=None, description="The document this run belongs to."
    )

    status: IndexStatus = Field(description="Lifecycle state of the run.")

    chunk_count: int | None = Field(
        default=None, description="Passages the text was divided into, and therefore vectors stored."
    )
    page_count: int | None = Field(
        default=None,
        description=(
            "Pages of extracted text the run read. Lower than the document's page count when a "
            "page yielded no text — a blank separator sheet produces no passage."
        ),
    )
    character_count: int | None = Field(
        default=None, description="Total characters fed to the chunker."
    )

    embedding_model: str | None = Field(
        default=None, description="Model that produced the vectors."
    )
    embedding_dimensions: int | None = Field(
        default=None, description="Width of those vectors."
    )
    vector_collection: str | None = Field(
        default=None, description="Collection the vectors were written to."
    )
    chunk_size: int | None = Field(
        default=None, description="Target passage length this run used, in characters."
    )
    chunk_overlap: int | None = Field(
        default=None, description="Characters each passage repeated from its predecessor."
    )
    detected_language: str | None = Field(
        default=None, description="Dominant language of the indexed text, as an ISO 639-1 code."
    )

    started_at: datetime | None = Field(default=None, description="When indexing began.")
    finished_at: datetime | None = Field(
        default=None, description="When the run reached a terminal state, whichever one."
    )
    duration_ms: int | None = Field(
        default=None, description="Wall-clock duration of the run, in milliseconds."
    )
    attempt_count: int = Field(
        description="How many times indexing has been attempted, including the first."
    )

    error_code: str | None = Field(
        default=None, description="Machine-readable failure cause, when the run failed."
    )
    error_message: str | None = Field(
        default=None, description="Human-readable explanation of the failure."
    )

    requested_by: uuid.UUID | None = Field(
        default=None,
        description="Who last requested this run. Absent for the run a completed extraction schedules.",
    )

    created_at: datetime = Field(description="When the run was first queued.")
    updated_at: datetime = Field(description="When the run was last modified.")

    @computed_field(  # type: ignore[prop-decorator]
        description="Whether the run has finished, successfully or not.",
    )
    @property
    def is_terminal(self) -> bool:
        """Derived rather than stored, so it cannot disagree with `status`."""
        return self.status in {IndexStatus.INDEXED, IndexStatus.FAILED}

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Whether the run is queued or indexing. Clients should keep polling while true."
        ),
    )
    @property
    def is_active(self) -> bool:
        return self.status in {IndexStatus.PENDING, IndexStatus.INDEXING}

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Whether this document can be re-indexed now. False while it is queued or indexing — "
            "so a client never offers a Re-index the API would answer with 409."
        ),
    )
    @property
    def can_reindex(self) -> bool:
        """Computed from the same transition table the service enforces."""
        from core.indexing import can_reindex

        return can_reindex(self.status)

    @computed_field(  # type: ignore[prop-decorator]
        description="Duration in seconds, rounded to two decimals, for display.",
    )
    @property
    def duration_seconds(self) -> float | None:
        """Formatted once, server-side, so every surface expresses it identically."""
        if self.duration_ms is None:
            return None
        return round(self.duration_ms / 1000, 2)


class IndexResultPage(BaseModel):
    """One page of the indexing-run list."""

    items: list[IndexRead] = Field(description="Runs on this page.")
    total_records: int = Field(description="Total runs matching the filters, across all pages.")
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of runs per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(
        cls, items: list[IndexRead], *, total: int, page: int, page_size: int
    ) -> IndexResultPage:
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


class IndexMetricsRead(BaseModel):
    """Platform-wide indexing health, as the spec's "Monitoring" section describes it.

    The four figures it names — indexed documents, indexed chunks, average
    indexing duration, and failures — plus the counts and the collection state
    behind them. Rates are computed over **finished** runs only: including queued
    and in-flight work would make the success rate dip on every upload and
    recover as it processed, which measures traffic rather than quality.
    """

    window_days: int | None = Field(
        default=None,
        description="Size of the window these figures cover. Absent means the whole history.",
    )

    total_runs: int = Field(description="Every recorded run in the window.")
    pending: int = Field(description="Runs queued but not yet started.")
    indexing: int = Field(description="Runs currently indexing.")
    indexed: int = Field(description="Documents successfully indexed.")
    failed: int = Field(description="Runs that ended without a usable index.")

    total_chunks: int = Field(
        description="Chunks across every successful run — the vectors this platform has stored."
    )

    success_rate: float = Field(
        description="Percentage of finished runs that succeeded, 0-100. `0` when none have finished."
    )
    failure_rate: float = Field(
        description="Percentage of finished runs that failed, 0-100. Complements `success_rate`."
    )
    average_duration_ms: float | None = Field(
        default=None,
        description=(
            "Mean wall-clock duration of successful runs, in milliseconds. Failed runs are "
            "excluded: a timeout would answer a different question."
        ),
    )
    failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Failed runs grouped by cause. A failure rate says something is wrong; this says "
            "what — an unreachable vector database and a stack of documents with no extracted "
            "text read identically otherwise."
        ),
    )

    embedding_model: str = Field(description="Model this deployment is configured to embed with.")
    embedding_dimensions: int = Field(description="Width of that model's vectors.")
    embedding_available: bool = Field(
        description="Whether that model can actually load here. False means new work will fail."
    )

    chunker: str = Field(description="Chunking strategy this deployment is configured to use.")
    chunk_size: int = Field(description="Target passage length, in characters.")
    chunk_overlap: int = Field(description="Characters each passage repeats from its predecessor.")

    vector_collection: str = Field(description="Qdrant collection vectors are written to.")
    vector_store_available: bool = Field(
        description="Whether the vector database answers. False means new work will fail."
    )
    vector_collection_exists: bool = Field(
        description="Whether the collection has been created. Distinct from holding no vectors."
    )
    stored_vectors: int | None = Field(
        default=None,
        description=(
            "Vectors currently in the collection, as the vector database reports them. Absent "
            "when it is unreachable — which is not the same as zero."
        ),
    )

    enabled: bool = Field(description="Whether new indexing work is being scheduled at all.")

    @computed_field(  # type: ignore[prop-decorator]
        description="Runs that reached a terminal state — the denominator of both rates.",
    )
    @property
    def finished_runs(self) -> int:
        return self.indexed + self.failed

    @computed_field(  # type: ignore[prop-decorator]
        description="Mean duration of successful runs in seconds, for display.",
    )
    @property
    def average_duration_seconds(self) -> float | None:
        if self.average_duration_ms is None:
            return None
        return round(self.average_duration_ms / 1000, 2)

    @computed_field(  # type: ignore[prop-decorator]
        description="Mean chunks per successfully indexed document.",
    )
    @property
    def average_chunks_per_document(self) -> float | None:
        """Derived rather than stored: it is the ratio of two figures already here.

        ``None`` rather than ``0`` when nothing has been indexed — an average
        over no documents is undefined, and reporting zero would read as
        "documents are being indexed and producing nothing".
        """
        if self.indexed <= 0:
            return None
        return round(self.total_chunks / self.indexed, 2)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class IndexSortField(StrEnum):
    """Sortable columns of the indexing-run list."""

    CREATED_AT = "created_at"
    STARTED_AT = "started_at"
    FINISHED_AT = "finished_at"
    DURATION_MS = "duration_ms"
    CHUNK_COUNT = "chunk_count"
    STATUS = "status"


class IndexListQuery(BaseModel):
    """Validated query parameters for ``GET /indexing``.

    Every filter is optional and they combine with AND, exactly as the case,
    document, and OCR lists do.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Runs per page (max {MAX_PAGE_SIZE}).",
    )
    status: IndexStatus | None = Field(default=None, description="Only runs in this state.")
    document_id: uuid.UUID | None = Field(
        default=None, description="Only runs for this document."
    )
    case_id: uuid.UUID | None = Field(
        default=None, description="Only runs for documents filed under this case."
    )
    error_code: str | None = Field(
        default=None, max_length=50, description="Only failed runs with this cause."
    )
    embedding_model: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Only runs built with this model. Changing the embedding model requires re-indexing, "
            "and this is how the documents still on the old one are found."
        ),
    )
    sort_by: IndexSortField = Field(
        default=IndexSortField.CREATED_AT, description="Column to sort by."
    )
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="Sort direction.")

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size


class IndexMetricsQuery(BaseModel):
    """Validated query parameters for ``GET /indexing/metrics``."""

    model_config = ConfigDict(extra="forbid")

    window_days: Annotated[int | None, Field(ge=1, le=365)] = Field(
        default=None,
        description=(
            "Restrict the figures to runs queued in the last N days. Omitted covers the whole "
            "history, which is the right default for a platform that has just started indexing."
        ),
    )
