"""Unit tests for :mod:`schemas.search`.

Two things are being pinned here, and the second is the one that would be
expensive to get wrong:

* the request's validation — what a query, a limit, an offset, and a filter may
  be, so a route stays thin and every rejection is a 422 with a field name;
* the **response's field set**. ``11-semantic-search.md`` says *"Do not return
  unrelated internal metadata"*, and this is where that is asserted rather than
  hoped for: a payload key that should never leave the platform is one careless
  projection away from doing so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.config import settings
from models.document import DocumentCategory
from schemas.search import (
    SearchDocumentSummary,
    SearchFilterInput,
    SearchRequest,
    SearchResponse,
    SearchResultRead,
    result_from_payload,
)


def a_payload(**overrides: object) -> dict[str, object]:
    """A vector payload exactly as Document Indexing writes it."""
    payload: dict[str, object] = {
        "document_id": str(uuid.uuid4()),
        "document_version": 1,
        "case_id": str(uuid.uuid4()),
        "page_number": 3,
        "chunk_number": 7,
        "text": "Le loyer est payable d'avance.",
        "language": "fr",
        "embedding_model": "BAAI/bge-m3",
        "indexed_at": datetime.now(UTC).isoformat(),
    }
    payload.update(overrides)
    return payload


class TestSearchRequest:
    def test_a_minimal_request_takes_the_configured_defaults(self) -> None:
        request = SearchRequest(query="bail commercial")

        assert request.limit == settings.SEARCH_DEFAULT_LIMIT
        assert request.offset == 0
        assert request.min_score is None
        assert request.filters.is_empty is True

    def test_the_query_is_normalized_on_the_way_in(self) -> None:
        """The text embedded must be the text the passages were normalised into."""
        assert SearchRequest(query="  bail   commercial ").query == "bail commercial"

    @pytest.mark.parametrize("query", ["", " ", "a", "?", "!!!"])
    def test_a_query_with_nothing_to_search_for_is_refused(self, query: str) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query=query)

    def test_an_over_long_query_is_refused_rather_than_truncated(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="a" * (settings.SEARCH_QUERY_MAX_LENGTH + 1))

    def test_the_limit_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="bail", limit=settings.SEARCH_MAX_LIMIT + 1)
        with pytest.raises(ValidationError):
            SearchRequest(query="bail", limit=0)

    def test_the_offset_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="bail", offset=-1)
        with pytest.raises(ValidationError):
            SearchRequest(query="bail", offset=settings.SEARCH_MAX_OFFSET + 1)

    def test_the_score_floor_is_a_similarity(self) -> None:
        """Cosine similarity lives in [-1, 1]; anything else is a mistake."""
        with pytest.raises(ValidationError):
            SearchRequest(query="bail", min_score=1.5)
        with pytest.raises(ValidationError):
            SearchRequest(query="bail", min_score=-2)

    def test_an_unknown_field_is_refused(self) -> None:
        """`extra="forbid"`, so a typo in a client is a 422 rather than silence."""
        with pytest.raises(ValidationError):
            SearchRequest(query="bail", top_k=5)  # type: ignore[call-arg]


class TestSearchFilterInput:
    def test_language_and_file_type_codes_are_normalized(self) -> None:
        filters = SearchFilterInput(languages=["FR", "fr", " ar "], file_types=[".PDF", "pdf"])

        assert filters.languages == ["fr", "ar"]
        assert filters.file_types == ["pdf"]

    def test_a_blank_list_becomes_no_filter(self) -> None:
        """"Filter by nothing" and "do not filter" are the same intent.

        Treating the first as an empty set would match nothing at all, which is
        the opposite of what the caller meant.
        """
        assert SearchFilterInput(languages=["", "  "]).languages is None

    def test_a_reversed_date_range_is_refused(self) -> None:
        """It matches nothing, and an empty result with no reason is worse than a 422."""
        with pytest.raises(ValidationError):
            SearchFilterInput(
                indexed_from=datetime(2026, 6, 1, tzinfo=UTC),
                indexed_to=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_an_equal_date_range_is_accepted(self) -> None:
        instant = datetime(2026, 1, 1, tzinfo=UTC)
        assert SearchFilterInput(indexed_from=instant, indexed_to=instant).indexed_from == instant

    def test_document_lookup_is_needed_only_for_document_level_filters(self) -> None:
        """Category and file type are columns on `documents`; the payload has neither."""
        assert SearchFilterInput().needs_document_lookup is False
        assert SearchFilterInput(languages=["fr"]).needs_document_lookup is False
        assert SearchFilterInput(case_id=uuid.uuid4()).needs_document_lookup is False
        assert (
            SearchFilterInput(categories=[DocumentCategory.CONTRACT]).needs_document_lookup
            is True
        )
        assert SearchFilterInput(file_types=["pdf"]).needs_document_lookup is True

    def test_is_empty_reports_every_filter(self) -> None:
        assert SearchFilterInput().is_empty is True
        for filters in (
            SearchFilterInput(case_id=uuid.uuid4()),
            SearchFilterInput(document_id=uuid.uuid4()),
            SearchFilterInput(document_version=1),
            SearchFilterInput(languages=["fr"]),
            SearchFilterInput(categories=[DocumentCategory.EVIDENCE]),
            SearchFilterInput(file_types=["pdf"]),
            SearchFilterInput(indexed_from=datetime.now(UTC)),
            SearchFilterInput(indexed_to=datetime.now(UTC)),
        ):
            assert filters.is_empty is False


class TestResultProjection:
    def test_the_projection_keeps_exactly_the_documented_fields(self) -> None:
        payload = a_payload()
        projected = result_from_payload(payload, score=0.87, rank=1)

        assert set(projected) == {
            "document_id",
            "document_version",
            "case_id",
            "page_number",
            "chunk_number",
            "text",
            "language",
            "score",
            "rank",
        }

    def test_internal_payload_keys_are_not_projected(self) -> None:
        """The spec's "do not return unrelated internal metadata", asserted.

        The model and the indexing timestamp are on every point and are of no
        use to a reader of a passage. The model *is* reported — once, on the
        monitoring endpoint, where it is an operator's business.
        """
        projected = result_from_payload(a_payload(), score=0.5, rank=1)

        assert "embedding_model" not in projected
        assert "indexed_at" not in projected

    def test_a_result_never_carries_the_vector_or_the_point_id(self) -> None:
        """Neither is anything a reader can act on, and both are large or internal."""
        fields = set(SearchResultRead.model_fields) | set(
            SearchResultRead.model_computed_fields
        )

        assert "vector" not in fields
        assert "embedding" not in fields
        assert "point_id" not in fields
        assert "embedding_model" not in fields

    def test_a_result_carries_the_seven_references_the_spec_lists(self) -> None:
        fields = set(SearchResultRead.model_fields)

        assert {
            "document_id",
            "document_version",
            "case_id",
            "page_number",
            "chunk_number",
            "score",
            "text",
        } <= fields

    def test_the_document_summary_is_narrow(self) -> None:
        """A search response must not become a second way to read a document record."""
        assert set(SearchDocumentSummary.model_fields) == {
            "id",
            "case_id",
            "original_filename",
            "file_extension",
            "category",
        }


class TestSearchResponse:
    def _result(self, rank: int, score: float) -> SearchResultRead:
        return SearchResultRead(
            document_id=uuid.uuid4(),
            document_version=1,
            case_id=uuid.uuid4(),
            page_number=1,
            chunk_number=rank,
            score=score,
            text="passage",
            language="fr",
            rank=rank,
        )

    def test_an_empty_response_says_so(self) -> None:
        response = SearchResponse(
            query="bail",
            results=[],
            result_count=0,
            limit=10,
            offset=0,
            has_more=False,
            duration_ms=4,
        )

        assert response.is_empty is True
        assert response.top_score is None

    def test_a_populated_response_is_not_empty(self) -> None:
        response = SearchResponse(
            query="bail",
            results=[self._result(1, 0.9)],
            result_count=1,
            limit=10,
            offset=0,
            has_more=False,
            duration_ms=4,
            top_score=0.9,
            average_score=0.9,
        )

        assert response.is_empty is False

    def test_the_response_carries_no_answer_no_summary_and_no_prompt(self) -> None:
        """The scope boundary, asserted on the wire shape.

        This feature retrieves. Anything generated from what it retrieves belongs
        to the RAG pipeline, and a field for it here is how that boundary would
        quietly move.
        """
        fields = set(SearchResponse.model_fields) | set(SearchResponse.model_computed_fields)

        for forbidden in ("answer", "summary", "prompt", "completion", "citations", "message"):
            assert forbidden not in fields
