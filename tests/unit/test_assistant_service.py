"""Unit tests for :class:`~services.assistant.AssistantService`.

Against the **real** RAG pipeline, on the real search service, on the real
repositories, with the real access policy — only the embedding model, the vector
database, the language model, and the follow-up suggester are doubles. That is
deliberate: this feature's whole claim is that it delegates every answer to the
pipeline and inherits its authorization, and a faked pipeline would make every
one of those assertions vacuous.
"""

from __future__ import annotations

import uuid

import pytest

from core.assistant import ConversationRole
from core.config import settings
from core.exceptions import (
    ConversationArchivedError,
    ConversationFullError,
    ConversationMessageNotFoundError,
    ConversationNotFoundError,
    InvalidFeedbackTargetError,
    RagUnavailableError,
    SearchAccessDeniedError,
)
from core.rag import INSUFFICIENT_EVIDENCE_MARKER, RagFailureCode
from models.conversation import ConversationStatus, FeedbackRating
from models.user import UserRole
from schemas.conversation import (
    ConversationCreate,
    ConversationListQuery,
    ConversationUpdate,
    FeedbackCreate,
    MessageCreate,
)
from schemas.search import SearchFilterInput
from services.llm import LLMUnavailableError


@pytest.fixture
def lawyer(make_user):  # type: ignore[no-untyped-def]
    return make_user(email="lawyer@example.com", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user):  # type: ignore[no-untyped-def]
    return make_user(email="other@example.com", role=UserRole.LAWYER)


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #


class TestCreateConversation:
    def test_it_opens_a_conversation_owned_by_the_caller(self, assistant_service, lawyer) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(ConversationCreate(), actor=lawyer)

        assert conversation.owner_id == lawyer.id
        assert conversation.status is ConversationStatus.ACTIVE
        assert conversation.message_count == 0

    def test_an_unnamed_conversation_gets_a_placeholder_not_a_guess(
        self, assistant_service, lawyer
    ) -> None:  # type: ignore[no-untyped-def]
        """Nothing has been said in it yet, so there is no subject to name."""
        conversation = assistant_service.create_conversation(ConversationCreate(), actor=lawyer)

        assert conversation.title
        assert not conversation.title_is_custom

    def test_a_supplied_title_is_marked_as_the_users(self, assistant_service, lawyer) -> None:  # type: ignore[no-untyped-def]
        """Automatic titling must never overwrite a name somebody chose."""
        conversation = assistant_service.create_conversation(
            ConversationCreate(title="Bail Atlas"), actor=lawyer
        )

        assert conversation.title == "Bail Atlas"
        assert conversation.title_is_custom

    def test_a_case_is_not_validated_here(self, assistant_service, lawyer) -> None:  # type: ignore[no-untyped-def]
        """Pinning is a default retrieval filter and a label, never a grant: the
        check that matters happens on every message, inside the search service.
        Validating it twice would be a second rule to keep in step with the
        first — and validating it *only* here would be the dangerous half."""
        unreachable = uuid.uuid4()
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=unreachable), actor=lawyer
        )

        assert conversation.case_id == unreachable

    def test_it_is_refused_when_the_assistant_is_disabled(
        self, assistant_service, lawyer, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        from core.exceptions import AssistantDisabledError

        monkeypatch.setattr(settings, "ASSISTANT_ENABLED", False)

        with pytest.raises(AssistantDisabledError):
            assistant_service.create_conversation(ConversationCreate(), actor=lawyer)


class TestConversationOwnership:
    def test_another_users_conversation_is_not_found(
        self, assistant_service, lawyer, other_lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        """**Not** 403. Confirming that another user's conversation *exists* is
        itself the disclosure the spec forbids — unlike a case, which a colleague
        may legitimately ask to be assigned to."""
        theirs = make_conversation(owner=other_lawyer)

        with pytest.raises(ConversationNotFoundError):
            assistant_service.get_conversation(theirs.id, actor=lawyer)

    def test_a_deleted_conversation_is_not_found(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = make_conversation(owner=lawyer)
        assistant_service.delete_conversation(conversation.id, actor=lawyer)

        with pytest.raises(ConversationNotFoundError):
            assistant_service.get_conversation(conversation.id, actor=lawyer)

    def test_the_list_contains_only_the_callers_own(
        self, assistant_service, lawyer, other_lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        make_conversation(owner=lawyer, title="Mienne")
        make_conversation(owner=other_lawyer, title="La leur")

        conversations, total = assistant_service.list_conversations(
            ConversationListQuery(), actor=lawyer
        )

        assert total == 1
        assert [conversation.title for conversation in conversations] == ["Mienne"]

    def test_the_total_counts_only_the_callers_own(
        self, assistant_service, lawyer, other_lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        """A total that included other people's threads would disclose how many
        of them exist."""
        for index in range(4):
            make_conversation(owner=other_lawyer, title=f"Leur {index}")

        _, total = assistant_service.list_conversations(ConversationListQuery(), actor=lawyer)

        assert total == 0


class TestListConversations:
    def test_archived_conversations_are_hidden_by_default(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        """Returning them by default would make archiving do nothing visible."""
        make_conversation(owner=lawyer, title="Active")
        make_conversation(
            owner=lawyer, title="Archivée", status=ConversationStatus.ARCHIVED
        )

        conversations, total = assistant_service.list_conversations(
            ConversationListQuery(), actor=lawyer
        )

        assert total == 1
        assert conversations[0].title == "Active"

    def test_archived_conversations_are_returned_when_asked_for(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        make_conversation(owner=lawyer, title="Archivée", status=ConversationStatus.ARCHIVED)

        conversations, _ = assistant_service.list_conversations(
            ConversationListQuery(status=ConversationStatus.ARCHIVED), actor=lawyer
        )

        assert [conversation.title for conversation in conversations] == ["Archivée"]

    def test_search_matches_the_title(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        make_conversation(owner=lawyer, title="Bail commercial")
        make_conversation(owner=lawyer, title="Contrat de travail")

        conversations, _ = assistant_service.list_conversations(
            ConversationListQuery(search="bail"), actor=lawyer
        )

        assert [conversation.title for conversation in conversations] == ["Bail commercial"]

    def test_a_brand_new_conversation_is_not_stranded_at_the_bottom(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        """It has no activity timestamp at all, and ordering by a nullable column
        alone would drop the one the user just created out of sight."""
        make_conversation(owner=lawyer, title="Ancienne")
        fresh = assistant_service.create_conversation(
            ConversationCreate(title="Nouvelle"), actor=lawyer
        )

        conversations, _ = assistant_service.list_conversations(
            ConversationListQuery(), actor=lawyer
        )

        assert conversations[0].id == fresh.id


class TestUpdateConversation:
    def test_renaming_marks_the_title_as_the_users(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = make_conversation(owner=lawyer)

        updated = assistant_service.update_conversation(
            conversation.id, ConversationUpdate(title="Mon titre"), actor=lawyer
        )

        assert updated.title == "Mon titre"
        assert updated.title_is_custom

    def test_archiving_leaves_the_title_alone(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        """An omitted field means "leave it alone" — a client that only archives
        must not silently clear the name."""
        conversation = make_conversation(owner=lawyer, title="Bail commercial")

        updated = assistant_service.update_conversation(
            conversation.id,
            ConversationUpdate(status=ConversationStatus.ARCHIVED),
            actor=lawyer,
        )

        assert updated.status is ConversationStatus.ARCHIVED
        assert updated.title == "Bail commercial"

    def test_restoring_makes_it_writable_again(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = make_conversation(
            owner=lawyer, status=ConversationStatus.ARCHIVED
        )

        updated = assistant_service.update_conversation(
            conversation.id, ConversationUpdate(status=ConversationStatus.ACTIVE), actor=lawyer
        )

        assert updated.status is ConversationStatus.ACTIVE

    def test_another_users_conversation_cannot_be_renamed(
        self, assistant_service, lawyer, other_lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        theirs = make_conversation(owner=other_lawyer)

        with pytest.raises(ConversationNotFoundError):
            assistant_service.update_conversation(
                theirs.id, ConversationUpdate(title="Détourné"), actor=lawyer
            )


class TestDeleteConversation:
    def test_deletion_is_logical(
        self, assistant_service, lawyer, make_conversation, db_session
    ) -> None:  # type: ignore[no-untyped-def]
        """The transcript carries the citations of advice that may have been
        acted on, so the row survives — it is simply unreachable."""
        from models.conversation import Conversation

        conversation = make_conversation(owner=lawyer)
        assistant_service.delete_conversation(conversation.id, actor=lawyer)

        row = db_session.get(Conversation, conversation.id)
        assert row is not None
        assert row.deleted_at is not None

    def test_a_second_delete_answers_not_found(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = make_conversation(owner=lawyer)
        assistant_service.delete_conversation(conversation.id, actor=lawyer)

        with pytest.raises(ConversationNotFoundError):
            assistant_service.delete_conversation(conversation.id, actor=lawyer)

    def test_another_users_conversation_cannot_be_deleted(
        self, assistant_service, lawyer, other_lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        theirs = make_conversation(owner=other_lawyer)

        with pytest.raises(ConversationNotFoundError):
            assistant_service.delete_conversation(theirs.id, actor=lawyer)


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #


FRENCH_PAGE = (
    "Le loyer mensuel doit être payé d'avance le premier jour de chaque mois. "
    "Tout retard de paiement entraîne une pénalité de cinq pour cent."
)


@pytest.fixture
def index_document(indexing_service, make_ocr_result):  # type: ignore[no-untyped-def]
    """Push a document through the **real** indexing pipeline into the store.

    Real rather than seeded, so a citation returned in these tests points at a
    passage that travelled extract → chunk → embed → store before being
    retrieved — the same fixture the RAG integration tests use.
    """

    def _index(document, pages: list[str]) -> None:  # type: ignore[no-untyped-def]
        indexing_service.schedule_for_ocr_result(
            make_ocr_result(document_id=document.id, pages=pages)
        )

    return _index


@pytest.fixture
def indexed_case(make_case, make_document, index_document, lawyer):  # type: ignore[no-untyped-def]
    """A case with one indexed French document, retrievable by the lawyer."""
    legal_case = make_case(assigned_lawyer_id=lawyer.id)
    document = make_document(
        case_id=legal_case.id, original_filename="bail-commercial.pdf", uploaded_by=lawyer.id
    )
    index_document(document, [FRENCH_PAGE])
    return legal_case


class TestSendMessage:
    def test_it_persists_both_turns(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id,
            MessageCreate(content="Quand le loyer est-il payable ?"),
            actor=lawyer,
        )

        assert exchange.user_message.role is ConversationRole.USER
        assert exchange.assistant_message.role is ConversationRole.ASSISTANT
        assert exchange.conversation.message_count == 2

    def test_the_sequence_is_contiguous_and_the_question_comes_first(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        first = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )
        second = assistant_service.send_message(
            conversation.id, MessageCreate(content="Et la pénalité de retard ?"), actor=lawyer
        )

        assert [first.user_message.sequence, first.assistant_message.sequence] == [1, 2]
        assert [second.user_message.sequence, second.assistant_message.sequence] == [3, 4]

    def test_the_question_is_stored_verbatim_not_resolved(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        """A user must read back exactly what they sent; the follow-up preamble
        is machinery, and showing it would show them words they did not write."""
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )
        assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Et le délai ?"), actor=lawyer
        )

        assert exchange.user_message.content == "Et le délai ?"

    def test_a_follow_up_is_resolved_against_the_earlier_question(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )
        assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer mensuel ?"), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Et le délai ?"), actor=lawyer
        )

        assert exchange.assistant_message.context_turns == 1
        # The earlier question reached the model, which is what "conversation
        # context" actually means here.
        assert "loyer mensuel" in llm_provider.calls[-1][1]

    def test_a_first_question_carries_no_context(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        assert exchange.assistant_message.context_turns == 0

    def test_the_first_question_titles_the_conversation(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id,
            MessageCreate(content="Quand le loyer est-il payable ?"),
            actor=lawyer,
        )

        assert exchange.conversation.title == "Quand le loyer est-il payable ?"

    def test_a_user_chosen_title_is_never_overwritten(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(title="Bail Atlas", case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        assert exchange.conversation.title == "Bail Atlas"

    def test_a_later_question_does_not_re_title(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )
        first = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )
        second = assistant_service.send_message(
            conversation.id, MessageCreate(content="Et la pénalité ?"), actor=lawyer
        )

        assert second.conversation.title == first.conversation.title

    def test_the_citations_are_the_pipelines_own(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        """*"Display citations without modifying them"* — so what is stored is
        the pipeline's citation objects serialized, with its four references."""
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        assert exchange.assistant_message.citations
        citation = exchange.assistant_message.citations[0]
        assert {"document_id", "document_version", "page_number", "case_id"} <= set(citation)

    def test_the_conversations_case_becomes_the_default_filter(
        self, assistant_service, lawyer, indexed_case, make_case, make_document, index_document
    ) -> None:  # type: ignore[no-untyped-def]
        """A conversation about one matter must not answer from another."""
        other = make_case(assigned_lawyer_id=lawyer.id, case_number="CASE-2026-9999")
        document = make_document(
            case_id=other.id, uploaded_by=lawyer.id, original_filename="autre.pdf"
        )
        index_document(document, [FRENCH_PAGE])

        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )
        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        cases = {citation["case_id"] for citation in exchange.assistant_message.citations}
        assert cases == {str(indexed_case.id)}

    def test_an_explicit_filter_overrides_the_conversations_case(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id,
            MessageCreate(
                content="Quel est le loyer ?",
                filters=SearchFilterInput(case_id=indexed_case.id),
            ),
            actor=lawyer,
        )

        assert exchange.assistant_message.citations

    def test_a_filter_naming_an_inaccessible_case_is_refused_by_the_pipeline(
        self, assistant_service, lawyer, indexed_case, make_case, other_lawyer
    ) -> None:  # type: ignore[no-untyped-def]
        """Passed through **unchanged** from the search service: a second
        authorization rule here would be a second one to keep in step."""
        theirs = make_case(
            assigned_lawyer_id=other_lawyer.id, case_number="CASE-2026-8888"
        )
        conversation = assistant_service.create_conversation(
            ConversationCreate(), actor=lawyer
        )

        with pytest.raises(SearchAccessDeniedError):
            assistant_service.send_message(
                conversation.id,
                MessageCreate(
                    content="Quel est le loyer ?",
                    filters=SearchFilterInput(case_id=theirs.id),
                ),
                actor=lawyer,
            )

    def test_an_unassigned_lawyer_is_answered_from_nothing(
        self, assistant_service, indexed_case, make_user
    ) -> None:  # type: ignore[no-untyped-def]
        """Inherited from the search service: "assigned to no cases" is an empty
        scope that matches nothing, never an absent filter that matches all."""
        outsider = make_user(email="outsider@example.com", role=UserRole.LAWYER)
        conversation = assistant_service.create_conversation(
            ConversationCreate(), actor=outsider
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=outsider
        )

        assert exchange.assistant_message.grounded is False
        assert exchange.assistant_message.insufficient_evidence is True
        assert exchange.assistant_message.citations == []

    def test_an_answer_with_no_evidence_is_still_a_stored_message(
        self, assistant_service, lawyer, make_user
    ) -> None:  # type: ignore[no-untyped-def]
        """It is a successful outcome, not an error: it is persisted, shown, and
        rateable like any other."""
        conversation = assistant_service.create_conversation(
            ConversationCreate(), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        assert exchange.assistant_message.content
        assert exchange.conversation.message_count == 2

    def test_a_model_that_declines_produces_an_ungrounded_message(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        assert exchange.assistant_message.grounded is False
        # The sentinel never reaches the transcript as text.
        assert INSUFFICIENT_EVIDENCE_MARKER not in exchange.assistant_message.content
        assert exchange.assistant_message.citations == []

    def test_an_archived_conversation_refuses_new_messages(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = make_conversation(
            owner=lawyer, status=ConversationStatus.ARCHIVED
        )

        with pytest.raises(ConversationArchivedError):
            assistant_service.send_message(
                conversation.id, MessageCreate(content="Une question ?"), actor=lawyer
            )

    def test_a_full_conversation_refuses_new_messages(
        self, assistant_service, lawyer, make_conversation, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(settings, "ASSISTANT_MAX_MESSAGES", 2)
        conversation = make_conversation(owner=lawyer)
        conversation.message_count = 2

        with pytest.raises(ConversationFullError):
            assistant_service.send_message(
                conversation.id, MessageCreate(content="Une question ?"), actor=lawyer
            )

    def test_a_message_cannot_be_sent_to_another_users_conversation(
        self, assistant_service, lawyer, other_lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        theirs = make_conversation(owner=other_lawyer)

        with pytest.raises(ConversationNotFoundError):
            assistant_service.send_message(
                theirs.id, MessageCreate(content="Une question ?"), actor=lawyer
            )

    def test_a_provider_failure_surfaces_as_an_unavailability(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        llm_provider.raises = LLMUnavailableError("no key")
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        with pytest.raises(RagUnavailableError):
            assistant_service.send_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )

    def test_a_failed_message_leaves_the_blocking_transcript_untouched(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        """The blocking path writes both turns together, so a failure writes
        neither — a transcript never holds a question whose answer was lost."""
        llm_provider.raises = LLMUnavailableError("no key")
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        with pytest.raises(RagUnavailableError):
            assistant_service.send_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )

        _, total = assistant_service.list_messages(
            conversation.id, offset=0, limit=50, actor=lawyer
        )
        assert total == 0


class TestStreamMessage:
    def test_it_emits_retrieval_then_deltas_then_final(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        from services.rag import RagStreamEventKind

        llm_provider.stream_chunks = ["Le loyer ", "est payable ", "le 1er [1]."]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        kinds = [
            event.kind
            for event in assistant_service.stream_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )
        ]

        assert kinds[0] is RagStreamEventKind.RETRIEVAL
        assert kinds[-1] is RagStreamEventKind.FINAL
        assert kinds.count(RagStreamEventKind.DELTA) >= 1

    def test_the_stream_persists_both_turns(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        llm_provider.stream_chunks = ["Le loyer est payable le 1er [1]."]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        list(
            assistant_service.stream_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )
        )

        messages, total = assistant_service.list_messages(
            conversation.id, offset=0, limit=50, actor=lawyer
        )
        assert total == 2
        assert [message.role for message in messages] == [
            ConversationRole.USER,
            ConversationRole.ASSISTANT,
        ]

    def test_the_question_survives_a_stream_that_fails(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        """A browser that vanishes mid-stream, or a provider that dies mid-answer,
        must not lose the question the user already sent and saw echoed."""
        llm_provider.stream_chunks = ["Le loyer ", "est ", "payable"]
        llm_provider.stream_raises = LLMUnavailableError("gone")
        llm_provider.stream_raises_after = 1

        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        with pytest.raises(RagUnavailableError):
            list(
                assistant_service.stream_message(
                    conversation.id,
                    MessageCreate(content="Quel est le loyer ?"),
                    actor=lawyer,
                )
            )

        messages, total = assistant_service.list_messages(
            conversation.id, offset=0, limit=50, actor=lawyer
        )
        assert total == 1
        assert messages[0].role is ConversationRole.USER

    def test_it_falls_back_when_the_provider_cannot_stream(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        """The spec's *"gracefully fall back to non-streaming responses"*. The
        caller cannot tell, except by the number of fragments."""
        from services.rag import RagStreamEventKind

        llm_provider.stream_raises = LLMUnavailableError("streaming not supported")
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        events = list(
            assistant_service.stream_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )
        )

        final = events[-1]
        assert final.kind is RagStreamEventKind.FINAL
        assert final.outcome is not None
        assert final.outcome.grounded

    def test_the_switch_serves_the_endpoint_from_the_blocking_pipeline(
        self, assistant_service, lawyer, indexed_case, llm_provider, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """``ASSISTANT_STREAMING_ENABLED=false`` has to change something on the
        *server*: an operator who turns streaming off because a proxy buffers
        responses needs the API to stop streaming, and a client cannot be trusted
        to honour a setting it merely reads."""
        from services.rag import RagStreamEventKind

        monkeypatch.setattr(settings, "ASSISTANT_STREAMING_ENABLED", False)
        llm_provider.stream_chunks = ["Le loyer ", "est payable ", "le 1er [1]."]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        events = list(
            assistant_service.stream_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )
        )

        assert llm_provider.stream_calls == 0
        # The same event *shape*, so a client needs no branch for it.
        assert [event.kind for event in events] == [
            RagStreamEventKind.RETRIEVAL,
            RagStreamEventKind.DELTA,
            RagStreamEventKind.FINAL,
        ]

    def test_an_unstreamed_answer_is_not_counted_as_streamed(
        self, assistant_service, lawyer, indexed_case, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The metric exists to make a silent degradation visible; counting this
        would defeat it."""
        monkeypatch.setattr(settings, "ASSISTANT_STREAMING_ENABLED", False)
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        list(
            assistant_service.stream_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )
        )

        metrics = assistant_service.health().metrics
        assert metrics.successful_requests == 1
        assert metrics.streamed_requests == 0

    def test_the_switch_still_persists_both_turns(
        self, assistant_service, lawyer, indexed_case, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(settings, "ASSISTANT_STREAMING_ENABLED", False)
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        list(
            assistant_service.stream_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )
        )

        _, total = assistant_service.list_messages(
            conversation.id, offset=0, limit=50, actor=lawyer
        )
        assert total == 2

    def test_the_refusal_sentinel_is_never_streamed_as_text(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        """A reader must never see ``INSUFFICIENT_EVID…`` flash by and then be
        replaced by a paragraph — it looks like a malfunction and exposes an
        internal token."""
        from services.rag import RagStreamEventKind

        llm_provider.stream_chunks = ["INSUFFICIENT", "_EVIDENCE"]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        events = list(
            assistant_service.stream_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )
        )

        deltas = "".join(
            event.text for event in events if event.kind is RagStreamEventKind.DELTA
        )
        assert INSUFFICIENT_EVIDENCE_MARKER not in deltas
        assert deltas == ""


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


class TestSuggestions:
    def test_a_grounded_answer_carries_suggestions(
        self, assistant_service, lawyer, indexed_case, follow_up_suggester
    ) -> None:  # type: ignore[no-untyped-def]
        follow_up_suggester.suggestions = ["Quelle est la durée du bail ?"]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        assert exchange.assistant_message.suggestions == ["Quelle est la durée du bail ?"]

    def test_an_ungrounded_answer_carries_none(
        self, assistant_service, lawyer, follow_up_suggester
    ) -> None:  # type: ignore[no-untyped-def]
        """Suggestions must never invent unsupported facts, and an answer that
        found no supporting document supports no follow-up either."""
        conversation = assistant_service.create_conversation(
            ConversationCreate(), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        assert exchange.assistant_message.suggestions == []

    def test_a_suggester_failure_never_costs_the_answer(
        self, assistant_service, lawyer, indexed_case, follow_up_suggester
    ) -> None:  # type: ignore[no-untyped-def]
        """An answer the user is already waiting for must not be lost to a
        failure in the convenience that follows it."""
        follow_up_suggester.raises = RuntimeError("suggester exploded")
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        exchange = assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        assert exchange.assistant_message.content
        assert exchange.assistant_message.suggestions == []

    def test_the_suggester_never_receives_a_way_to_reach_a_document(
        self, assistant_service, lawyer, indexed_case, follow_up_suggester
    ) -> None:  # type: ignore[no-untyped-def]
        """It is handed one exchange — question, answer, citations, language —
        all of which already passed through the pipeline's authorization."""
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )
        assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        question, answer, citations, language = follow_up_suggester.calls[-1]
        assert question == "Quel est le loyer ?"
        assert answer
        assert citations >= 1
        assert language == "fr"


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #


class TestFeedback:
    @pytest.fixture
    def answered(self, assistant_service, lawyer, indexed_case):  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )
        return assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

    def test_an_answer_can_be_rated(self, assistant_service, lawyer, answered) -> None:  # type: ignore[no-untyped-def]
        feedback = assistant_service.submit_feedback(
            answered.conversation.id,
            answered.assistant_message.id,
            FeedbackCreate(rating=FeedbackRating.HELPFUL),
            actor=lawyer,
        )

        assert feedback.rating is FeedbackRating.HELPFUL
        assert feedback.rated_by == lawyer.id

    def test_rating_does_not_change_the_conversation_history(
        self, assistant_service, lawyer, answered
    ) -> None:  # type: ignore[no-untyped-def]
        """*"Feedback should not modify conversation history."* Structural rather
        than remembered: a rating writes to a table the transcript is not read
        from."""
        before = answered.assistant_message.content
        before_count = answered.conversation.message_count

        assistant_service.submit_feedback(
            answered.conversation.id,
            answered.assistant_message.id,
            FeedbackCreate(rating=FeedbackRating.NOT_HELPFUL, comment="Trop vague"),
            actor=lawyer,
        )

        messages, total = assistant_service.list_messages(
            answered.conversation.id, offset=0, limit=50, actor=lawyer
        )
        assert total == before_count
        assert messages[-1].content == before

    def test_rating_twice_updates_rather_than_appends(
        self, assistant_service, lawyer, answered
    ) -> None:  # type: ignore[no-untyped-def]
        """Two contradictory ratings of one answer by one person is not an
        evaluation signal, it is noise."""
        assistant_service.submit_feedback(
            answered.conversation.id,
            answered.assistant_message.id,
            FeedbackCreate(rating=FeedbackRating.HELPFUL),
            actor=lawyer,
        )
        updated = assistant_service.submit_feedback(
            answered.conversation.id,
            answered.assistant_message.id,
            FeedbackCreate(rating=FeedbackRating.NOT_HELPFUL),
            actor=lawyer,
        )

        health = assistant_service.health()
        assert updated.rating is FeedbackRating.NOT_HELPFUL
        assert health.total_feedback == 1

    def test_rating_your_own_question_is_refused(
        self, assistant_service, lawyer, answered
    ) -> None:  # type: ignore[no-untyped-def]
        """Not a judgement about the assistant, and accepting it would put noise
        into the evaluation data the spec asks to persist."""
        with pytest.raises(InvalidFeedbackTargetError):
            assistant_service.submit_feedback(
                answered.conversation.id,
                answered.user_message.id,
                FeedbackCreate(rating=FeedbackRating.HELPFUL),
                actor=lawyer,
            )

    def test_an_unknown_message_is_not_found(
        self, assistant_service, lawyer, answered
    ) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ConversationMessageNotFoundError):
            assistant_service.submit_feedback(
                answered.conversation.id,
                uuid.uuid4(),
                FeedbackCreate(rating=FeedbackRating.HELPFUL),
                actor=lawyer,
            )

    def test_another_users_message_cannot_be_rated(
        self, assistant_service, other_lawyer, answered
    ) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ConversationNotFoundError):
            assistant_service.submit_feedback(
                answered.conversation.id,
                answered.assistant_message.id,
                FeedbackCreate(rating=FeedbackRating.HELPFUL),
                actor=other_lawyer,
            )

    def test_withdrawing_removes_the_rating(
        self, assistant_service, lawyer, answered
    ) -> None:  # type: ignore[no-untyped-def]
        assistant_service.submit_feedback(
            answered.conversation.id,
            answered.assistant_message.id,
            FeedbackCreate(rating=FeedbackRating.HELPFUL),
            actor=lawyer,
        )
        assistant_service.withdraw_feedback(
            answered.conversation.id, answered.assistant_message.id, actor=lawyer
        )

        assert assistant_service.health().total_feedback == 0

    def test_withdrawing_nothing_is_a_success(
        self, assistant_service, lawyer, answered
    ) -> None:  # type: ignore[no-untyped-def]
        """The end state asked for is the one that already holds."""
        assistant_service.withdraw_feedback(
            answered.conversation.id, answered.assistant_message.id, actor=lawyer
        )


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestHealth:
    def test_conversation_counts_come_from_the_database(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        """They are properties of persisted rows: counting them in a process
        would reset on restart *and* be wrong."""
        make_conversation(owner=lawyer, title="Une")
        make_conversation(owner=lawyer, title="Deux", status=ConversationStatus.ARCHIVED)

        health = assistant_service.health()

        assert health.active_conversations == 1
        assert health.archived_conversations == 1
        assert health.total_conversations == 2

    def test_a_deleted_conversation_stops_being_counted(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = make_conversation(owner=lawyer)
        assistant_service.delete_conversation(conversation.id, actor=lawyer)

        assert assistant_service.health().total_conversations == 0

    def test_a_successful_message_is_counted_once(
        self, assistant_service, lawyer, indexed_case
    ) -> None:  # type: ignore[no-untyped-def]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )
        assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        metrics = assistant_service.health().metrics
        assert metrics.total_requests == 1
        assert metrics.successful_requests == 1
        assert metrics.failed_requests == 0

    def test_a_no_evidence_answer_is_a_success(
        self, assistant_service, lawyer
    ) -> None:  # type: ignore[no-untyped-def]
        """Declining is the assistant working, not failing. Counting it as a
        failure would make the failure rate a measure of the corpus."""
        conversation = assistant_service.create_conversation(
            ConversationCreate(), actor=lawyer
        )
        assistant_service.send_message(
            conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
        )

        metrics = assistant_service.health().metrics
        assert metrics.successful_requests == 1
        assert metrics.insufficient_evidence == 1
        assert metrics.grounded_answers == 0

    def test_a_dependency_failure_is_counted_with_its_cause(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        llm_provider.raises = LLMUnavailableError("no key")
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        with pytest.raises(RagUnavailableError):
            assistant_service.send_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )

        metrics = assistant_service.health().metrics
        assert metrics.failed_requests == 1
        assert metrics.failures_by_code == {RagFailureCode.LLM_UNAVAILABLE.value: 1}

    def test_a_rejected_request_is_not_counted_as_a_failure(
        self, assistant_service, lawyer, make_conversation
    ) -> None:  # type: ignore[no-untyped-def]
        """The failure rate is a health signal, and an archived conversation says
        nothing about whether the platform is healthy."""
        conversation = make_conversation(
            owner=lawyer, status=ConversationStatus.ARCHIVED
        )

        with pytest.raises(ConversationArchivedError):
            assistant_service.send_message(
                conversation.id, MessageCreate(content="Une question ?"), actor=lawyer
            )

        metrics = assistant_service.health().metrics
        assert metrics.total_requests == 0
        assert metrics.failed_requests == 0

    def test_a_streamed_message_is_counted_as_streamed(
        self, assistant_service, lawyer, indexed_case, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        llm_provider.stream_chunks = ["Le loyer est payable le 1er [1]."]
        conversation = assistant_service.create_conversation(
            ConversationCreate(case_id=indexed_case.id), actor=lawyer
        )

        list(
            assistant_service.stream_message(
                conversation.id, MessageCreate(content="Quel est le loyer ?"), actor=lawyer
            )
        )

        metrics = assistant_service.health().metrics
        assert metrics.streamed_requests == 1


# --------------------------------------------------------------------------- #
# Scope of the feature
# --------------------------------------------------------------------------- #


class TestScope:
    def test_the_service_holds_no_retrieval_collaborator(self, assistant_service) -> None:  # type: ignore[no-untyped-def]
        """Structural rather than a matter of discipline: there is no path from
        the assistant to a passage that does not pass through the pipeline."""
        held = {type(value).__name__ for value in vars(assistant_service).values()}

        assert not {"SearchService", "InMemoryVectorSearcher", "QdrantVectorSearcher"} & held
        assert not {"FakeEmbedder", "SentenceTransformerEmbedder"} & held
        assert not {"DocumentRepository", "SearchRepository"} & held

    def test_the_module_imports_no_retrieval_or_prompt_machinery(self) -> None:
        """The spec forbids duplicating retrieval, prompt construction, and
        orchestration; an import is where the first of them would appear."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "apps"
            / "api"
            / "services"
            / "assistant.py"
        ).read_text(encoding="utf-8")

        for forbidden in (
            "from services.search import",
            "from services.vector_search import",
            "from services.embedding import",
            "from services.prompts import",
            "from services.llm import",
        ):
            assert forbidden not in source
