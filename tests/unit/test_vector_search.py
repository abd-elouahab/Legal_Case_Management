"""Unit tests for :mod:`services.vector_search`.

Two kinds of test, and the split is deliberate.

**Against the driver's own models.** The filter builder is a contract with
``qdrant_client``, and this codebase has already been burned by a hand-written
stub that accepted a shape real Qdrant rejects — the ``FilterSelector`` defect
recorded in ``progress-tracker.md``, which passed 25 unit tests and failed on the
first live call. So :class:`TestFilterConstruction` builds real
``qdrant_client.models`` objects and asserts their structure. That is the
strongest check available without a running database, and it is the one that
would have caught that bug.

**Against a stub, for the translation logic.** Error handling, the empty-scope
short circuit, and the payload projection do not touch Qdrant's request models,
so a stub is honest there.

The single most important assertion in this file is
:meth:`TestFilterConstruction.test_an_empty_case_scope_matches_nothing`: an empty
set of accessible cases must match nothing, never everything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from core.search import SearchFailureCode
from services.vector_search import (
    QdrantVectorSearcher,
    SearchFilters,
    VectorSearchError,
    get_vector_searcher,
)


class StubQdrant:
    """A stand-in for the Qdrant client, recording what it was asked."""

    def __init__(self, points: list[Any] | None = None, *, exists: bool = True) -> None:
        self.points = points or []
        self.exists = exists
        self.raises: Exception | None = None
        self.last_call: dict[str, Any] | None = None

    def get_collections(self) -> Any:
        if self.raises is not None:
            raise self.raises
        return object()

    def collection_exists(self, name: str) -> bool:
        if self.raises is not None:
            raise self.raises
        return self.exists

    def query_points(self, **kwargs: Any) -> Any:
        self.last_call = kwargs
        if self.raises is not None:
            raise self.raises

        class Response:
            def __init__(self, points: list[Any]) -> None:
                self.points = points

        return Response(self.points)


class StubPoint:
    def __init__(self, point_id: str, score: float, payload: dict[str, Any]) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload


def a_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "document_id": str(uuid.uuid4()),
        "document_version": 1,
        "case_id": str(uuid.uuid4()),
        "page_number": 1,
        "chunk_number": 0,
        "text": "Le loyer est payable d'avance.",
        "language": "fr",
        "embedding_model": "BAAI/bge-m3",
        "indexed_at": datetime.now(UTC).isoformat(),
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# The filter, against the real driver models
# --------------------------------------------------------------------------- #


class TestFilterConstruction:
    def test_an_unrestricted_filter_is_none(self) -> None:
        """Only reachable for a caller holding `cases:view-all` who filtered nothing."""
        assert QdrantVectorSearcher.build_filter(SearchFilters()) is None

    def test_the_case_scope_becomes_a_match_any_condition(self) -> None:
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        case_ids = {uuid.uuid4(), uuid.uuid4()}
        built = QdrantVectorSearcher.build_filter(SearchFilters(case_ids=frozenset(case_ids)))

        assert isinstance(built, Filter)
        assert built.must is not None
        condition = built.must[0]
        assert isinstance(condition, FieldCondition)
        assert condition.key == "case_id"
        assert isinstance(condition.match, MatchAny)
        assert set(condition.match.any) == {str(case_id) for case_id in case_ids}

    def test_an_empty_case_scope_matches_nothing(self) -> None:
        """**The most important assertion in this feature.**

        A lawyer assigned to no cases must search nothing. If the empty set were
        confused with "no restriction", they would search the entire platform —
        a silent, total authorization failure. The two are different values and
        :meth:`SearchFilters.matches_nothing` names the distinction.
        """
        filters = SearchFilters(case_ids=frozenset())

        assert filters.matches_nothing() is True
        assert QdrantVectorSearcher(client=StubQdrant()).search(
            [0.1] * 8, filters=filters, limit=10
        ) == []

    def test_no_case_scope_is_not_the_same_value_as_an_empty_one(self) -> None:
        assert SearchFilters(case_ids=None).matches_nothing() is False
        assert SearchFilters(case_ids=frozenset()).matches_nothing() is True

    def test_an_empty_document_filter_matches_nothing(self) -> None:
        """A category filter that resolved to no documents must return nothing."""
        assert SearchFilters(document_ids=frozenset()).matches_nothing() is True

    def test_every_condition_lands_in_must(self) -> None:
        """The spec's "metadata filtering cannot bypass permissions", structurally.

        With every condition ANDed and no ``should`` branch, no combination of
        user filters can widen the case scope — there is nowhere for one to land
        that would be ORed with it.
        """
        built = QdrantVectorSearcher.build_filter(
            SearchFilters(
                case_ids=frozenset({uuid.uuid4()}),
                document_ids=frozenset({uuid.uuid4()}),
                document_version=2,
                languages=frozenset({"fr", "ar"}),
                embedding_model="BAAI/bge-m3",
                indexed_from=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

        assert built is not None
        assert built.should is None
        assert built.must_not is None
        assert len(built.must) == 6

    def test_the_version_filter_matches_an_integer_not_a_string(self) -> None:
        """`document_version` is stored as a number; a string would match nothing."""
        from qdrant_client.models import MatchValue

        built = QdrantVectorSearcher.build_filter(SearchFilters(document_version=2))

        assert built is not None
        match = built.must[0].match
        assert isinstance(match, MatchValue)
        assert match.value == 2
        assert not isinstance(match.value, str)

    def test_the_date_filter_uses_a_datetime_range(self) -> None:
        """`indexed_at` is an RFC 3339 *string*.

        A numeric ``Range`` over it matches nothing at all rather than failing,
        which is the worst possible outcome: it looks exactly like "no results".
        """
        from qdrant_client.models import DatetimeRange

        built = QdrantVectorSearcher.build_filter(
            SearchFilters(
                indexed_from=datetime(2026, 1, 1, tzinfo=UTC),
                indexed_to=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )

        assert built is not None
        condition = built.must[0]
        assert condition.key == "indexed_at"
        assert isinstance(condition.range, DatetimeRange)

    def test_the_filter_keys_are_the_payload_keys_indexing_writes(self) -> None:
        """A key that does not exist on a point silently matches nothing.

        Built against the indexer's own payload builder rather than against a
        list written out here, so a rename on either side fails this test rather
        than producing an empty result set in production.
        """
        from services.vector_store import build_payload

        payload = build_payload(
            document_id=uuid.uuid4(),
            document_version=1,
            case_id=uuid.uuid4(),
            page_number=1,
            chunk_number=0,
            text="passage",
            language="fr",
            embedding_model="BAAI/bge-m3",
        )

        built = QdrantVectorSearcher.build_filter(
            SearchFilters(
                case_ids=frozenset({uuid.uuid4()}),
                document_ids=frozenset({uuid.uuid4()}),
                document_version=1,
                languages=frozenset({"fr"}),
                embedding_model="BAAI/bge-m3",
                indexed_from=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

        assert built is not None
        for condition in built.must:
            assert condition.key in payload

    def test_the_scope_is_serialized_deterministically(self) -> None:
        """Same scope, same request — which makes a failure reproducible from a log."""
        case_ids = frozenset({uuid.uuid4() for _ in range(5)})

        first = QdrantVectorSearcher.build_filter(SearchFilters(case_ids=case_ids))
        second = QdrantVectorSearcher.build_filter(SearchFilters(case_ids=case_ids))

        assert first is not None and second is not None
        assert first.must[0].match.any == second.must[0].match.any


# --------------------------------------------------------------------------- #
# Retrieval and error translation
# --------------------------------------------------------------------------- #


class TestSearch:
    def test_matches_are_projected_from_the_points(self) -> None:
        payload = a_payload()
        client = StubQdrant([StubPoint("point-1", 0.83, payload)])

        matches = QdrantVectorSearcher(client=client).search(
            [0.1] * 8, filters=SearchFilters(), limit=5
        )

        assert len(matches) == 1
        assert matches[0].point_id == "point-1"
        assert matches[0].score == pytest.approx(0.83)
        assert matches[0].payload["text"] == payload["text"]

    def test_the_stored_vector_is_never_requested(self) -> None:
        """A thousand floats per hit that nobody reads.

        Asking for them would multiply the response size by two orders of
        magnitude for no reader at all.
        """
        client = StubQdrant()
        QdrantVectorSearcher(client=client).search([0.1] * 8, filters=SearchFilters(), limit=5)

        assert client.last_call is not None
        assert client.last_call["with_vectors"] is False
        assert client.last_call["with_payload"] is True

    def test_limit_and_offset_are_passed_to_the_database(self) -> None:
        """Pagination executes in Qdrant, not by slicing a larger result in Python."""
        client = StubQdrant()
        QdrantVectorSearcher(client=client).search(
            [0.1] * 8, filters=SearchFilters(), limit=7, offset=14
        )

        assert client.last_call is not None
        assert client.last_call["limit"] == 7
        assert client.last_call["offset"] == 14

    def test_the_score_threshold_is_applied_by_the_database(self) -> None:
        client = StubQdrant()
        QdrantVectorSearcher(client=client).search(
            [0.1] * 8, filters=SearchFilters(), limit=5, score_threshold=0.4
        )

        assert client.last_call is not None
        assert client.last_call["score_threshold"] == 0.4

    def test_a_missing_collection_returns_no_results_rather_than_failing(self) -> None:
        """Nothing indexed yet is a corpus with no matches, not an outage."""
        client = StubQdrant(exists=False)

        assert (
            QdrantVectorSearcher(client=client).search(
                [0.1] * 8, filters=SearchFilters(), limit=5
            )
            == []
        )

    def test_a_driver_failure_is_translated_at_the_boundary(self) -> None:
        client = StubQdrant()
        client.raises = RuntimeError("connection refused to qdrant://internal")

        with pytest.raises(VectorSearchError) as excinfo:
            QdrantVectorSearcher(client=client).search(
                [0.1] * 8, filters=SearchFilters(), limit=5
            )

        assert excinfo.value.code is SearchFailureCode.VECTOR_STORE_UNAVAILABLE

    def test_the_driver_s_own_message_never_escapes(self) -> None:
        """It can quote the filter and the payload — the caller's cases, and the text."""
        client = StubQdrant()
        client.raises = RuntimeError("failed on payload text='Contrat de bail commercial'")

        with pytest.raises(VectorSearchError) as excinfo:
            QdrantVectorSearcher(client=client).search(
                [0.1] * 8, filters=SearchFilters(), limit=5
            )

        assert "Contrat de bail" not in str(excinfo.value)

    def test_availability_is_probed(self) -> None:
        client = StubQdrant()
        searcher = QdrantVectorSearcher(client=client)

        assert searcher.is_available() is True

        client.raises = RuntimeError("down")
        assert searcher.is_available() is False


class TestTheReadWriteBoundary:
    def test_the_searcher_cannot_write(self) -> None:
        """The mirror image of the store having no query method.

        ``10-document-indexing.md`` made "indexing does not retrieve" structural;
        this keeps "retrieval does not index" structural in the same way. Neither
        half can do the other's job by accident.
        """
        members = {
            name
            for name in dir(QdrantVectorSearcher)
            if not name.startswith("_")
        }

        assert members == {"build_filter", "collection", "is_available", "name", "search"}

    def test_the_vector_store_still_has_no_query_method(self) -> None:
        """Re-asserted here, from the other side.

        Semantic Search shipping is exactly when someone would be tempted to add
        a ``search`` to the write-side protocol and delete this module.
        """
        from services.vector_store import VectorStore

        members = {name for name in dir(VectorStore) if not name.startswith("_")}

        assert members == {
            "collection",
            "collection_info",
            "count_document_version",
            "delete_document_version",
            "ensure_collection",
            "is_available",
            "upsert",
        }


class TestResolution:
    def test_the_default_searcher_is_qdrant(self) -> None:
        assert isinstance(get_vector_searcher(), QdrantVectorSearcher)

    def test_the_collection_can_be_overridden(self) -> None:
        assert get_vector_searcher("other-collection").collection == "other-collection"
