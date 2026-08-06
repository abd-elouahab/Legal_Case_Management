"""Unit tests for :mod:`schemas.indexing`.

Two things are asserted here that nothing else can:

* the **computed** fields cannot drift from the rules the service enforces —
  `is_active`, `can_reindex`, and the derived durations are what a client uses to
  decide whether to keep polling and whether to offer a Rebuild, and a second
  copy of those rules in the browser would be the one the user sees when the two
  disagree;
* **no payload carries a chunk, a vector, or a passage of text**. That is the
  spec's scope boundary made concrete: a schema that returned passages would be a
  retrieval API with a different name.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.indexing import can_reindex
from models.indexing import IndexStatus
from schemas.indexing import (
    MAX_PAGE_SIZE,
    IndexListQuery,
    IndexMetricsQuery,
    IndexMetricsRead,
    IndexRead,
    IndexResultPage,
    IndexSortField,
)


def read(**overrides: object) -> IndexRead:
    payload: dict[str, object] = {
        "id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "document_version": 1,
        "case_id": uuid.uuid4(),
        "status": IndexStatus.INDEXED,
        "chunk_count": 12,
        "page_count": 3,
        "character_count": 4200,
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimensions": 1024,
        "vector_collection": "document_chunks",
        "chunk_size": 1000,
        "chunk_overlap": 200,
        "detected_language": "fr",
        "duration_ms": 4200,
        "attempt_count": 1,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    payload.update(overrides)
    return IndexRead.model_validate(payload)


class TestComputedFields:
    @pytest.mark.parametrize(
        ("status", "terminal", "active"),
        [
            (IndexStatus.PENDING, False, True),
            (IndexStatus.INDEXING, False, True),
            (IndexStatus.INDEXED, True, False),
            (IndexStatus.FAILED, True, False),
        ],
    )
    def test_terminal_and_active_are_complementary(
        self, status: IndexStatus, terminal: bool, active: bool
    ) -> None:
        payload = read(status=status)
        assert payload.is_terminal is terminal
        assert payload.is_active is active

    @pytest.mark.parametrize("status", list(IndexStatus))
    def test_can_reindex_matches_the_transition_table(self, status: IndexStatus) -> None:
        # Computed from the *same* table the service enforces, so a client never
        # offers a Rebuild the API would answer with 409.
        assert read(status=status).can_reindex is can_reindex(status)

    def test_the_duration_is_formatted_once_server_side(self) -> None:
        assert read(duration_ms=4200).duration_seconds == 4.2

    def test_an_unfinished_run_has_no_duration(self) -> None:
        assert read(duration_ms=None).duration_seconds is None


class TestScopeBoundary:
    def test_no_field_carries_a_passage_or_a_vector(self) -> None:
        # The spec's scope boundary, asserted on the payload itself.
        fields = set(IndexRead.model_fields) | set(IndexRead.model_computed_fields)
        for forbidden in ("chunks", "chunk_text", "text", "passages", "vector", "vectors"):
            assert forbidden not in fields

    def test_the_payload_serialises_without_any_document_text(self) -> None:
        rendered = str(read().model_dump())
        assert "bailleur" not in rendered


class TestPage:
    def test_it_derives_the_page_count(self) -> None:
        page = IndexResultPage.build([], total=45, page=2, page_size=20)
        assert page.total_pages == 3

    def test_an_empty_result_still_reports_one_page(self) -> None:
        # So a client never renders "page 1 of 0".
        assert IndexResultPage.build([], total=0, page=1, page_size=20).total_pages == 1


class TestListQuery:
    def test_the_defaults_are_newest_first(self) -> None:
        query = IndexListQuery()
        assert query.page == 1
        assert query.sort_by is IndexSortField.CREATED_AT
        assert query.sort_order.value == "desc"

    def test_an_unknown_parameter_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            IndexListQuery(nonsense="yes")  # type: ignore[call-arg]

    def test_an_oversized_page_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            IndexListQuery(page_size=MAX_PAGE_SIZE + 1)

    def test_a_zero_page_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            IndexListQuery(page=0)

    def test_the_offset_follows_the_page(self) -> None:
        assert IndexListQuery(page=3, page_size=20).offset == 40

    def test_the_embedding_model_filter_exists(self) -> None:
        # The filter that makes "which documents still need re-indexing after a
        # model change?" answerable.
        assert IndexListQuery(embedding_model="BAAI/bge-m3").embedding_model == (
            "BAAI/bge-m3"
        )


class TestMetricsQuery:
    def test_the_window_is_optional(self) -> None:
        assert IndexMetricsQuery().window_days is None

    @pytest.mark.parametrize("value", [0, 366])
    def test_an_out_of_range_window_is_refused(self, value: int) -> None:
        with pytest.raises(ValidationError):
            IndexMetricsQuery(window_days=value)


def metrics(**overrides: object) -> IndexMetricsRead:
    payload: dict[str, object] = {
        "total_runs": 10,
        "pending": 1,
        "indexing": 1,
        "indexed": 6,
        "failed": 2,
        "total_chunks": 120,
        "success_rate": 75.0,
        "failure_rate": 25.0,
        "average_duration_ms": 4200.0,
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimensions": 1024,
        "embedding_available": True,
        "chunker": "recursive-character",
        "chunk_size": 1000,
        "chunk_overlap": 200,
        "vector_collection": "document_chunks",
        "vector_store_available": True,
        "vector_collection_exists": True,
        "stored_vectors": 120,
        "enabled": True,
    }
    payload.update(overrides)
    return IndexMetricsRead.model_validate(payload)


class TestMetrics:
    def test_finished_runs_is_the_denominator_of_both_rates(self) -> None:
        assert metrics().finished_runs == 8

    def test_the_average_is_formatted_for_display(self) -> None:
        assert metrics().average_duration_seconds == 4.2

    def test_chunks_per_document_is_derived(self) -> None:
        assert metrics(indexed=6, total_chunks=120).average_chunks_per_document == 20.0

    def test_nothing_indexed_reports_no_average_rather_than_zero(self) -> None:
        # An average over no documents is undefined; reporting zero would read as
        # "documents are being indexed and producing nothing".
        assert metrics(indexed=0, total_chunks=0).average_chunks_per_document is None

    def test_an_unreachable_vector_store_reports_no_count_rather_than_zero(self) -> None:
        # "The collection holds nothing" and "the database cannot be reached" are
        # different facts and need different responses.
        assert metrics(stored_vectors=None).stored_vectors is None

    def test_it_reports_no_document_and_no_case(self) -> None:
        rendered = str(metrics().model_dump())
        assert "document_id" not in rendered
        assert "case_id" not in rendered
        assert "filename" not in rendered
