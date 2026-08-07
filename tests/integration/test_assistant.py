"""Integration tests for the AI Legal Assistant API.

Exercise the endpoints over real HTTP: the conversation lifecycle, the message
contract, streaming as Server-Sent Events, citations, feedback, authorization
(401 vs 403 for every route and every role, plus the per-owner scope), and the
monitoring view.

The corpus is built by the *real* indexing pipeline and answered by the *real*
RAG pipeline: a document is uploaded, extracted, chunked, embedded, and stored,
and only then asked about. So a citation returned here points at a passage that
travelled the whole way from an uploaded file.

The service-level rules are unit-tested in
``tests/unit/test_assistant_service.py``; what these add is the wire contract —
status codes, the response shape a client renders, the SSE frame format, error
envelopes, and four assurances that can only be checked from the outside:

* **another user's conversation is indistinguishable from one that never
  existed**, which is the disclosure this feature must not make;
* **no field on the wire carries a prompt, a vector, a chunk number, or an
  owner**;
* **sending a message needs two permissions**, so a role holding one and not the
  other cannot reach the pipeline through this door;
* the assistant reaches an answer **only through the RAG pipeline**, which is the
  separation ``13-ai-legal-assistant.md`` requires and which
  ``tests/integration/test_rag.py`` asserts from the other side.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from core.rag import INSUFFICIENT_EVIDENCE_MARKER
from models.case import Case
from models.document import Document, DocumentCategory
from models.ocr import OcrResult
from models.user import User, UserRole
from services.llm import LLMUnavailableError

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
CONVERSATIONS_URL = f"{settings.API_V1_PREFIX}/assistant/conversations"
METRICS_URL = f"{settings.API_V1_PREFIX}/assistant/metrics"

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


def messages_url(conversation_id: str) -> str:
    return f"{CONVERSATIONS_URL}/{conversation_id}/messages"


def stream_url(conversation_id: str) -> str:
    return f"{messages_url(conversation_id)}/stream"


def feedback_url(conversation_id: str, message_id: str) -> str:
    return f"{messages_url(conversation_id)}/{message_id}/feedback"


def open_conversation(client: TestClient, token: str, **body: Any) -> Any:
    response = client.post(CONVERSATIONS_URL, json=body, headers=bearer(token))
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def ask(client: TestClient, token: str, conversation_id: str, content: str = QUESTION) -> Any:
    return client.post(
        messages_url(conversation_id), json={"content": content}, headers=bearer(token)
    )


def sse_frames(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a Server-Sent Events body into ``(event, payload)`` pairs."""
    frames: list[tuple[str, dict[str, Any]]] = []

    for raw in body.split("\n\n"):
        name = ""
        data = ""
        for line in raw.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if name and data:
            frames.append((name, json.loads(data)))

    return frames


# --------------------------------------------------------------------------- #
# Actors and corpus
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(
        email="chat-admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR
    )


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(email="chat-lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def outsider(make_user: MakeUser) -> User:
    return make_user(email="chat-outsider@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def court(make_user: MakeUser) -> User:
    return make_user(
        email="chat-court@example.com", password=PASSWORD, role=UserRole.COURT_REPRESENTATIVE
    )


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User, court: User) -> Case:
    return make_case(assigned_lawyer_id=lawyer.id, assigned_court_representative_id=court.id)


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
def lawyer_token(api_client: TestClient, lawyer: User) -> str:
    return token_for(api_client, lawyer.email)


@pytest.fixture
def outsider_token(api_client: TestClient, outsider: User) -> str:
    return token_for(api_client, outsider.email)


@pytest.fixture
def answered(
    api_client: TestClient, lawyer_token: str, legal_case: Case, french_contract: Document
) -> Any:
    """One conversation with one question and its grounded answer."""
    conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))
    response = ask(api_client, lawyer_token, conversation["id"])
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


class TestAuthentication:
    def test_listing_conversations_refuses_an_anonymous_caller(
        self, api_client: TestClient
    ) -> None:
        response = api_client.get(CONVERSATIONS_URL)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_opening_a_conversation_refuses_an_anonymous_caller(
        self, api_client: TestClient
    ) -> None:
        response = api_client.post(CONVERSATIONS_URL, json={})

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_the_metrics_route_refuses_an_anonymous_caller(
        self, api_client: TestClient
    ) -> None:
        response = api_client.get(METRICS_URL)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_the_message_route_refuses_an_anonymous_caller(
        self, api_client: TestClient
    ) -> None:
        response = api_client.post(
            messages_url(str(uuid.uuid4())), json={"content": QUESTION}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuthorization:
    def test_a_court_representative_is_refused(
        self, api_client: TestClient, court: User
    ) -> None:
        """``ai:chat`` has been withheld from this role since Authorization
        shipped, and the assistant is the surface it names."""
        response = api_client.post(
            CONVERSATIONS_URL, json={}, headers=bearer(token_for(api_client, court.email))
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_denial_names_neither_permission_nor_role(
        self, api_client: TestClient, court: User
    ) -> None:
        """Naming the missing permission would hand out a map of the platform's
        capability model."""
        response = api_client.post(
            CONVERSATIONS_URL, json={}, headers=bearer(token_for(api_client, court.email))
        )
        body = response.text.lower()

        assert "ai:chat" not in body
        assert "ai:ask" not in body
        assert "court" not in body

    def test_metrics_are_refused_to_a_lawyer(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        """Platform-wide figures are administrative and deliberately not scoped."""
        response = api_client.get(METRICS_URL, headers=bearer(lawyer_token))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_metrics_are_served_to_an_administrator(
        self, api_client: TestClient, admin: User
    ) -> None:
        response = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        )

        assert response.status_code == status.HTTP_200_OK

    def test_sending_a_message_requires_both_ai_permissions(
        self, api_client: TestClient, lawyer: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ai:chat`` opens the surface and ``ai:ask`` puts the question; a
        message does both, so a deployment granting one and withholding the other
        must not reach the pipeline through this door.

        Exercised by narrowing the lawyer's policy rather than by inspecting the
        route, because what matters is the **refusal**, not the declaration.
        """
        import core.roles as roles
        from core.permissions import Permission

        narrowed = {
            role: (grants - {Permission.AI_ASK} if role is UserRole.LAWYER else grants)
            for role, grants in roles.ROLE_PERMISSIONS.items()
        }
        monkeypatch.setattr(roles, "ROLE_PERMISSIONS", narrowed)

        token = token_for(api_client, lawyer.email)
        conversation = open_conversation(api_client, token)
        response = ask(api_client, token, conversation["id"])

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_reading_a_transcript_does_not_require_ai_ask(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        """Reading what was already answered asks nothing new of the pipeline."""
        response = api_client.get(
            messages_url(answered["conversation"]["id"]), headers=bearer(lawyer_token)
        )

        assert response.status_code == status.HTTP_200_OK


class TestOwnership:
    def test_another_users_conversation_is_not_found(
        self, api_client: TestClient, lawyer_token: str, outsider_token: str
    ) -> None:
        """**404, not 403.** Confirming that another user's conversation exists is
        itself the disclosure the spec forbids — unlike a case, which a colleague
        may legitimately ask to be assigned to."""
        theirs = open_conversation(api_client, outsider_token)

        response = api_client.get(
            f"{CONVERSATIONS_URL}/{theirs['id']}", headers=bearer(lawyer_token)
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_a_conversation_that_never_existed_answers_identically(
        self, api_client: TestClient, lawyer_token: str, outsider_token: str
    ) -> None:
        """The two must be indistinguishable, or the difference is an oracle."""
        theirs = open_conversation(api_client, outsider_token)

        real = api_client.get(
            f"{CONVERSATIONS_URL}/{theirs['id']}", headers=bearer(lawyer_token)
        )
        imaginary = api_client.get(
            f"{CONVERSATIONS_URL}/{uuid.uuid4()}", headers=bearer(lawyer_token)
        )

        assert real.status_code == imaginary.status_code
        assert real.json()["error"] == imaginary.json()["error"]

    def test_the_list_contains_only_the_callers_own(
        self, api_client: TestClient, lawyer_token: str, outsider_token: str
    ) -> None:
        open_conversation(api_client, outsider_token, title="La leur")
        open_conversation(api_client, lawyer_token, title="La mienne")

        response = api_client.get(CONVERSATIONS_URL, headers=bearer(lawyer_token))
        body = response.json()

        assert body["total_records"] == 1
        assert [item["title"] for item in body["items"]] == ["La mienne"]

    def test_another_users_conversation_cannot_be_renamed(
        self, api_client: TestClient, lawyer_token: str, outsider_token: str
    ) -> None:
        theirs = open_conversation(api_client, outsider_token)

        response = api_client.patch(
            f"{CONVERSATIONS_URL}/{theirs['id']}",
            json={"title": "Détourné"},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_another_users_conversation_cannot_be_deleted(
        self, api_client: TestClient, lawyer_token: str, outsider_token: str
    ) -> None:
        theirs = open_conversation(api_client, outsider_token)

        response = api_client.delete(
            f"{CONVERSATIONS_URL}/{theirs['id']}", headers=bearer(lawyer_token)
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_a_message_cannot_be_sent_to_another_users_conversation(
        self, api_client: TestClient, lawyer_token: str, outsider_token: str
    ) -> None:
        theirs = open_conversation(api_client, outsider_token)

        response = ask(api_client, lawyer_token, theirs["id"])

        assert response.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# Conversation lifecycle
# --------------------------------------------------------------------------- #


class TestConversationLifecycle:
    def test_a_conversation_is_created_empty(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        conversation = open_conversation(api_client, lawyer_token)

        assert conversation["message_count"] == 0
        assert conversation["messages"] == []
        assert conversation["status"] == "active"

    def test_a_conversation_can_be_opened_with_its_first_question(
        self, api_client: TestClient, lawyer_token: str, legal_case: Case, french_contract: Document
    ) -> None:
        conversation = open_conversation(
            api_client, lawyer_token, case_id=str(legal_case.id), first_message=QUESTION
        )

        assert conversation["message_count"] == 2
        assert [message["role"] for message in conversation["messages"]] == [
            "user",
            "assistant",
        ]

    def test_renaming_marks_the_title_as_the_users(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        conversation = open_conversation(api_client, lawyer_token)

        response = api_client.patch(
            f"{CONVERSATIONS_URL}/{conversation['id']}",
            json={"title": "Bail Atlas"},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {**response.json(), "title": "Bail Atlas", "title_is_custom": True}

    def test_archiving_hides_it_from_the_default_list(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        conversation = open_conversation(api_client, lawyer_token)
        api_client.patch(
            f"{CONVERSATIONS_URL}/{conversation['id']}",
            json={"status": "archived"},
            headers=bearer(lawyer_token),
        )

        listed = api_client.get(CONVERSATIONS_URL, headers=bearer(lawyer_token)).json()
        archived = api_client.get(
            f"{CONVERSATIONS_URL}?status=archived", headers=bearer(lawyer_token)
        ).json()

        assert listed["total_records"] == 0
        assert archived["total_records"] == 1

    def test_an_archived_conversation_refuses_new_messages(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        conversation = open_conversation(api_client, lawyer_token)
        api_client.patch(
            f"{CONVERSATIONS_URL}/{conversation['id']}",
            json={"status": "archived"},
            headers=bearer(lawyer_token),
        )

        response = ask(api_client, lawyer_token, conversation["id"])

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error"] == "conversation_archived"

    def test_an_archived_conversation_stays_readable(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        """Archiving takes it out of the working set; it does not withdraw it."""
        conversation_id = answered["conversation"]["id"]
        api_client.patch(
            f"{CONVERSATIONS_URL}/{conversation_id}",
            json={"status": "archived"},
            headers=bearer(lawyer_token),
        )

        response = api_client.get(
            f"{CONVERSATIONS_URL}/{conversation_id}", headers=bearer(lawyer_token)
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["message_count"] == 2

    def test_deleting_answers_204_and_then_404(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        conversation = open_conversation(api_client, lawyer_token)

        deleted = api_client.delete(
            f"{CONVERSATIONS_URL}/{conversation['id']}", headers=bearer(lawyer_token)
        )
        again = api_client.get(
            f"{CONVERSATIONS_URL}/{conversation['id']}", headers=bearer(lawyer_token)
        )

        assert deleted.status_code == status.HTTP_204_NO_CONTENT
        assert not deleted.content
        assert again.status_code == status.HTTP_404_NOT_FOUND

    def test_the_list_is_paginated(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        for index in range(3):
            open_conversation(api_client, lawyer_token, title=f"Sujet {index}")

        page = api_client.get(
            f"{CONVERSATIONS_URL}?page=1&page_size=2", headers=bearer(lawyer_token)
        ).json()

        assert page["total_records"] == 3
        assert page["total_pages"] == 2
        assert len(page["items"]) == 2


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #


class TestMessaging:
    def test_a_question_is_answered_with_citations(self, answered: Any) -> None:
        answer = answered["assistant_message"]

        assert answer["grounded"] is True
        assert answer["citations"]
        assert answer["citation_count"] == len(answer["citations"])

    def test_a_citation_carries_document_version_page_and_case(
        self, answered: Any, french_contract: Document, legal_case: Case
    ) -> None:
        """The four references ``12-rag-pipeline.md`` names, passed through
        unmodified."""
        citation = answered["assistant_message"]["citations"][0]

        assert citation["document_id"] == str(french_contract.id)
        assert citation["document_name"] == "bail-commercial.pdf"
        assert citation["document_version"] == 1
        assert citation["page_number"] >= 1
        assert citation["case_id"] == str(legal_case.id)

    def test_both_messages_come_back(self, answered: Any) -> None:
        """The question is stored with a server-assigned identifier and sequence
        that a client would otherwise have to invent and reconcile."""
        assert answered["user_message"]["sequence"] == 1
        assert answered["assistant_message"]["sequence"] == 2
        assert answered["conversation"]["message_count"] == 2

    def test_the_first_question_titles_the_conversation(self, answered: Any) -> None:
        assert answered["conversation"]["title"] == QUESTION

    def test_the_transcript_is_returned_oldest_first(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        response = api_client.get(
            messages_url(answered["conversation"]["id"]), headers=bearer(lawyer_token)
        )
        body = response.json()

        assert [item["sequence"] for item in body["items"]] == [1, 2]

    def test_an_unanswerable_question_is_refused_with_422(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        conversation = open_conversation(api_client, lawyer_token)

        response = api_client.post(
            messages_url(conversation["id"]),
            json={"content": "???"},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_filter_naming_an_inaccessible_case_is_refused_with_403(
        self, api_client: TestClient, lawyer_token: str, other_case: Case
    ) -> None:
        """Passed through unchanged from the search service — the assistant adds
        no second authorization rule of its own."""
        conversation = open_conversation(api_client, lawyer_token)

        response = api_client.post(
            messages_url(conversation["id"]),
            json={"content": QUESTION, "filters": {"case_id": str(other_case.id)}},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_an_unassigned_lawyer_is_answered_from_nothing(
        self, api_client: TestClient, outsider_token: str, french_contract: Document
    ) -> None:
        conversation = open_conversation(api_client, outsider_token)

        response = ask(api_client, outsider_token, conversation["id"])
        answer = response.json()["assistant_message"]

        assert response.status_code == status.HTTP_201_CREATED
        assert answer["grounded"] is False
        assert answer["insufficient_evidence"] is True
        assert answer["citations"] == []

    def test_an_arabic_question_is_answered_and_the_filing_is_retrieved(
        self,
        api_client: TestClient,
        lawyer_token: str,
        legal_case: Case,
        arabic_evidence: Document,
    ) -> None:
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        response = ask(api_client, lawyer_token, conversation["id"], content="متى يؤدى الكراء؟")
        answer = response.json()["assistant_message"]

        assert response.status_code == status.HTTP_201_CREATED
        assert answer["language"] == "ar"
        assert any(
            citation["document_name"] == "عقد-كراء.pdf" for citation in answer["citations"]
        )

    def test_a_model_that_declines_never_leaks_the_sentinel(
        self, api_client: TestClient, lawyer_token: str, answered: Any, llm_provider: Any
    ) -> None:
        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER

        response = ask(
            api_client, lawyer_token, answered["conversation"]["id"], content="Et la durée ?"
        )
        answer = response.json()["assistant_message"]

        assert INSUFFICIENT_EVIDENCE_MARKER not in answer["content"]
        assert answer["grounded"] is False
        assert answer["citations"] == []

    def test_a_provider_outage_answers_503_naming_its_cause(
        self, api_client: TestClient, lawyer_token: str, legal_case: Case, french_contract: Document,
        llm_provider: Any
    ) -> None:
        llm_provider.raises = LLMUnavailableError("no credential")
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        response = ask(api_client, lawyer_token, conversation["id"])

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "llm_unavailable"

    def test_a_failure_body_quotes_neither_the_question_nor_the_sdk(
        self, api_client: TestClient, lawyer_token: str, legal_case: Case, french_contract: Document,
        llm_provider: Any
    ) -> None:
        llm_provider.raises = LLMUnavailableError("quota exceeded for project 12345")
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        response = ask(api_client, lawyer_token, conversation["id"])

        assert QUESTION not in response.text
        assert "12345" not in response.text


class TestConversationalContext:
    def test_a_follow_up_is_read_against_the_earlier_question(
        self, api_client: TestClient, lawyer_token: str, answered: Any, llm_provider: Any
    ) -> None:
        response = ask(
            api_client, lawyer_token, answered["conversation"]["id"], content="Et le préavis ?"
        )
        answer = response.json()["assistant_message"]

        assert answer["context_turns"] == 1
        # The earlier question reached the model, which is what "conversation
        # context" means in practice.
        assert "loyer" in llm_provider.calls[-1][1]

    def test_the_question_is_echoed_back_exactly_as_typed(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        """The follow-up preamble is machinery; showing it in a transcript would
        show someone words they did not write."""
        response = ask(
            api_client, lawyer_token, answered["conversation"]["id"], content="Et le préavis ?"
        )

        assert response.json()["user_message"]["content"] == "Et le préavis ?"

    def test_a_first_question_carries_no_context(self, answered: Any) -> None:
        assert answered["assistant_message"]["context_turns"] == 0


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


class TestStreaming:
    def test_the_stream_is_server_sent_events(
        self, api_client: TestClient, lawyer_token: str, legal_case: Case, french_contract: Document
    ) -> None:
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        response = api_client.post(
            stream_url(conversation["id"]),
            json={"content": QUESTION},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"

    def test_it_emits_retrieval_then_deltas_then_final(
        self,
        api_client: TestClient,
        lawyer_token: str,
        legal_case: Case,
        french_contract: Document,
        llm_provider: Any,
    ) -> None:
        llm_provider.stream_chunks = ["Le loyer ", "est payable ", "le 1er [1]."]
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        response = api_client.post(
            stream_url(conversation["id"]),
            json={"content": QUESTION},
            headers=bearer(lawyer_token),
        )
        frames = sse_frames(response.text)

        assert frames[0][0] == "retrieval"
        assert frames[0][1]["retrieved_count"] >= 1
        assert [name for name, _ in frames].count("delta") >= 1
        assert frames[-1][0] == "final"

    def test_the_final_frame_carries_the_answer_and_its_citations(
        self,
        api_client: TestClient,
        lawyer_token: str,
        legal_case: Case,
        french_contract: Document,
        llm_provider: Any,
    ) -> None:
        """A client must render *this*, not the accumulated deltas: a dangling
        citation marker has been removed from it, and a refusal replaced."""
        llm_provider.stream_chunks = ["Le loyer est payable le 1er [1]."]
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        response = api_client.post(
            stream_url(conversation["id"]),
            json={"content": QUESTION},
            headers=bearer(lawyer_token),
        )
        final = dict(sse_frames(response.text))["final"]

        assert final["grounded"] is True
        assert final["citations"]
        assert final["answer"]

    def test_a_streamed_exchange_is_persisted(
        self,
        api_client: TestClient,
        lawyer_token: str,
        legal_case: Case,
        french_contract: Document,
        llm_provider: Any,
    ) -> None:
        llm_provider.stream_chunks = ["Le loyer est payable le 1er [1]."]
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        api_client.post(
            stream_url(conversation["id"]),
            json={"content": QUESTION},
            headers=bearer(lawyer_token),
        )
        transcript = api_client.get(
            messages_url(conversation["id"]), headers=bearer(lawyer_token)
        ).json()

        assert transcript["total_records"] == 2
        assert [item["role"] for item in transcript["items"]] == ["user", "assistant"]

    def test_a_request_rejection_keeps_its_http_status(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        """The stream is started only once retrieval has succeeded, so a
        rejection never has to be smuggled into an event."""
        conversation = open_conversation(api_client, lawyer_token)
        api_client.patch(
            f"{CONVERSATIONS_URL}/{conversation['id']}",
            json={"status": "archived"},
            headers=bearer(lawyer_token),
        )

        response = api_client.post(
            stream_url(conversation["id"]),
            json={"content": QUESTION},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.headers["content-type"].startswith("application/json")

    def test_an_unknown_conversation_answers_404_not_a_stream(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        response = api_client.post(
            stream_url(str(uuid.uuid4())),
            json={"content": QUESTION},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_streaming_falls_back_when_the_provider_cannot_stream(
        self,
        api_client: TestClient,
        lawyer_token: str,
        legal_case: Case,
        french_contract: Document,
        llm_provider: Any,
    ) -> None:
        llm_provider.stream_raises = LLMUnavailableError("streaming unsupported")
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        response = api_client.post(
            stream_url(conversation["id"]),
            json={"content": QUESTION},
            headers=bearer(lawyer_token),
        )
        frames = dict(sse_frames(response.text))

        assert response.status_code == status.HTTP_200_OK
        assert frames["final"]["grounded"] is True

    def test_an_arabic_answer_survives_the_stream_unescaped(
        self,
        api_client: TestClient,
        lawyer_token: str,
        legal_case: Case,
        arabic_evidence: Document,
        llm_provider: Any,
    ) -> None:
        """``ensure_ascii=False``: an Arabic answer escaped into ``\\uXXXX``
        triples its size and is unreadable in a network inspector."""
        llm_provider.stream_chunks = ["يؤدى الكراء في اليوم الأول [1]."]
        conversation = open_conversation(api_client, lawyer_token, case_id=str(legal_case.id))

        response = api_client.post(
            stream_url(conversation["id"]),
            json={"content": "متى يؤدى الكراء؟"},
            headers=bearer(lawyer_token),
        )

        assert "الكراء" in response.text


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


class TestSuggestions:
    def test_a_grounded_answer_carries_suggested_follow_ups(
        self, answered: Any, follow_up_suggester: Any
    ) -> None:
        assert answered["assistant_message"]["suggestions"] == follow_up_suggester.suggestions

    def test_an_ungrounded_answer_carries_none(
        self, api_client: TestClient, outsider_token: str, french_contract: Document
    ) -> None:
        conversation = open_conversation(api_client, outsider_token)

        response = ask(api_client, outsider_token, conversation["id"])

        assert response.json()["assistant_message"]["suggestions"] == []

    def test_suggestions_survive_a_reload(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        """Stored rather than regenerated: a model call per page view would be
        absurd, and regenerating could suggest something different each time."""
        transcript = api_client.get(
            messages_url(answered["conversation"]["id"]), headers=bearer(lawyer_token)
        ).json()

        assert transcript["items"][-1]["suggestions"] == (
            answered["assistant_message"]["suggestions"]
        )


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #


class TestFeedback:
    def test_an_answer_can_be_rated(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        response = api_client.put(
            feedback_url(answered["conversation"]["id"], answered["assistant_message"]["id"]),
            json={"rating": "helpful"},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["rating"] == "helpful"

    def test_the_rating_comes_back_on_the_transcript(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        api_client.put(
            feedback_url(answered["conversation"]["id"], answered["assistant_message"]["id"]),
            json={"rating": "not_helpful", "comment": "Trop vague"},
            headers=bearer(lawyer_token),
        )

        transcript = api_client.get(
            messages_url(answered["conversation"]["id"]), headers=bearer(lawyer_token)
        ).json()

        assert transcript["items"][-1]["feedback"]["rating"] == "not_helpful"
        assert transcript["items"][-1]["feedback"]["comment"] == "Trop vague"

    def test_rating_does_not_change_the_answer(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        """*"Feedback should not modify conversation history."*"""
        before = answered["assistant_message"]["content"]

        api_client.put(
            feedback_url(answered["conversation"]["id"], answered["assistant_message"]["id"]),
            json={"rating": "not_helpful"},
            headers=bearer(lawyer_token),
        )
        transcript = api_client.get(
            messages_url(answered["conversation"]["id"]), headers=bearer(lawyer_token)
        ).json()

        assert transcript["total_records"] == 2
        assert transcript["items"][-1]["content"] == before

    def test_rating_twice_leaves_one_rating(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        """A `PUT` because a rating is a property of the message rather than a
        new thing each time."""
        url = feedback_url(
            answered["conversation"]["id"], answered["assistant_message"]["id"]
        )
        api_client.put(url, json={"rating": "helpful"}, headers=bearer(lawyer_token))
        second = api_client.put(
            url, json={"rating": "not_helpful"}, headers=bearer(lawyer_token)
        )

        assert second.status_code == status.HTTP_200_OK
        assert second.json()["rating"] == "not_helpful"

    def test_rating_your_own_question_is_refused(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        response = api_client.put(
            feedback_url(answered["conversation"]["id"], answered["user_message"]["id"]),
            json={"rating": "helpful"},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["error"] == "invalid_feedback_target"

    def test_a_third_rating_is_refused(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        response = api_client.put(
            feedback_url(answered["conversation"]["id"], answered["assistant_message"]["id"]),
            json={"rating": "excellent"},
            headers=bearer(lawyer_token),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_withdrawing_answers_204_and_is_idempotent(
        self, api_client: TestClient, lawyer_token: str, answered: Any
    ) -> None:
        url = feedback_url(
            answered["conversation"]["id"], answered["assistant_message"]["id"]
        )
        api_client.put(url, json={"rating": "helpful"}, headers=bearer(lawyer_token))

        first = api_client.delete(url, headers=bearer(lawyer_token))
        second = api_client.delete(url, headers=bearer(lawyer_token))

        assert first.status_code == status.HTTP_204_NO_CONTENT
        assert second.status_code == status.HTTP_204_NO_CONTENT

    def test_another_users_answer_cannot_be_rated(
        self, api_client: TestClient, outsider_token: str, answered: Any
    ) -> None:
        response = api_client.put(
            feedback_url(answered["conversation"]["id"], answered["assistant_message"]["id"]),
            json={"rating": "helpful"},
            headers=bearer(outsider_token),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_it_reports_the_six_figures_the_spec_names(
        self, api_client: TestClient, admin: User, answered: Any
    ) -> None:
        response = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        )
        body = response.json()

        for field in (
            "active_conversations",
            "average_response_ms",
            "average_conversation_length",
            "successful_requests",
            "failed_requests",
            "total_feedback",
        ):
            assert field in body

    def test_it_counts_conversations_and_messages(
        self, api_client: TestClient, admin: User, answered: Any
    ) -> None:
        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert body["active_conversations"] == 1
        assert body["total_messages"] == 2
        assert body["average_conversation_length"] == 2.0

    def test_it_counts_feedback(
        self, api_client: TestClient, admin: User, lawyer_token: str, answered: Any
    ) -> None:
        api_client.put(
            feedback_url(answered["conversation"]["id"], answered["assistant_message"]["id"]),
            json={"rating": "helpful"},
            headers=bearer(lawyer_token),
        )

        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert body["total_feedback"] == 1
        assert body["helpful_feedback"] == 1
        assert body["helpful_rate"] == 100.0

    def test_the_helpful_rate_is_null_when_nobody_has_rated(
        self, api_client: TestClient, admin: User, answered: Any
    ) -> None:
        """`0` would read as "every answer was unhelpful", which is a different
        and much more alarming statement than "nobody has said"."""
        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert body["helpful_rate"] is None

    def test_it_exposes_no_question_answer_title_or_identifier(
        self, api_client: TestClient, admin: User, answered: Any, legal_case: Case
    ) -> None:
        """An operational view of the platform: counts, rates, and configuration
        only — never a conversation, a title, a question, or whose thread it was."""
        raw = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).text

        for forbidden in (
            QUESTION,
            answered["assistant_message"]["content"],
            answered["conversation"]["id"],
            str(legal_case.id),
            "bail-commercial.pdf",
        ):
            assert forbidden not in raw


# --------------------------------------------------------------------------- #
# The wire contract
# --------------------------------------------------------------------------- #


class TestWireContract:
    def test_a_message_carries_no_prompt_vector_or_chunk_number(
        self, answered: Any
    ) -> None:
        """The prompt contains the retrieved passages of a case file."""
        raw = json.dumps(answered)

        for forbidden in ("prompt_text", "system_instruction", "vector", "point_id", "chunk_number"):
            assert forbidden not in raw

    def test_a_conversation_carries_no_owner(self, answered: Any) -> None:
        """It is only ever served to its owner, so naming them adds nothing — and
        a payload carrying a user identifier invites a client to use it as one."""
        assert "owner_id" not in answered["conversation"]

    def test_a_citation_exposes_no_internal_identifier(self, answered: Any) -> None:
        citation = answered["assistant_message"]["citations"][0]

        assert set(citation) == {
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


class TestSeparationFromThePipeline:
    def test_the_assistant_owns_the_conversation_paths_and_rag_owns_none(self) -> None:
        """``12-rag-pipeline.md`` puts conversations, history, streaming, and
        feedback out of the pipeline's scope; this feature is where all four
        legitimately arrive, and they arrive under ``/assistant``."""
        from main import app

        paths = set(app.openapi()["paths"])
        rag_paths = {
            path for path in paths if path.startswith(f"{settings.API_V1_PREFIX}/rag")
        }

        assert rag_paths == {
            f"{settings.API_V1_PREFIX}/rag/answer",
            f"{settings.API_V1_PREFIX}/rag/metrics",
        }
        assert any("conversation" in path for path in paths)

    def test_the_assistant_exposes_exactly_six_paths(self) -> None:
        """A seventh is how report generation, summarization, or a second
        retrieval surface would arrive early."""
        from main import app

        paths = {
            path
            for path in app.openapi()["paths"]
            if path.startswith(f"{settings.API_V1_PREFIX}/assistant")
        }

        assert paths == {
            CONVERSATIONS_URL,
            f"{CONVERSATIONS_URL}/{{conversation_id}}",
            f"{CONVERSATIONS_URL}/{{conversation_id}}/messages",
            f"{CONVERSATIONS_URL}/{{conversation_id}}/messages/stream",
            f"{CONVERSATIONS_URL}/{{conversation_id}}/messages/{{message_id}}/feedback",
            METRICS_URL,
        }

    def test_the_assistant_exposes_no_retrieval_endpoint(self) -> None:
        """Retrieval is the search service's, and the pipeline reaches it through
        nothing else. A ``/assistant/search`` would be a second, unscoped route
        to a passage."""
        from main import app

        paths = " ".join(
            path
            for path in app.openapi()["paths"]
            if path.startswith(f"{settings.API_V1_PREFIX}/assistant")
        )

        for forbidden in ("search", "retrieve", "passage", "prompt", "index"):
            assert forbidden not in paths
