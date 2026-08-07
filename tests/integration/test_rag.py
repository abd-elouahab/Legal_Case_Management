"""Integration tests for the RAG pipeline API.

Exercise the endpoints over real HTTP: the answer contract, citations,
authorization (401 vs 403 for every route and every role, plus the per-case
scope), the no-evidence path, the failure envelopes, and the monitoring view.

The corpus is built by the *real* indexing pipeline: a document is uploaded,
extracted, chunked, embedded, and stored, and only then asked about. So a
citation returned here points at a passage that travelled the whole way from an
uploaded file, which is what makes these tests about the platform rather than
about a fixture.

The service-level rules are unit-tested in ``tests/unit/test_rag_service.py``;
what these add is the wire contract — status codes, the response shape a client
renders, error envelopes, and three assurances that can only be checked from the
outside:

* **no field on the wire carries a prompt, a vector, a chunk number, or a passage
  the caller may not read**;
* the answer endpoint returns the **same status code** as the document endpoint
  for a caller who is not party to the case, so the pipeline cannot be used to
  probe for matters;
* **the API exposes no chat interface** — ``12-rag-pipeline.md`` puts
  conversations, history, streaming, and feedback out of scope, and this is where
  a stray endpoint would show up.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from core.rag import INSUFFICIENT_EVIDENCE_MARKER, NO_EVIDENCE_MESSAGES, RagFailureCode
from models.case import Case
from models.document import Document, DocumentCategory
from models.ocr import OcrResult
from models.user import User, UserRole
from services.embedding import EmbeddingError
from services.llm import LLMTimeoutError, LLMUnavailableError
from services.vector_search import VectorSearchError

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import (
        FakeEmbedder,
        InMemoryVectorSearcher,
        ScriptedLLMProvider,
    )

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
ANSWER_URL = f"{settings.API_V1_PREFIX}/rag/answer"
METRICS_URL = f"{settings.API_V1_PREFIX}/rag/metrics"

QUESTION = "Quand le loyer est-il payable ?"

FRENCH_PAGE = (
    "CONTRAT DE BAIL COMMERCIAL. Article 4 : Loyer et charges. Le loyer mensuel est "
    "payable d'avance le premier jour de chaque mois, au domicile du bailleur. Toute "
    "résiliation anticipée doit être notifiée par écrit avec un préavis de trois mois."
)
ARABIC_PAGE = (
    "عقد كراء تجاري. المادة الرابعة: الكراء والتحملات. يؤدى الكراء الشهري مسبقا في "
    "اليوم الأول من كل شهر بمقر المكري، ويجب إشعار الطرف الآخر كتابة قبل ثلاثة أشهر."
)

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeDocument = Callable[..., Document]
MakeOcrResult = Callable[..., OcrResult]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == status.HTTP_200_OK, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def ask(client: TestClient, token: str, question: str = QUESTION, **body: Any) -> Any:
    return client.post(ANSWER_URL, json={"question": question, **body}, headers=bearer(token))


# --------------------------------------------------------------------------- #
# Actors and corpus
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(email="rag-admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(email="rag-lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def outsider(make_user: MakeUser) -> User:
    return make_user(email="rag-outsider@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def court(make_user: MakeUser) -> User:
    return make_user(
        email="rag-court@example.com", password=PASSWORD, role=UserRole.COURT_REPRESENTATIVE
    )


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User, court: User) -> Case:
    return make_case(
        assigned_lawyer_id=lawyer.id, assigned_court_representative_id=court.id
    )


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


# --------------------------------------------------------------------------- #
# Authentication and capability
# --------------------------------------------------------------------------- #


class TestAuthentication:
    def test_answering_requires_authentication(self, api_client: TestClient) -> None:
        response = api_client.post(ANSWER_URL, json={"question": QUESTION})

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_metrics_require_authentication(self, api_client: TestClient) -> None:
        assert api_client.get(METRICS_URL).status_code == status.HTTP_401_UNAUTHORIZED

    def test_a_lawyer_may_ask(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        response = ask(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_200_OK

    def test_a_court_representative_may_not_ask(
        self, api_client: TestClient, court: User, french_contract: Document
    ) -> None:
        """`ai:ask` is withheld where `search:query` is granted: search returns
        the platform's own text verbatim, while this returns a *generated*
        interpretation of a case file, and the role descriptions give court
        representatives no AI capabilities at all."""
        response = ask(api_client, token_for(api_client, court.email))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_denial_names_neither_permission_nor_role(
        self, api_client: TestClient, court: User
    ) -> None:
        body = ask(api_client, token_for(api_client, court.email)).json()

        assert "ai:ask" not in str(body)
        assert "court" not in str(body).lower()

    def test_metrics_are_refused_to_a_lawyer_and_a_court_representative(
        self, api_client: TestClient, lawyer: User, court: User
    ) -> None:
        """`ai:monitor` is administrative and deliberately not case-scoped."""
        for user in (lawyer, court):
            response = api_client.get(
                METRICS_URL, headers=bearer(token_for(api_client, user.email))
            )
            assert response.status_code == status.HTTP_403_FORBIDDEN


# --------------------------------------------------------------------------- #
# The answer contract
# --------------------------------------------------------------------------- #


class TestAnswer:
    def test_a_grounded_answer_comes_back_with_its_citations(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        legal_case: Case,
    ) -> None:
        body = ask(api_client, token_for(api_client, lawyer.email)).json()

        assert body["grounded"] is True
        assert body["insufficient_evidence"] is False
        assert body["answer"]
        assert body["citation_count"] >= 1

        citation = body["citations"][0]
        assert citation["document_id"] == str(french_contract.id)
        assert citation["document_name"] == "bail-commercial.pdf"
        assert citation["document_version"] == 1
        assert citation["page_number"] == 1
        assert citation["case_id"] == str(legal_case.id)

    def test_the_response_carries_the_documented_fields_and_no_others(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        body = ask(api_client, token_for(api_client, lawyer.email)).json()

        assert set(body) == {
            "question",
            "answer",
            "language",
            "grounded",
            "insufficient_evidence",
            "truncated",
            "citations",
            "citation_count",
            "referenced_count",
            "retrieved_count",
            "context_count",
            "context_characters",
            "context_truncated",
            "duration_ms",
            "retrieval_ms",
            "generation_ms",
            "prompt_name",
            "prompt_version",
            "provider",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        }

    def test_the_wire_carries_no_prompt_no_vector_and_no_chunk_number(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        raw = ask(api_client, token_for(api_client, lawyer.email)).text

        for forbidden in ("system_instruction", "END CONTEXT", "chunk_number", "point_id"):
            assert forbidden not in raw

    def test_the_answer_records_its_prompt_and_model(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        body = ask(api_client, token_for(api_client, lawyer.email)).json()

        assert body["prompt_name"] == settings.RAG_PROMPT_TEMPLATE
        assert body["prompt_version"] == settings.RAG_PROMPT_VERSION
        assert body["provider"] == "scripted"

    def test_the_normalised_question_is_echoed_back(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        body = ask(api_client, token_for(api_client, lawyer.email), "  Quand   le loyer ?  ").json()

        assert body["question"] == "Quand le loyer ?"

    def test_asking_is_a_post_only(self, api_client: TestClient, lawyer: User) -> None:
        """A GET would write every question to the proxy log, the browser's
        history, and the next page's `Referer` header."""
        response = api_client.get(ANSWER_URL, headers=bearer(token_for(api_client, lawyer.email)))

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_an_empty_question_is_refused_with_a_field_message(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        response = api_client.post(
            ANSWER_URL, json={"question": "?"}, headers=bearer(token_for(api_client, lawyer.email))
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["details"][0]["field"] == "question"

    def test_an_unsupported_answer_language_is_refused(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        response = ask(api_client, token_for(api_client, lawyer.email), language="de")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_unknown_field_is_refused(self, api_client: TestClient, lawyer: User) -> None:
        response = api_client.post(
            ANSWER_URL,
            json={"question": QUESTION, "conversation_id": str(uuid.uuid4())},
            headers=bearer(token_for(api_client, lawyer.email)),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# --------------------------------------------------------------------------- #
# No evidence
# --------------------------------------------------------------------------- #


class TestNoEvidence:
    def test_an_empty_corpus_answers_200_rather_than_404(
        self, api_client: TestClient, lawyer: User, llm_provider: ScriptedLLMProvider
    ) -> None:
        """The documents not covering a question is an answer, not an error."""
        response = ask(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["insufficient_evidence"] is True
        assert body["grounded"] is False
        assert body["citations"] == []
        assert body["generation_ms"] is None
        assert llm_provider.calls == []

    def test_the_no_evidence_answer_is_the_platforms_own_sentence(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        body = ask(api_client, token_for(api_client, lawyer.email)).json()

        assert body["answer"] == NO_EVIDENCE_MESSAGES["fr"]

    def test_the_model_declining_reaches_the_client_as_a_typed_flag(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER
        body = ask(api_client, token_for(api_client, lawyer.email)).json()

        assert body["insufficient_evidence"] is True
        assert INSUFFICIENT_EVIDENCE_MARKER not in body["answer"]
        # No citations beside "I could not find any supporting document", and the
        # counts still say what was considered.
        assert body["citations"] == []
        assert body["retrieved_count"] >= 1
        assert body["context_count"] >= 1


# --------------------------------------------------------------------------- #
# Authorization, per resource
# --------------------------------------------------------------------------- #


class TestScope:
    def test_a_lawyer_is_answered_only_from_their_own_cases(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        foreign_document: Document,
        legal_case: Case,
    ) -> None:
        body = ask(api_client, token_for(api_client, lawyer.email)).json()

        assert {citation["case_id"] for citation in body["citations"]} == {str(legal_case.id)}

    def test_an_unassigned_lawyer_is_answered_from_nothing(
        self,
        api_client: TestClient,
        outsider: User,
        french_contract: Document,
        make_user: MakeUser,
    ) -> None:
        stranger = make_user(
            email="rag-stranger@example.com", password=PASSWORD, role=UserRole.LAWYER
        )
        body = ask(api_client, token_for(api_client, stranger.email)).json()

        assert body["grounded"] is False
        assert body["citations"] == []

    def test_an_administrator_spans_every_case(
        self,
        api_client: TestClient,
        admin: User,
        french_contract: Document,
        foreign_document: Document,
    ) -> None:
        body = ask(api_client, token_for(api_client, admin.email)).json()
        cited = {citation["document_id"] for citation in body["citations"]}

        assert {str(french_contract.id), str(foreign_document.id)} <= cited

    def test_filtering_by_another_partys_case_is_403_not_an_empty_answer(
        self, api_client: TestClient, lawyer: User, other_case: Case, foreign_document: Document
    ) -> None:
        response = ask(
            api_client,
            token_for(api_client, lawyer.email),
            filters={"case_id": str(other_case.id)},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_the_answer_endpoint_agrees_with_the_document_endpoint(
        self, api_client: TestClient, lawyer: User, other_case: Case, foreign_document: Document
    ) -> None:
        """The pipeline must not be usable to probe for matters the document API
        already refuses."""
        token = token_for(api_client, lawyer.email)
        document_url = f"{settings.API_V1_PREFIX}/documents/{foreign_document.id}"

        answered = ask(
            api_client, token, filters={"document_id": str(foreign_document.id)}
        )
        fetched = api_client.get(document_url, headers=bearer(token))

        assert answered.status_code == fetched.status_code

    def test_filtering_by_a_case_that_does_not_exist_is_404(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        response = ask(
            api_client,
            token_for(api_client, lawyer.email),
            filters={"case_id": str(uuid.uuid4())},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_a_filter_narrows_the_corpus(
        self,
        api_client: TestClient,
        admin: User,
        french_contract: Document,
        foreign_document: Document,
        legal_case: Case,
    ) -> None:
        body = ask(
            api_client,
            token_for(api_client, admin.email),
            filters={"case_id": str(legal_case.id)},
        ).json()

        assert {citation["case_id"] for citation in body["citations"]} == {str(legal_case.id)}


# --------------------------------------------------------------------------- #
# Multilingual
# --------------------------------------------------------------------------- #


class TestMultilingual:
    def test_an_arabic_question_reaches_the_arabic_filing(
        self, api_client: TestClient, lawyer: User, arabic_evidence: Document
    ) -> None:
        body = ask(api_client, token_for(api_client, lawyer.email), "متى يؤدى الكراء الشهري؟").json()

        assert body["language"] == "ar"
        assert body["citations"][0]["document_id"] == str(arabic_evidence.id)

    def test_the_answer_language_can_be_chosen(
        self, api_client: TestClient, lawyer: User, arabic_evidence: Document
    ) -> None:
        body = ask(
            api_client, token_for(api_client, lawyer.email), "متى يؤدى الكراء الشهري؟", language="fr"
        ).json()

        assert body["language"] == "fr"

    def test_an_arabic_document_name_survives_the_wire(
        self, api_client: TestClient, lawyer: User, arabic_evidence: Document
    ) -> None:
        body = ask(api_client, token_for(api_client, lawyer.email), "متى يؤدى الكراء الشهري؟").json()

        assert body["citations"][0]["document_name"] == "عقد-كراء.pdf"


# --------------------------------------------------------------------------- #
# Failures, on the wire
# --------------------------------------------------------------------------- #


class TestFailureEnvelopes:
    def test_a_missing_embedding_model_is_503_naming_retrieval(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        embedder: FakeEmbedder,
    ) -> None:
        embedder.raises = EmbeddingError("model missing")
        response = ask(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == RagFailureCode.RETRIEVAL_UNAVAILABLE.value

    def test_an_unreachable_vector_database_is_503_naming_retrieval(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        vector_searcher: InMemoryVectorSearcher,
    ) -> None:
        vector_searcher.raises = VectorSearchError("qdrant down")
        response = ask(api_client, token_for(api_client, lawyer.email))

        assert response.json()["error"] == RagFailureCode.RETRIEVAL_UNAVAILABLE.value

    def test_a_missing_credential_is_503_naming_the_model(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """A missing model and a missing vector database read identically
        otherwise, and need different responses."""
        llm_provider.raises = LLMUnavailableError("no key")
        response = ask(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == RagFailureCode.LLM_UNAVAILABLE.value

    def test_a_model_timeout_is_503_naming_the_timeout(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMTimeoutError("too slow")
        response = ask(api_client, token_for(api_client, lawyer.email))

        assert response.json()["error"] == RagFailureCode.TIMEOUT.value

    def test_a_failure_body_never_quotes_the_question(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("no key")
        raw = ask(api_client, token_for(api_client, lawyer.email)).text

        assert QUESTION not in raw
        assert "loyer" not in raw.lower()

    def test_a_failure_body_never_exposes_an_internal_message(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("credential rejected by vendor endpoint")
        raw = ask(api_client, token_for(api_client, lawyer.email)).text

        assert "vendor endpoint" not in raw

    def test_a_failure_carries_the_standard_envelope(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("no key")
        body = ask(api_client, token_for(api_client, lawyer.email)).json()

        assert set(body) >= {"error", "message", "request_id", "details"}


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_metrics_report_the_figures_the_spec_names(
        self, api_client: TestClient, admin: User, french_contract: Document
    ) -> None:
        token = token_for(api_client, admin.email)
        ask(api_client, token)

        body = api_client.get(METRICS_URL, headers=bearer(token)).json()

        assert body["total_requests"] == 1
        assert body["successful_requests"] == 1
        assert body["failed_requests"] == 0
        assert body["average_latency_ms"] is not None
        assert body["average_retrieval_ms"] is not None
        assert body["total_prompt_tokens"] == 120

    def test_the_rates_sum_to_one_hundred(
        self, api_client: TestClient, admin: User, french_contract: Document
    ) -> None:
        token = token_for(api_client, admin.email)
        ask(api_client, token)

        body = api_client.get(METRICS_URL, headers=bearer(token)).json()

        assert body["success_rate"] + body["failure_rate"] == 100.0

    def test_metrics_report_availability_and_configuration(
        self, api_client: TestClient, admin: User
    ) -> None:
        """The counters cannot tell 'no credential', 'no prompts', and 'nobody has
        asked yet' apart — all three show the same zeros."""
        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert body["llm_available"] is True
        assert body["prompt_available"] is True
        assert body["provider"] == "scripted"
        assert body["prompt_name"] == settings.RAG_PROMPT_TEMPLATE
        assert body["retrieval_top_k"] == settings.RAG_RETRIEVAL_TOP_K
        assert body["enabled"] is True

    def test_a_failure_is_grouped_by_cause(
        self,
        api_client: TestClient,
        admin: User,
        french_contract: Document,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        token = token_for(api_client, admin.email)
        llm_provider.raises = LLMUnavailableError("no key")
        ask(api_client, token)
        llm_provider.raises = None

        body = api_client.get(METRICS_URL, headers=bearer(token)).json()

        assert body["failed_requests"] == 1
        assert body["failures_by_code"] == {RagFailureCode.LLM_UNAVAILABLE.value: 1}

    def test_metrics_contain_no_question_answer_document_or_case(
        self,
        api_client: TestClient,
        admin: User,
        french_contract: Document,
        legal_case: Case,
    ) -> None:
        token = token_for(api_client, admin.email)
        ask(api_client, token)

        raw = api_client.get(METRICS_URL, headers=bearer(token)).text

        for forbidden in (
            QUESTION,
            "loyer",
            "bail-commercial.pdf",
            str(french_contract.id),
            str(legal_case.id),
        ):
            assert forbidden not in raw


# --------------------------------------------------------------------------- #
# Scope of the feature
# --------------------------------------------------------------------------- #


class TestNoChatInterface:
    """``12-rag-pipeline.md`` puts the chat UI, conversation history, persistent
    memory, and report generation out of scope. A stray endpoint is how one of
    them would arrive early."""

    def test_the_api_exposes_exactly_two_rag_endpoints(self, client: TestClient) -> None:
        from main import app

        paths = {
            path for path in app.openapi()["paths"] if path.startswith(f"{settings.API_V1_PREFIX}/rag")
        }

        assert paths == {ANSWER_URL, METRICS_URL}

    def test_the_rag_module_exposes_no_conversation_endpoint(self, client: TestClient) -> None:
        """The pipeline itself holds no conversation, and its paths say so.

        This test asserted that *no path anywhere on the platform* contained
        "conversation", "chat", "message", or "feedback" — correct while none
        existed, and the check that would have caught the chat interface arriving
        inside Feature 12. The AI Legal Assistant (Feature 13) is where all four
        legitimately arrive, under ``/assistant``, so the assertion is narrowed to
        what it was always about: **the RAG pipeline is not the chat interface**.
        ``tests/integration/test_assistant.py`` asserts the separation from the
        other side — that the assistant reaches an answer only through the
        pipeline — exactly as ``test_search.py`` did for indexing.
        """
        from main import app

        rag_paths = " ".join(
            path
            for path, operations in app.openapi()["paths"].items()
            if path.startswith(f"{settings.API_V1_PREFIX}/rag")
            or any(
                "rag" in (operation.get("tags") or [])
                for operation in operations.values()
                if isinstance(operation, dict)
            )
        )

        for forbidden in ("conversation", "chat", "message", "feedback"):
            assert forbidden not in rag_paths

    def test_no_rag_response_schema_mentions_a_conversation(self, client: TestClient) -> None:
        from main import app

        schemas = app.openapi()["components"]["schemas"]
        rag_schemas = {name: body for name, body in schemas.items() if name.startswith("Rag")}

        assert rag_schemas
        for body in rag_schemas.values():
            properties = set(body.get("properties", {}))
            assert not {"conversation_id", "messages", "history", "session_id"} & properties
