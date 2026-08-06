"""Unit tests for :class:`~services.rag.RagService`.

The pipeline is exercised against a **real** search service (real repositories,
real access policy, real ranker) over a corpus built by the **real** indexing
pipeline, with only the three genuinely external things substituted: the
embedding model, the vector database, and the language model. That matters more
here than anywhere else in the suite — this feature's entire authorization story
is "retrieval goes through the search service", and a faked search service would
make every authorization assertion below vacuous.

What is asserted, grouped by the promise it protects:

* **grounding** — an answer is built from retrieved passages, the passages
  actually reach the model, and citations point back at them;
* **no fabrication** — nothing retrieved means no model call and the platform's
  own message; the model's insufficiency sentinel is honoured; an invented
  citation marker never reaches the reader;
* **authorization** — a caller retrieves only their own cases' passages, and an
  inaccessible filter is refused rather than answered;
* **failure** — every dependency failure becomes a typed 503 with its cause, and
  is counted once;
* **privacy** — no question, passage, or answer reaches a log.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from core.config import settings
from core.exceptions import (
    CaseNotFoundError,
    InvalidQuestionError,
    RagDisabledError,
    RagUnavailableError,
    SearchAccessDeniedError,
)
from core.rag import INSUFFICIENT_EVIDENCE_MARKER, NO_EVIDENCE_MESSAGES, RagFailureCode
from models.case import Case
from models.document import Document, DocumentCategory
from models.user import User, UserRole
from schemas.rag import RagRequest
from services.embedding import EmbeddingError
from services.llm import LLMTimeoutError, LLMTransientError, LLMUnavailableError
from services.rag import citation_document_ids
from services.vector_search import VectorSearchError

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import InMemoryVectorSearcher, ScriptedLLMProvider

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeDocument = Callable[..., Document]
MakeOcrResult = Callable[..., Any]

FRENCH_PAGE = (
    "CONTRAT DE BAIL COMMERCIAL. Article 4 : Loyer et charges. Le loyer mensuel est "
    "payable d'avance le premier jour de chaque mois, au domicile du bailleur. Toute "
    "résiliation anticipée doit être notifiée par écrit avec un préavis de trois mois."
)
ARABIC_PAGE = (
    "عقد كراء تجاري. المادة الرابعة: الكراء والتحملات. يؤدى الكراء الشهري مسبقا في "
    "اليوم الأول من كل شهر بمقر المكري، ويجب إشعار الطرف الآخر كتابة قبل ثلاثة أشهر."
)

QUESTION = "Quand le loyer est-il payable ?"


# --------------------------------------------------------------------------- #
# Actors and corpus
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(email="rag-admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(email="rag-lawyer@example.com", role=UserRole.LAWYER)


@pytest.fixture
def outsider(make_user: MakeUser) -> User:
    return make_user(email="rag-outsider@example.com", role=UserRole.LAWYER)


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User) -> Case:
    return make_case(assigned_lawyer_id=lawyer.id)


@pytest.fixture
def other_case(make_case: MakeCase, outsider: User) -> Case:
    return make_case(assigned_lawyer_id=outsider.id)


@pytest.fixture
def index_document(indexing_service: Any, make_ocr_result: MakeOcrResult):  # type: ignore[no-untyped-def]
    """Push a document through the real indexing pipeline into the vector store."""

    def _index(document: Document, pages: list[str]) -> None:
        indexing_service.schedule_for_ocr_result(
            make_ocr_result(document_id=document.id, pages=pages)
        )

    return _index


@pytest.fixture
def french_contract(
    make_document: MakeDocument, legal_case: Case, index_document: Any
) -> Document:
    document = make_document(
        case_id=legal_case.id,
        original_filename="bail-commercial.pdf",
        category=DocumentCategory.CONTRACT,
    )
    index_document(document, [FRENCH_PAGE])
    return document


@pytest.fixture
def arabic_evidence(
    make_document: MakeDocument, legal_case: Case, index_document: Any
) -> Document:
    document = make_document(
        case_id=legal_case.id,
        original_filename="عقد-كراء.pdf",
        category=DocumentCategory.EVIDENCE,
    )
    index_document(document, [ARABIC_PAGE])
    return document


@pytest.fixture
def foreign_document(
    make_document: MakeDocument, other_case: Case, index_document: Any
) -> Document:
    """An indexed document on a case our lawyer is not party to."""
    document = make_document(case_id=other_case.id, original_filename="autre.pdf")
    index_document(document, [FRENCH_PAGE])
    return document


def ask(question: str = QUESTION, **fields: Any) -> RagRequest:
    return RagRequest(question=question, **fields)


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #


class TestGroundedAnswer:
    def test_a_question_is_answered_from_retrieved_passages(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.grounded is True
        assert outcome.insufficient_evidence is False
        assert outcome.answer == "Le loyer est payable le premier jour de chaque mois [1]."
        assert outcome.retrieved_count >= 1

    def test_the_retrieved_passages_actually_reach_the_model(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """The whole of 'retrieval-augmented', asserted on the prompt itself."""
        rag_service.answer(ask(), actor=lawyer)

        system, prompt = llm_provider.calls[0]
        assert "payable d'avance le premier jour" in prompt
        assert QUESTION in prompt
        assert "only the numbered sources" in system.lower()

    def test_the_model_is_called_exactly_once(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """The spec's 'avoid duplicate LLM calls'."""
        rag_service.answer(ask(), actor=lawyer)

        assert len(llm_provider.calls) == 1

    def test_the_answer_records_which_prompt_and_model_produced_it(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        """Configuration is current; an answer is historical. An evaluation run
        cannot compare two prompts unless each answer says which it used."""
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.prompt_name == settings.RAG_PROMPT_TEMPLATE
        assert outcome.prompt_version == settings.RAG_PROMPT_VERSION
        assert outcome.provider == "scripted"
        assert outcome.model == "scripted/test-model"

    def test_token_usage_is_reported_when_the_provider_supplies_it(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.prompt_tokens == 120
        assert outcome.completion_tokens == 40
        assert outcome.total_tokens == 160

    def test_the_timings_split_retrieval_from_generation(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.duration_ms >= 0
        assert outcome.retrieval_ms >= 0
        assert outcome.generation_ms is not None

    def test_a_truncated_answer_is_reported_as_truncated(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """A legal answer that ends mid-sentence must not read as a complete one."""
        llm_provider.truncated = True

        assert rag_service.answer(ask(), actor=lawyer).truncated is True


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #


class TestCitations:
    def test_every_source_is_cited_with_its_provenance(
        self, rag_service: Any, lawyer: User, french_contract: Document, legal_case: Case
    ) -> None:
        """The spec's four references: document, version, page, case."""
        citation = rag_service.answer(ask(), actor=lawyer).citations[0]

        assert citation.marker == 1
        assert citation.document_id == french_contract.id
        assert citation.document_name == "bail-commercial.pdf"
        assert citation.document_version == 1
        assert citation.page_number == 1
        assert citation.case_id == legal_case.id

    def test_a_citation_carries_the_text_the_model_actually_read(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        citation = rag_service.answer(ask(), actor=lawyer).citations[0]

        assert citation.excerpt
        assert citation.excerpt in FRENCH_PAGE or citation.excerpt.endswith("…")

    def test_a_citation_names_the_document_rather_than_an_identifier(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        """'Contrat de bail.pdf, page 7' is a citation a lawyer can act on."""
        citation = rag_service.answer(ask(), actor=lawyer).citations[0]

        assert citation.document_name.endswith(".pdf")

    def test_a_citation_exposes_no_internal_identifier(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        """No chunk number, no point id, no embedding model, no vector."""
        citation = rag_service.answer(ask(), actor=lawyer).citations[0]

        assert set(citation.model_dump()) == {
            "marker",
            "document_id",
            "document_name",
            "document_version",
            "page_number",
            "case_id",
            "score",
            "excerpt",
            "excerpt_truncated",
            "referenced",
        }

    def test_a_source_the_answer_cited_is_marked_referenced(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        citation = rag_service.answer(ask(), actor=lawyer).citations[0]

        assert citation.referenced is True

    def test_a_source_the_model_ignored_is_still_returned(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """The spec asks for citations whenever supporting context exists; a model
        that forgot a marker has not made the evidence disappear."""
        llm_provider.answer = "Le loyer est payable le premier jour du mois."
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.citations
        assert all(citation.referenced is False for citation in outcome.citations)

    def test_markers_are_assigned_in_relevance_order_before_generation(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
    ) -> None:
        """So `[2]` in the prose and the second citation are the same source."""
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert [citation.marker for citation in outcome.citations] == list(
            range(1, len(outcome.citations) + 1)
        )

    def test_an_invented_citation_marker_never_reaches_the_reader(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """A dangling reference in a legal answer invites a reader to look for a
        source that does not exist."""
        llm_provider.answer = "Le loyer est payable le 5 [1], selon la jurisprudence [9]."
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert "[9]" not in outcome.answer
        assert "[1]" in outcome.answer
        assert "jurisprudence" in outcome.answer

    def test_removing_an_invented_marker_leaves_readable_prose(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.answer = "Le loyer est payable le 5 [9] ."
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.answer == "Le loyer est payable le 5."

    def test_the_distinct_documents_behind_an_answer_can_be_listed(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
    ) -> None:
        outcome = rag_service.answer(ask(), actor=lawyer)
        documents = citation_document_ids(outcome.citations)

        assert len(documents) == len(set(documents))
        assert all(isinstance(document_id, uuid.UUID) for document_id in documents)


# --------------------------------------------------------------------------- #
# No fabrication
# --------------------------------------------------------------------------- #


class TestNoEvidence:
    def test_an_empty_corpus_is_answered_without_calling_a_model(
        self, rag_service: Any, lawyer: User, llm_provider: ScriptedLLMProvider
    ) -> None:
        """The cheapest safeguard against fabrication is the call not made."""
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert llm_provider.calls == []
        assert outcome.grounded is False
        assert outcome.insufficient_evidence is True
        assert outcome.citations == []

    def test_the_no_evidence_message_is_the_platforms_own(
        self, rag_service: Any, lawyer: User
    ) -> None:
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.answer == NO_EVIDENCE_MESSAGES["fr"]

    def test_the_no_evidence_message_follows_the_answer_language(
        self, rag_service: Any, lawyer: User
    ) -> None:
        outcome = rag_service.answer(ask(language="ar"), actor=lawyer)

        assert outcome.language == "ar"
        assert outcome.answer == NO_EVIDENCE_MESSAGES["ar"]

    def test_a_caller_assigned_to_nothing_gets_no_answer_from_anyone_elses_file(
        self,
        rag_service: Any,
        outsider: User,
        make_case: MakeCase,
        make_document: MakeDocument,
        index_document: Any,
        llm_provider: ScriptedLLMProvider,
        legal_case: Case,
        french_contract: Document,
    ) -> None:
        """The most dangerous silent failure this feature could have."""
        unassigned = make_case()
        outcome = rag_service.answer(ask(), actor=outsider)

        assert outcome.grounded is False
        assert llm_provider.calls == []
        assert unassigned.id is not None

    def test_the_model_declining_is_honoured(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """The spec's 'acknowledge insufficient evidence', as a typed outcome
        rather than a sentence a client pattern-matches in three languages."""
        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.insufficient_evidence is True
        assert outcome.grounded is False
        assert outcome.answer == NO_EVIDENCE_MESSAGES["fr"]
        assert INSUFFICIENT_EVIDENCE_MARKER not in outcome.answer

    def test_a_declined_answer_carries_no_citations(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """An answer reading "I could not find any supporting document" beside a
        list of sources contradicts itself in front of the reader."""
        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.citations == []

    def test_both_no_evidence_paths_agree_about_citations(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
        make_user: MakeUser,
    ) -> None:
        """Retrieval finding nothing and the model judging what it found
        insufficient are the same outcome to a caller; returning citations for one
        and not the other would be a distinction they cannot act on."""
        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER
        declined = rag_service.answer(ask(), actor=lawyer)

        stranger = make_user(email="rag-nobody@example.com", role=UserRole.LAWYER)
        unretrieved = rag_service.answer(ask(), actor=stranger)

        assert declined.insufficient_evidence == unretrieved.insufficient_evidence is True
        assert declined.citations == unretrieved.citations == []

    def test_what_was_considered_is_still_reported(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """Withholding the citations conceals nothing: the counts stay."""
        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.retrieved_count >= 1
        assert outcome.context_count >= 1

    def test_declining_is_a_success_rather_than_a_failure(
        self, rag_service: Any, lawyer: User, rag_metrics: Any
    ) -> None:
        """Counting it as a failure would make the failure rate a measure of the
        corpus, and would hide a genuine outage behind it."""
        rag_service.answer(ask(), actor=lawyer)
        snapshot = rag_metrics.snapshot()

        assert snapshot.successful_requests == 1
        assert snapshot.failed_requests == 0
        assert snapshot.insufficient_evidence == 1
        assert snapshot.grounded_answers == 0


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuthorization:
    def test_a_caller_retrieves_only_their_own_cases_passages(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        foreign_document: Document,
        legal_case: Case,
    ) -> None:
        outcome = rag_service.answer(ask(), actor=lawyer)
        cited = {citation.case_id for citation in outcome.citations}

        assert cited == {legal_case.id}

    def test_an_unauthorized_passage_never_reaches_the_model(
        self,
        rag_service: Any,
        lawyer: User,
        foreign_document: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """`ai-architecture.md`: unauthorized content must never reach the LLM."""
        rag_service.answer(ask(), actor=lawyer)

        assert llm_provider.calls == []

    def test_an_administrator_spans_every_case(
        self,
        rag_service: Any,
        admin: User,
        french_contract: Document,
        foreign_document: Document,
    ) -> None:
        outcome = rag_service.answer(ask(), actor=admin)
        cited = {citation.document_id for citation in outcome.citations}

        assert {french_contract.id, foreign_document.id} <= cited

    def test_filtering_by_an_inaccessible_case_is_refused_rather_than_emptied(
        self, rag_service: Any, lawyer: User, other_case: Case, foreign_document: Document
    ) -> None:
        """An inaccessible matter and a quiet one must not be indistinguishable."""
        with pytest.raises(SearchAccessDeniedError):
            rag_service.answer(
                ask(filters={"case_id": str(other_case.id)}), actor=lawyer
            )

    def test_filtering_by_a_case_that_does_not_exist_is_a_404(
        self, rag_service: Any, lawyer: User
    ) -> None:
        with pytest.raises(CaseNotFoundError):
            rag_service.answer(
                ask(filters={"case_id": str(uuid.uuid4())}), actor=lawyer
            )

    def test_a_filter_narrows_the_corpus(
        self,
        rag_service: Any,
        admin: User,
        french_contract: Document,
        foreign_document: Document,
        legal_case: Case,
    ) -> None:
        outcome = rag_service.answer(
            ask(filters={"case_id": str(legal_case.id)}), actor=admin
        )

        assert {citation.case_id for citation in outcome.citations} == {legal_case.id}

    def test_a_rejected_filter_is_not_counted_as_a_pipeline_failure(
        self,
        rag_service: Any,
        lawyer: User,
        other_case: Case,
        foreign_document: Document,
        rag_metrics: Any,
    ) -> None:
        with pytest.raises(SearchAccessDeniedError):
            rag_service.answer(ask(filters={"case_id": str(other_case.id)}), actor=lawyer)

        assert rag_metrics.snapshot().failed_requests == 0


# --------------------------------------------------------------------------- #
# Context assembly
# --------------------------------------------------------------------------- #


class TestContextAssembly:
    def test_the_context_budget_is_enforced_before_the_provider_is_called(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
        llm_provider: ScriptedLLMProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An over-long prompt is rejected after being sent, billed, and waited
        for — or, worse, silently truncated."""
        monkeypatch.setattr(settings, "RAG_MAX_CONTEXT_CHARACTERS", 260)
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.context_characters <= 260
        assert outcome.context_truncated is True
        assert len(llm_provider.calls[0][1]) < 4_000

    def test_fewer_sources_than_retrieved_is_reported(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "RAG_MAX_CONTEXT_CHARACTERS", 260)
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.context_count <= outcome.retrieved_count

    def test_a_clipped_excerpt_says_so(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "RAG_MAX_PASSAGE_CHARACTERS", 210)
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert outcome.citations[0].excerpt_truncated is True

    def test_a_budget_smaller_than_one_passage_fails_rather_than_answering_blind(
        self, rag_service: Any, lawyer: User, french_contract: Document, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "RAG_MAX_CONTEXT_CHARACTERS", 10)

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.CONTEXT_OVERFLOW.value

    def test_sources_are_capped_so_every_one_is_citable(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A source the model is shown but cannot cite is one the reader can
        never check."""
        monkeypatch.setattr(settings, "RAG_MAX_CITATIONS", 1)
        outcome = rag_service.answer(ask(), actor=lawyer)

        assert len(outcome.citations) == 1

    def test_the_retrieval_breadth_is_clamped_to_what_search_will_return(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        outcome = rag_service.answer(ask(top_k=settings.SEARCH_MAX_LIMIT), actor=lawyer)

        assert outcome.retrieved_count <= settings.SEARCH_MAX_LIMIT


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #


class TestLanguage:
    def test_the_language_is_detected_from_the_question(
        self, rag_service: Any, lawyer: User, arabic_evidence: Document
    ) -> None:
        outcome = rag_service.answer(ask("متى يؤدى الكراء الشهري؟"), actor=lawyer)

        assert outcome.language == "ar"

    def test_an_explicit_language_overrides_detection(
        self, rag_service: Any, lawyer: User, arabic_evidence: Document
    ) -> None:
        outcome = rag_service.answer(ask("متى يؤدى الكراء الشهري؟", language="fr"), actor=lawyer)

        assert outcome.language == "fr"

    def test_the_answer_language_reaches_the_prompt(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        rag_service.answer(ask(language="ar"), actor=lawyer)

        system, prompt = llm_provider.calls[0]
        assert "ARABIC" in system
        assert "Arabic" in prompt

    def test_an_arabic_question_retrieves_across_the_shared_embedding_space(
        self, rag_service: Any, lawyer: User, arabic_evidence: Document
    ) -> None:
        outcome = rag_service.answer(ask("متى يؤدى الكراء الشهري؟"), actor=lawyer)

        assert outcome.grounded is True
        assert outcome.citations[0].document_id == arabic_evidence.id


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


class TestFailures:
    def test_a_question_with_nothing_to_answer_is_refused(
        self, rag_service: Any, lawyer: User
    ) -> None:
        request = RagRequest.model_construct(
            question="?", language=None, top_k=None, min_score=None, filters=ask().filters
        )

        with pytest.raises(InvalidQuestionError):
            rag_service.answer(request, actor=lawyer)

    def test_a_missing_embedding_model_is_a_retrieval_failure(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        embedder: Any,
    ) -> None:
        embedder.raises = EmbeddingError("model missing")

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.RETRIEVAL_UNAVAILABLE.value
        assert caught.value.status_code == 503

    def test_an_unreachable_vector_database_is_a_retrieval_failure(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        vector_searcher: InMemoryVectorSearcher,
    ) -> None:
        vector_searcher.raises = VectorSearchError("qdrant down")

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.RETRIEVAL_UNAVAILABLE.value

    def test_a_missing_credential_is_reported_as_such(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("no key")

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.LLM_UNAVAILABLE.value

    def test_a_model_timeout_is_reported_as_a_timeout(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMTimeoutError("too slow")

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.TIMEOUT.value

    def test_a_provider_failure_is_reported_as_a_model_failure(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMTransientError("rate limited")

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.LLM_FAILURE.value

    def test_an_empty_completion_is_a_malformed_response(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """Showing a blank answer beside a citation list would read as 'the
        documents say nothing', which the platform has no basis for."""
        llm_provider.answer = "   "

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.MALFORMED_RESPONSE.value

    def test_an_unexpected_fault_is_a_503_rather_than_a_500(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = RuntimeError("something nobody anticipated")

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.UNKNOWN.value

    def test_a_deployment_with_the_pipeline_switched_off_refuses(
        self, rag_service: Any, lawyer: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "RAG_ENABLED", False)

        with pytest.raises(RagDisabledError):
            rag_service.answer(ask(), actor=lawyer)

    def test_a_missing_prompt_template_is_an_unavailability(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A deployment fault: the caller can change nothing that would fix it."""
        monkeypatch.setattr(settings, "RAG_PROMPT_VERSION", 99)

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert caught.value.error_code == RagFailureCode.LLM_UNAVAILABLE.value

    def test_a_failure_is_counted_exactly_once(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
        rag_metrics: Any,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("no key")

        with pytest.raises(RagUnavailableError):
            rag_service.answer(ask(), actor=lawyer)

        snapshot = rag_metrics.snapshot()
        assert snapshot.total_requests == 1
        assert snapshot.failed_requests == 1
        assert snapshot.failures_by_code == {RagFailureCode.LLM_UNAVAILABLE.value: 1}

    def test_a_failure_leaves_the_corpus_untouched(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
        vector_store: Any,
    ) -> None:
        """Answering a question is a read: nothing it can do may change a document."""
        before = dict(vector_store.points)
        llm_provider.raises = LLMUnavailableError("no key")

        with pytest.raises(RagUnavailableError):
            rag_service.answer(ask(), actor=lawyer)

        assert dict(vector_store.points) == before

    def test_the_failure_message_never_quotes_the_question_or_a_passage(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMTransientError("boom")

        with pytest.raises(RagUnavailableError) as caught:
            rag_service.answer(ask(), actor=lawyer)

        assert QUESTION not in caught.value.message
        assert "loyer" not in caught.value.message.lower()


# --------------------------------------------------------------------------- #
# Metrics and health
# --------------------------------------------------------------------------- #


class TestObservability:
    def test_a_grounded_answer_is_recorded(
        self, rag_service: Any, lawyer: User, french_contract: Document, rag_metrics: Any
    ) -> None:
        rag_service.answer(ask(), actor=lawyer)
        snapshot = rag_metrics.snapshot()

        assert snapshot.successful_requests == 1
        assert snapshot.grounded_answers == 1
        assert snapshot.total_citations >= 1
        assert snapshot.average_retrieval_ms is not None
        assert snapshot.average_generation_ms is not None

    def test_token_usage_is_accumulated(
        self, rag_service: Any, lawyer: User, french_contract: Document, rag_metrics: Any
    ) -> None:
        rag_service.answer(ask(), actor=lawyer)
        snapshot = rag_metrics.snapshot()

        assert snapshot.total_prompt_tokens == 120
        assert snapshot.total_completion_tokens == 40
        assert snapshot.metered_requests == 1

    def test_a_run_that_never_called_a_model_does_not_dilute_generation_latency(
        self, rag_service: Any, lawyer: User, rag_metrics: Any
    ) -> None:
        rag_service.answer(ask(), actor=lawyer)

        assert rag_metrics.snapshot().average_generation_ms is None

    def test_health_reports_configuration_and_availability(
        self, rag_service: Any
    ) -> None:
        health = rag_service.health()

        assert health.provider == "scripted"
        assert health.model == "scripted/test-model"
        assert health.llm_available is True
        assert health.prompt_name == settings.RAG_PROMPT_TEMPLATE
        assert health.prompt_available is True
        assert health.enabled is True

    def test_health_reports_a_missing_template(
        self, rag_service: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counters cannot tell 'no prompts installed' from 'nobody has asked'."""
        monkeypatch.setattr(settings, "RAG_PROMPT_VERSION", 99)

        assert rag_service.health().prompt_available is False

    def test_health_reports_an_unconfigured_provider(
        self, rag_service: Any, llm_provider: ScriptedLLMProvider
    ) -> None:
        llm_provider.available = False

        assert rag_service.health().llm_available is False


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


class TestPrivacy:
    # `structlog.testing.capture_logs` rather than pytest's `caplog`, for the
    # reason `tests/unit/test_ocr_access.py` records: the platform logs through
    # structlog, whose console renderer writes to stdout without passing the
    # event through the stdlib handler `caplog` installs — so a `caplog`-based
    # assertion here would pass against an empty string and prove nothing. That
    # matters more in this class than anywhere else, because every assertion in
    # it is a *negative* one.
    @staticmethod
    def events(service: Any, actor: User, **fields: Any) -> list[dict[str, Any]]:
        from structlog.testing import capture_logs

        with capture_logs() as captured:
            service.answer(ask(**fields), actor=actor)
        return captured

    def test_the_pipeline_logs_at_all(
        self, rag_service: Any, lawyer: User, french_contract: Document
    ) -> None:
        """Guards every negative assertion below from passing vacuously."""
        names = {event["event"] for event in self.events(rag_service, lawyer)}

        assert {
            "rag_requested",
            "rag_retrieval_completed",
            "rag_prompt_built",
            "rag_llm_invoked",
            "rag_completed",
        } <= names

    def test_no_question_passage_or_answer_reaches_the_log(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`12-rag-pipeline.md`: never log confidential document contents. A
        question names what a lawyer is looking for, and an answer is an
        interpretation of a client's file."""
        monkeypatch.setattr(settings, "RAG_LOG_QUESTIONS", False)

        written = str(self.events(rag_service, lawyer))

        assert QUESTION not in written
        assert "payable d'avance le premier jour" not in written
        assert "Le loyer est payable le premier jour de chaque mois" not in written
        assert "bail-commercial.pdf" not in written

    def test_the_question_is_correlated_by_a_fingerprint(
        self, rag_service: Any, lawyer: User
    ) -> None:
        from core.rag import question_fingerprint

        written = str(self.events(rag_service, lawyer))

        assert question_fingerprint(QUESTION) in written

    def test_an_operator_can_opt_into_question_logging(
        self, rag_service: Any, lawyer: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "RAG_LOG_QUESTIONS", True)

        written = str(self.events(rag_service, lawyer))

        assert QUESTION in written

    def test_a_failure_log_names_the_cause_and_nothing_else(
        self,
        rag_service: Any,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        from structlog.testing import capture_logs

        llm_provider.raises = LLMUnavailableError("no key")

        with capture_logs() as captured, pytest.raises(RagUnavailableError):
            rag_service.answer(ask(), actor=lawyer)

        failure = next(event for event in captured if event["event"] == "rag_failed")
        assert failure["error_code"] == RagFailureCode.LLM_UNAVAILABLE.value
        assert failure["question"] is None
        assert QUESTION not in str(captured)

    def test_there_is_no_switch_that_logs_the_answer(self) -> None:
        """No operational question is worth putting a generated answer about a
        client's case into a log file."""
        assert not [name for name in dir(settings) if "LOG_ANSWER" in name.upper()]


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


class TestScope:
    def test_the_pipeline_holds_no_way_to_reach_a_vector_directly(
        self, rag_service: Any
    ) -> None:
        """The spec forbids querying the vector database directly when a retrieval
        abstraction exists, and this is what makes that structural rather than a
        matter of discipline."""
        collaborators = vars(rag_service)

        assert "_searcher" not in collaborators
        assert "_embedder" not in collaborators
        assert "_session" not in collaborators
        assert "_documents" not in collaborators

    def test_the_module_imports_no_vector_client(self) -> None:
        """Asserted on the source, because an import is how the boundary would be
        crossed and the type system cannot forbid one."""
        import inspect

        import services.rag as module

        source = inspect.getsource(module)
        for forbidden in (
            "from services.vector_search",
            "from services.vector_store",
            "from core.vector",
            "qdrant",
        ):
            assert forbidden not in source

    def test_the_service_manages_no_conversation(self, rag_service: Any) -> None:
        """`ai-architecture.md`: the RAG pipeline must never manage conversations."""
        members = {name for name in dir(rag_service) if not name.startswith("__")}

        assert not {"conversations", "messages", "history", "remember"} & members
