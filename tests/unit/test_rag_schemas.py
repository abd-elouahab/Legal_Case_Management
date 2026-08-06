"""Unit tests for :mod:`schemas.rag`.

Validation on the way in, and the derived values a client would otherwise compute
and get subtly wrong on the way out. Also the two *absences* that are the spec's
scope boundary made concrete: no schema here carries a conversation, and no
citation carries an internal identifier.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from core.config import settings
from schemas.rag import RagAnswerResponse, RagCitationRead, RagRequest
from schemas.search import SearchFilterInput


def citation(marker: int = 1, *, referenced: bool = True) -> RagCitationRead:
    return RagCitationRead(
        marker=marker,
        document_id=uuid.uuid4(),
        document_name="bail-commercial.pdf",
        document_version=1,
        page_number=7,
        case_id=uuid.uuid4(),
        score=0.81,
        excerpt="Le loyer mensuel est payable d'avance.",
        referenced=referenced,
    )


def answer(**fields: object) -> RagAnswerResponse:
    defaults: dict[str, object] = {
        "question": "Quand le loyer est-il payable ?",
        "answer": "Le premier jour de chaque mois [1].",
        "language": "fr",
        "grounded": True,
        "insufficient_evidence": False,
        "citations": [],
        "retrieved_count": 1,
        "context_count": 1,
        "context_characters": 120,
        "duration_ms": 900,
        "retrieval_ms": 120,
        "prompt_name": "rag/answer",
        "prompt_version": 1,
    }
    return RagAnswerResponse(**{**defaults, **fields})  # type: ignore[arg-type]


class TestRequest:
    def test_a_question_is_normalised_on_the_way_in(self) -> None:
        """The question *is* the retrieval query, so it must reach the embedding
        model in the form the indexed passages were normalised into."""
        assert RagRequest(question="  loyer   commercial  ").question == "loyer commercial"

    @pytest.mark.parametrize("value", ["", " ", "?", "a"])
    def test_a_question_with_nothing_to_answer_is_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            RagRequest(question=value)

    def test_an_over_long_question_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RagRequest(question="a" * (settings.RAG_QUESTION_MAX_LENGTH + 1))

    def test_the_question_ceiling_does_not_exceed_the_retrieval_query_ceiling(self) -> None:
        """One this endpoint accepted and search refused would fail deep inside
        the pipeline with a message about a search."""
        assert settings.RAG_QUESTION_MAX_LENGTH <= settings.SEARCH_QUERY_MAX_LENGTH

    @pytest.mark.parametrize("language", ["ar", "fr", "en", "FR", " ar "])
    def test_a_supported_language_is_accepted_and_normalised(self, language: str) -> None:
        assert RagRequest(question="Quand ?", language=language).language == language.strip().lower()

    def test_an_unsupported_language_is_refused_rather_than_ignored(self) -> None:
        """A caller who asked for German and got French would have no way to tell
        the request was understood and overruled rather than honoured."""
        with pytest.raises(ValidationError, match="Answers are available in"):
            RagRequest(question="Quand ?", language="de")

    def test_a_blank_language_is_treated_as_unset(self) -> None:
        assert RagRequest(question="Quand ?", language="  ").language is None

    def test_the_breadth_is_bounded_by_what_search_will_return(self) -> None:
        with pytest.raises(ValidationError):
            RagRequest(question="Quand ?", top_k=settings.SEARCH_MAX_LIMIT + 1)

    def test_a_similarity_floor_outside_the_cosine_range_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RagRequest(question="Quand ?", min_score=1.5)

    def test_unknown_fields_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            RagRequest(question="Quand ?", conversation_id=str(uuid.uuid4()))  # type: ignore[call-arg]

    def test_the_filters_are_the_search_filters_reused_verbatim(self) -> None:
        """A parallel filter model would be a second vocabulary to keep in step."""
        assert RagRequest.model_fields["filters"].annotation is SearchFilterInput

    def test_filters_default_to_none_at_all(self) -> None:
        assert RagRequest(question="Quand ?").filters.is_empty is True

    def test_the_request_carries_no_conversation(self) -> None:
        """`ai-architecture.md`: the RAG pipeline must never manage conversations."""
        fields = set(RagRequest.model_fields)

        assert not {"conversation_id", "messages", "history", "session_id"} & fields


class TestCitation:
    def test_it_carries_the_four_references_the_spec_names(self) -> None:
        fields = set(RagCitationRead.model_fields)

        assert {"document_id", "document_version", "page_number", "case_id"} <= fields

    def test_it_exposes_no_chunk_number_point_id_or_vector(self) -> None:
        fields = set(RagCitationRead.model_fields)

        assert not {"chunk_number", "point_id", "vector", "embedding_model"} & fields


class TestAnswer:
    def test_the_citation_count_cannot_disagree_with_the_citations(self) -> None:
        response = answer(citations=[citation(1), citation(2)])

        assert response.citation_count == 2

    def test_the_referenced_count_reports_only_what_the_prose_cited(self) -> None:
        """A grounded answer with citations but nothing referenced is a model that
        ignored the citation instruction — visible on one answer, not only in an
        aggregate."""
        response = answer(citations=[citation(1), citation(2, referenced=False)])

        assert response.citation_count == 2
        assert response.referenced_count == 1

    def test_generation_time_is_absent_when_no_model_was_called(self) -> None:
        response = answer(grounded=False, insufficient_evidence=True, generation_ms=None)

        assert response.generation_ms is None

    def test_the_response_carries_no_conversation(self) -> None:
        fields = set(RagAnswerResponse.model_fields)

        assert not {"conversation_id", "message_id", "history"} & fields

    def test_the_response_carries_no_prompt_text(self) -> None:
        """The prompt names the sources; sending it back would be a second copy of
        the evidence with none of the citation structure."""
        fields = set(RagAnswerResponse.model_fields)

        assert "prompt" not in fields
        assert "system_prompt" not in fields
