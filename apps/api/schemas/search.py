"""Semantic-search request and response schemas.

Two responsibilities, the same two the case, document, OCR, and indexing schemas
carry:

* **Validation** — the query's length, the result limit and its ceiling, the
  pagination offset, and every metadata filter are enforced here, so the route
  stays thin and every rejection comes back in the standard envelope with a
  per-field message.
* **Serialization** — a result is returned with the seven references
  ``11-semantic-search.md`` lists, plus the passage itself, plus the derived
  values a client would otherwise compute and get subtly wrong.

**The request is a POST body, not a query string, and that is a privacy decision
rather than a stylistic one.** A URL is written to the reverse proxy's access
log, to the browser's history, and to the `Referer` header of anything the page
loads next. The spec forbids logging user queries carrying sensitive legal
information, and a query string would put every query into three logs the
application does not control. A body is carried by none of them.

**No schema here carries a prompt, an answer, a summary, or a citation
paragraph.** That is the spec's scope boundary made concrete: this feature
returns *passages that already exist in the corpus*, verbatim, with their
provenance. Anything generated from them belongs to the RAG pipeline, which is a
separate feature.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from core.config import settings
from core.search import MIN_QUERY_LENGTH, normalize_query
from models.document import DocumentCategory

__all__ = [
    "SearchDocumentSummary",
    "SearchFilterInput",
    "SearchMetricsRead",
    "SearchRequest",
    "SearchResponse",
    "SearchResultRead",
    "result_from_payload",
]


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #


class SearchFilterInput(BaseModel):
    """Metadata restrictions applied to a search.

    Nested under the request rather than flattened into it, because the two are
    different kinds of thing: ``query`` and ``limit`` describe *the search*, while
    these describe *the corpus it runs over*. The nesting is also what keeps
    adding a filter from widening the request's top level — the spec asks for
    extensibility, and a filter object is where a future one lands.

    Every filter is optional and they combine with **AND**, exactly as the case,
    document, OCR, and indexing lists do. None of them can widen the caller's
    authorization scope: that scope is applied as one more ``AND`` in the vector
    query, and there is no ``OR`` branch for a user filter to land in.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Only passages from documents filed under this case. A case the caller is not party "
            "to is refused with 403 rather than answered with an empty result set."
        ),
    )
    document_id: uuid.UUID | None = Field(
        default=None, description="Only passages from this document."
    )
    document_version: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Only passages from this version. Omitted searches every indexed version, which is "
            "the right default: a clause removed in version 2 is still findable in version 1."
        ),
    )
    languages: list[str] | None = Field(
        default=None,
        max_length=10,
        description=(
            "Only passages labelled with one of these ISO 639-1 codes. The label is per passage, "
            "so a French annex inside an Arabic filing is reachable on its own."
        ),
    )
    categories: list[DocumentCategory] | None = Field(
        default=None,
        max_length=20,
        description="Only passages from documents in one of these categories.",
    )
    file_types: list[str] | None = Field(
        default=None,
        max_length=20,
        description=(
            "Only passages from documents with one of these file extensions, without the dot."
        ),
    )
    indexed_from: datetime | None = Field(
        default=None, description="Only passages indexed at or after this instant."
    )
    indexed_to: datetime | None = Field(
        default=None, description="Only passages indexed at or before this instant."
    )

    @field_validator("languages", "file_types")
    @classmethod
    def _normalize_codes(cls, value: list[str] | None) -> list[str] | None:
        """Lowercase, de-duplicate, and drop blanks.

        Both are stored lowercase (an ISO 639-1 code by
        :func:`~core.indexing.detect_language`, an extension by
        :mod:`core.documents`), so a request saying ``"PDF"`` must match. An
        entirely blank list becomes ``None`` — "filter by nothing" and "do not
        filter" are the same intent, and treating the first as an empty set would
        match nothing at all.
        """
        if value is None:
            return None

        cleaned = list(
            dict.fromkeys(item.strip().lstrip(".").lower() for item in value if item.strip())
        )
        return cleaned or None

    @model_validator(mode="after")
    def _validate_date_range(self) -> SearchFilterInput:
        """Reject a reversed date range.

        An inverted range matches nothing, so without this the caller would get
        an empty result set and no reason for it — indistinguishable from a
        corpus that genuinely holds nothing.
        """
        if (
            self.indexed_from is not None
            and self.indexed_to is not None
            and self.indexed_from > self.indexed_to
        ):
            raise ValueError("indexed_from must not be later than indexed_to")
        return self

    @property
    def is_empty(self) -> bool:
        """Whether nothing at all is being filtered."""
        return not any(
            value is not None
            for value in (
                self.case_id,
                self.document_id,
                self.document_version,
                self.languages,
                self.categories,
                self.file_types,
                self.indexed_from,
                self.indexed_to,
            )
        )

    @property
    def needs_document_lookup(self) -> bool:
        """Whether a filter here can only be answered by the documents table.

        Category and file type are columns on ``documents``; the vector payload
        carries neither, because indexing stores what a *chunk* is rather than
        what its document is. A filter on either has to be resolved to a set of
        document ids before the vector query runs — see
        :meth:`~repositories.search.SearchRepository.document_ids_matching`.
        """
        return bool(self.categories or self.file_types)


class SearchRequest(BaseModel):
    """A natural-language search over indexed documents.

    The query is normalised on the way in (NFC, whitespace collapsed, control
    characters dropped) so that the text embedded here is byte-identical to the
    form the indexed passages were normalised into — an Arabic or French query
    typed with decomposed characters must embed to the same vector as the
    composed form, or a search for a word would miss the page containing it.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=settings.SEARCH_QUERY_MAX_LENGTH,
        description=(
            "What to search for, in natural language. Arabic, French, and English are supported "
            "by the same model, and a query in one language retrieves passages in the others."
        ),
    )
    limit: int = Field(
        default=settings.SEARCH_DEFAULT_LIMIT,
        ge=1,
        le=settings.SEARCH_MAX_LIMIT,
        description=(
            f"Passages to return (max {settings.SEARCH_MAX_LIMIT}). The Top-K of the spec's "
            f"vector search, configurable per request."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=settings.SEARCH_MAX_OFFSET,
        description=(
            "Passages to skip, for pagination. Present from the start so paging can be added to "
            "a client without changing this contract."
        ),
    )
    min_score: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description=(
            "Similarity floor, between -1 and 1. Omitted uses the deployment's default. Cosine "
            "similarity on unit-length vectors, so it is directly comparable between searches."
        ),
    )
    filters: SearchFilterInput = Field(
        default_factory=SearchFilterInput, description="Metadata restrictions."
    )

    @field_validator("query")
    @classmethod
    def _normalize(cls, value: str) -> str:
        """Normalise the query and reject one that carries nothing to search for.

        Rejected here rather than answered, because embedding a single character
        or a row of punctuation returns a vector near nothing in particular — and
        therefore a page of arbitrary passages that look exactly like results.
        """
        normalized = normalize_query(value)
        if len(normalized) < MIN_QUERY_LENGTH or not any(
            character.isalnum() for character in normalized
        ):
            raise ValueError(
                f"Enter a search query of at least {MIN_QUERY_LENGTH} characters."
            )
        return normalized


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #


class SearchDocumentSummary(BaseModel):
    """The document a passage came from, as shown beside it.

    Narrow on purpose, for the same reason as
    :class:`~schemas.indexing.IndexDocumentSummary`: a search response must not
    become a second, unpaginated way to read document records complete with their
    storage locations.

    It is here at all because the spec's seven fields are identifiers, and a
    result reading "document 3f2a…, page 7" is not something a lawyer can act on.
    The summary is resolved in **one** query for the whole page — never one per
    result — so it costs a round trip rather than a round trip per hit.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique document identifier.")
    case_id: uuid.UUID = Field(description="Case the document is filed under.")
    original_filename: str = Field(description="Name the document is known by.")
    file_extension: str = Field(description="Lowercase file extension, without the dot.")
    category: DocumentCategory = Field(description="Category the document is filed under.")


class SearchResultRead(BaseModel):
    """One retrieved passage and where it came from.

    Exactly the seven fields ``11-semantic-search.md`` lists under "Search
    Response" — document id, document version, case id, page number, chunk
    number, relevance score, chunk text — plus the passage's language, its rank,
    and the document summary above.

    **And deliberately nothing else.** The point identifier, the embedding model,
    the collection name, and the vector itself are all available at this layer and
    are all withheld: the spec says not to return unrelated internal metadata, and
    none of the four tells a reader anything about the passage they are reading.
    (The model and collection *are* reported — once, on the monitoring endpoint,
    where they are an operator's business rather than a result's.)
    """

    document_id: uuid.UUID = Field(description="Document this passage was taken from.")
    document_version: int = Field(
        description=(
            "Version of that document. Each version keeps its own index, so a passage is always "
            "attributed to the exact revision it appears in."
        )
    )
    case_id: uuid.UUID = Field(description="Case the document is filed under.")
    page_number: int = Field(
        description=(
            "1-based page the passage appears on. Preserved end to end by the indexer, because "
            "it is what a legal citation points at."
        )
    )
    chunk_number: int = Field(
        description="0-based position of the passage within the document, in reading order."
    )
    score: float = Field(
        description=(
            "Cosine similarity to the query, higher is closer. Comparable across searches and "
            "across months, because every vector is unit length."
        )
    )
    text: str = Field(description="The passage itself, verbatim as it was indexed.")
    language: str | None = Field(
        default=None,
        description=(
            "ISO 639-1 code the passage was labelled with, or `und` when it could not be "
            "determined. Per passage, not per document."
        ),
    )
    rank: int = Field(
        description=(
            "1-based position in this result set after ranking. Sent rather than inferred from "
            "the array index, so a client that re-sorts for display can still cite the relevance "
            "order the server produced."
        )
    )
    document: SearchDocumentSummary | None = Field(
        default=None, description="The document this passage belongs to."
    )


class SearchResponse(BaseModel):
    """One page of search results.

    Carries the figures a client needs to render a result page honestly:
    ``has_more`` rather than a total, because a vector search has no cheap exact
    total — counting every point within the similarity threshold would cost
    another full scan, and reporting a wrong total is worse than reporting none.
    """

    query: str = Field(
        description="The normalised query that was actually embedded, echoed back."
    )
    results: list[SearchResultRead] = Field(description="Passages on this page, most relevant first.")
    result_count: int = Field(description="Passages on this page.")
    limit: int = Field(description="Maximum passages requested.")
    offset: int = Field(description="Passages skipped.")
    has_more: bool = Field(
        description=(
            "Whether another page may exist. True when this page was filled, which is the "
            "honest signal a similarity search can give without a second query."
        )
    )
    duration_ms: int = Field(
        description="Wall-clock time the search took, including embedding the query."
    )
    top_score: float | None = Field(
        default=None,
        description=(
            "Best relevance on this page, or absent when nothing matched. Says whether the corpus "
            "holds anything relevant at all."
        ),
    )
    average_score: float | None = Field(
        default=None,
        description=(
            "Mean relevance on this page, or absent when nothing matched. Says whether the page "
            "as a whole is worth reading."
        ),
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Whether the search matched nothing. A successful outcome, not an error.",
    )
    @property
    def is_empty(self) -> bool:
        """Derived rather than stored, so it cannot disagree with `results`."""
        return not self.results


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class SearchMetricsRead(BaseModel):
    """Platform-wide search health, as the spec's "Monitoring" section describes it.

    The four figures it names — **search count, average latency, average
    relevance score, and failed searches** — plus the rates and the configuration
    behind them.

    ``since`` is not decoration. These counters live in the API process rather
    than in a table (see :mod:`services.search_metrics` for why), so they reset
    when it restarts and each instance counts only its own traffic. Reporting the
    start of the window is what keeps that limitation visible instead of making
    the numbers quietly mean less than they appear to.
    """

    since: datetime = Field(
        description=(
            "When this process started counting. The figures cover traffic since then, on this "
            "instance only."
        )
    )

    total_searches: int = Field(description="Searches attempted in the window.")
    successful_searches: int = Field(
        description=(
            "Searches that answered. A search matching nothing counts here: an empty corpus is "
            "an answer, not a fault."
        )
    )
    failed_searches: int = Field(
        description="Searches that could not be answered because a dependency was unavailable."
    )

    success_rate: float = Field(description="Percentage of searches that answered, 0-100.")
    failure_rate: float = Field(description="Percentage that failed. Complements `success_rate`.")

    average_latency_ms: float | None = Field(
        default=None,
        description=(
            "Mean wall-clock duration of successful searches. Failures are excluded: a search "
            "that timed out against an unreachable database would make the platform look slow "
            "when what it is, is down."
        ),
    )
    average_score: float | None = Field(
        default=None,
        description=(
            "Mean relevance across every result returned, weighted by result count. The spec's "
            "average relevance score."
        ),
    )
    total_results: int = Field(description="Passages returned across every successful search.")
    average_results: float | None = Field(
        default=None, description="Mean passages returned per successful search."
    )

    failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Failed searches grouped by cause. A failure rate says something is wrong; this says "
            "what — a missing embedding model and an unreachable vector database read identically "
            "otherwise."
        ),
    )

    embedding_model: str = Field(
        description="Model queries are embedded with. Must be the model the corpus was indexed with."
    )
    embedding_dimensions: int = Field(description="Width of that model's vectors.")
    embedding_available: bool = Field(
        description="Whether that model can actually load here. False means every search will fail."
    )

    vector_collection: str = Field(description="Qdrant collection searched.")
    vector_store_available: bool = Field(
        description="Whether the vector database answers. False means every search will fail."
    )

    ranker: str = Field(description="Ranking strategy this deployment is configured to use.")
    default_limit: int = Field(description="Results returned when a request does not ask.")
    max_limit: int = Field(description="Most results a single request may ask for.")
    min_score: float = Field(description="Similarity floor applied when a request does not set one.")
    enabled: bool = Field(description="Whether searching is permitted at all on this deployment.")

    @computed_field(  # type: ignore[prop-decorator]
        description="Mean latency of successful searches in seconds, for display.",
    )
    @property
    def average_latency_seconds(self) -> float | None:
        """Formatted once, server-side, so every surface expresses it identically."""
        if self.average_latency_ms is None:
            return None
        return round(self.average_latency_ms / 1000, 3)


def result_from_payload(
    payload: dict[str, Any], *, score: float, rank: int
) -> dict[str, Any]:
    """Project a vector payload onto the fields a result exposes.

    Built here rather than in the service so the response contract lives with the
    schemas, and so the **exclusion** is written once: the payload also carries
    ``embedding_model`` and ``indexed_at``, and neither is copied out. A projection
    that named the fields to *drop* would leak a new one the day indexing adds it;
    this one names the fields to keep.
    """
    return {
        "document_id": payload["document_id"],
        "document_version": payload["document_version"],
        "case_id": payload["case_id"],
        "page_number": payload["page_number"],
        "chunk_number": payload["chunk_number"],
        "text": payload.get("text", ""),
        "language": payload.get("language"),
        "score": score,
        "rank": rank,
    }
