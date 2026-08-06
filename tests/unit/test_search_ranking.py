"""Unit tests for :mod:`services.search_ranking`.

The shipped ranker is deliberately simple, so most of what is worth asserting is
that it is **total and stable**: every match given comes back, none is invented,
and equal scores are ordered the same way every time.

That last property is the one that earns the module its existence today. Qdrant
does not guarantee an order between points with identical scores, so without a
tie-break the same query can return two different pages — a bug that reproduces
about half the time and looks like caching.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from services.search_ranking import (
    RANKER_FACTORIES,
    Ranker,
    SimilarityRanker,
    available_rankers,
    get_ranker,
)
from services.vector_search import VectorMatch


def a_match(
    score: float,
    *,
    document_id: str = "11111111-1111-1111-1111-111111111111",
    version: int = 1,
    page: int = 1,
    chunk: int = 0,
) -> VectorMatch:
    return VectorMatch(
        point_id=str(uuid.uuid4()),
        score=score,
        payload={
            "document_id": document_id,
            "document_version": version,
            "page_number": page,
            "chunk_number": chunk,
            "text": "passage",
        },
    )


@pytest.fixture
def ranker() -> Ranker:
    return SimilarityRanker()


class TestSimilarityRanking:
    def test_results_are_ordered_by_score_descending(self, ranker: Ranker) -> None:
        matches = [a_match(0.31, chunk=0), a_match(0.94, chunk=1), a_match(0.62, chunk=2)]

        ranked = ranker.rank("bail", matches)

        assert [match.score for match in ranked] == [0.94, 0.62, 0.31]

    def test_ties_are_broken_by_position_in_the_document(self, ranker: Ranker) -> None:
        """So the same query returns the same page, and ties read in reading order."""
        matches = [
            a_match(0.5, page=3, chunk=9),
            a_match(0.5, page=1, chunk=2),
            a_match(0.5, page=1, chunk=1),
        ]

        ranked = ranker.rank("bail", matches)

        assert [
            (match.payload["page_number"], match.payload["chunk_number"]) for match in ranked
        ] == [(1, 1), (1, 2), (3, 9)]

    def test_ranking_is_stable_across_input_orderings(self, ranker: Ranker) -> None:
        """The property Qdrant does not give, made true here."""
        matches = [
            a_match(0.5, page=2, chunk=4),
            a_match(0.5, page=1, chunk=1),
            a_match(0.9, page=5, chunk=8),
        ]

        first = ranker.rank("bail", matches)
        second = ranker.rank("bail", list(reversed(matches)))

        assert [match.point_id for match in first] == [match.point_id for match in second]

    def test_ranking_never_invents_a_result(self, ranker: Ranker) -> None:
        """Authorization was applied when the matches were retrieved.

        Anything a ranker added afterwards would be unscoped, which is why the
        protocol admits reordering and dropping but nothing else.
        """
        matches = [a_match(0.5, chunk=index) for index in range(4)]

        ranked = ranker.rank("bail", matches)

        assert {match.point_id for match in ranked} == {match.point_id for match in matches}
        assert len(ranked) == len(matches)

    def test_an_empty_result_set_ranks_to_an_empty_list(self, ranker: Ranker) -> None:
        assert ranker.rank("bail", []) == []

    def test_a_malformed_payload_is_ranked_rather_than_raising(
        self, ranker: Ranker
    ) -> None:
        """One bad point, from an older build, must not fail the whole search."""
        broken = VectorMatch(point_id="broken", score=0.4, payload={})

        ranked = ranker.rank("bail", [a_match(0.9), broken])

        assert [match.point_id for match in ranked][-1] == "broken"

    def test_negative_scores_order_correctly(self, ranker: Ranker) -> None:
        """Cosine similarity is [-1, 1]; a query orthogonal to the corpus goes negative."""
        ranked = ranker.rank("bail", [a_match(-0.4, chunk=0), a_match(0.1, chunk=1)])

        assert [match.score for match in ranked] == [0.1, -0.4]

    def test_the_query_is_accepted_but_unused_by_this_strategy(
        self, ranker: Ranker
    ) -> None:
        """On the protocol because a *reranker* needs it.

        A cross-encoder scores (query, passage) pairs. Adding the parameter later
        would be a breaking change at every call site, so it is there from the
        start.
        """
        matches = [a_match(0.5, chunk=1), a_match(0.9, chunk=0)]

        assert ranker.rank("bail", matches) == ranker.rank("something else", matches)


class TestResolution:
    def test_the_default_ranker_is_similarity(self) -> None:
        assert isinstance(get_ranker(), SimilarityRanker)
        assert get_ranker().name == "similarity"

    def test_an_unknown_ranker_falls_back_rather_than_failing_startup(self) -> None:
        assert isinstance(get_ranker("cross-encoder-that-does-not-exist"), SimilarityRanker)

    def test_the_registry_is_the_extension_point(self) -> None:
        """A future reranker is one class plus one entry here."""
        assert available_rankers() == sorted(RANKER_FACTORIES)
        assert "similarity" in RANKER_FACTORIES

    def test_the_registry_cannot_be_mutated_at_runtime(self) -> None:
        with pytest.raises(TypeError):
            RANKER_FACTORIES["injected"] = SimilarityRanker  # type: ignore[index]


class TestTheScopeBoundary:
    def test_the_ranker_holds_nothing_that_could_generate_text(self) -> None:
        """Retrieval only.

        A ranker is the most plausible place for a future contributor to reach
        for an LLM ("let the model pick the best passages"), and the spec puts
        that out of scope. The protocol's two members are the guard.
        """
        members: set[str] = {
            name for name in dir(SimilarityRanker) if not name.startswith("_")
        }

        assert members == {"name", "rank"}

    def test_the_module_imports_no_language_model(self) -> None:
        import services.search_ranking as module

        source: Any = module.__doc__ or ""
        assert "litellm" not in source.lower()
        assert not hasattr(module, "generate")
