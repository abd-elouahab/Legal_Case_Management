"""AI Legal Assistant business logic.

Owns the flow ``13-ai-legal-assistant.md`` defines, in exactly the order its
diagram gives:

.. code-block:: text

    User → Chat Interface → Conversation Manager → RAG Pipeline
         → Grounded Response → Persist Conversation → Return Response

Scope boundaries, kept deliberately sharp — the spec's own list of what this
feature must **not** re-implement is retrieval, prompt construction, and
orchestration, and all three are absent here by construction:

* **Nothing is retrieved here, and nothing can be.** This service holds no vector
  searcher, no embedder, no search service, and no document repository. Its only
  route to a passage is :meth:`~services.rag.RagService.answer`, which retrieves
  through :class:`~services.search.SearchService`, which scopes every result to
  the caller's cases. That single fact is the whole of this feature's *document*
  authorization story, inherited rather than restated.
* **No prompt is built here**, and no template is named — except the one for
  follow-up suggestions, which is a *new* prompt for a purpose the pipeline does
  not serve, rendered through the same library (see :mod:`services.suggestions`).
* **No model is called here.** The pipeline calls one; this service relays what
  it returns.
* **No citation is constructed, reordered, or edited.** The spec says the
  assistant *"should display citations without modifying them"*, and what is
  persisted is the pipeline's own citation objects, serialized.

What it *does* own is everything the pipeline deliberately refused: that a
conversation belongs to one user and to nobody else, that every message is
persisted, that a follow-up question is resolved against what came before it,
that a title comes from somewhere, and that an answer can be rated.

**Ownership is enforced by the repository's queries, not by a policy module.**
There is no ``assistant_access.py``, and its absence is the same kind of decision
as the missing ``rag_access.py``: every read in
:class:`~repositories.conversation.ConversationRepository` takes an ``owner_id``,
so there is no query in the platform that can return another user's conversation
and therefore no rule for a second module to keep in step with.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from core.assistant import (
    ConversationRole,
    derive_title,
    history_questions,
    message_preview,
    resolve_question,
    untitled_conversation,
)
from core.config import settings
from core.exceptions import (
    AppException,
    AssistantDisabledError,
    ConversationArchivedError,
    ConversationFullError,
    ConversationMessageNotFoundError,
    ConversationNotFoundError,
    InvalidFeedbackTargetError,
    RagUnavailableError,
)
from core.localization import resolve_language
from core.rag import RagFailureCode, question_fingerprint, resolve_answer_language
from models.conversation import (
    Conversation,
    ConversationMessage,
    ConversationStatus,
    MessageFeedback,
)
from models.user import User
from repositories.conversation import ConversationRepository
from schemas.conversation import (
    ConversationCreate,
    ConversationListQuery,
    ConversationUpdate,
    FeedbackCreate,
    MessageCreate,
)
from schemas.rag import RagCitationRead, RagRequest
from schemas.search import SearchFilterInput
from services.assistant_metrics import (
    AssistantMetricsRecorder,
    AssistantMetricsSnapshot,
    NullAssistantMetrics,
)
from services.localization import LanguageDirectory, chosen_language
from services.rag import RagOutcome, RagService, RagStreamEvent, RagStreamEventKind
from services.suggestions import FollowUpSuggester, NullFollowUpSuggester

logger = structlog.get_logger(__name__)

#: Characters the follow-up preamble and its labels add to a resolved question.
#:
#: Reserved out of the pipeline's question budget before any history is carried,
#: so a resolved follow-up can never be refused by the very endpoint that built
#: it. Deliberately generous: over-reserving costs a few characters of carried
#: context, under-reserving costs the user their question.
CONTEXT_OVERHEAD_CHARACTERS = 64


@dataclass(frozen=True, slots=True)
class MessageExchange:
    """One completed turn: the question as stored, and the answer.

    Returned instead of the schema so the router owns serialization and the
    service owns the workflow — the same split every other service here makes.
    """

    conversation: Conversation
    user_message: ConversationMessage
    assistant_message: ConversationMessage


@dataclass(frozen=True, slots=True)
class AssistantHealth:
    """Platform-wide assistant health, as the monitoring endpoint reports it.

    Two halves with different provenance and different limits — the counters come
    from the process, the conversation and feedback figures from the database.
    See :mod:`services.assistant_metrics` for why neither could sensibly come
    from the other.
    """

    metrics: AssistantMetricsSnapshot

    total_conversations: int
    active_conversations: int
    archived_conversations: int
    total_messages: int
    assistant_messages: int

    total_feedback: int
    helpful_feedback: int
    not_helpful_feedback: int

    suggestions_enabled: bool
    streaming_enabled: bool
    enabled: bool


class AssistantService:
    """Manages conversations, and delegates every answer to the RAG pipeline."""

    def __init__(
        self,
        conversations: ConversationRepository,
        rag: RagService,
        *,
        suggester: FollowUpSuggester | None = None,
        metrics: AssistantMetricsRecorder | None = None,
        languages: LanguageDirectory | None = None,
    ) -> None:
        self._conversations = conversations
        self._rag = rag
        # Both default to the null implementation for the reason `TimelineService`'s
        # null recorder does: a service constructed by a script or a unit test
        # should not need observability or a language model to work. The
        # application wires the real ones in `api.deps.get_assistant_service`.
        self._suggester = suggester or NullFollowUpSuggester()
        self._metrics: AssistantMetricsRecorder = metrics or NullAssistantMetrics()
        # ``21-localization.md``: *"the AI Assistant should respond in the user's
        # preferred language by default"*. This is the only collaborator that can
        # answer what that preference **is** — and it is the one-method
        # `LanguageDirectory` rather than a settings service, so the assistant can
        # ask which language somebody reads in and nothing else about them.
        # ``None`` leaves the pre-Localization behaviour intact: detection from the
        # question, then the application default.
        self._languages = languages

    # -------------------------------------------------------- conversations #

    def create_conversation(self, payload: ConversationCreate, *, actor: User) -> Conversation:
        """Open a new conversation for this user.

        The title is the user's if they supplied one, and a placeholder otherwise
        — **not** a guess at a subject nothing has been said about yet. It is
        replaced by the first question's opening words when that question
        arrives, unless the user named it, which is what ``title_is_custom``
        records.

        A ``case_id`` here is a **default retrieval filter and a label, never a
        grant**: it is not validated against the caller's assignments, because
        the check that matters happens on every message, inside the search
        service, which refuses a case they are not party to. Validating it twice
        would be a second rule to keep in step with the first — and validating it
        *only* here would be the dangerous half of that pair.

        Raises:
            AssistantDisabledError: the assistant is switched off here.
        """
        self._require_enabled(actor=actor)

        language = payload.language
        # The **stored** language stays exactly what the request said — ``None``
        # when it said nothing — so a conversation opened today is not frozen to
        # the preference somebody held today. What the reader actually gets is
        # resolved per message by `_build_request`, which is what makes changing
        # the Language setting take effect on threads that already exist.
        #
        # The placeholder title is the one thing that has to be written *now*, so
        # it uses the caller's current preference.
        conversation = Conversation(
            owner_id=actor.id,
            title=payload.title
            or untitled_conversation(
                resolve_language(language, self._preferred_language(actor))
            ),
            title_is_custom=payload.title is not None,
            status=ConversationStatus.ACTIVE,
            language=language,
            case_id=payload.case_id,
        )
        # Stamped here rather than left to the column's server default, for the
        # reason `TimelineService` records about its own ordering column: the
        # list is ordered by activity and then by creation, a conversation with
        # no messages has no activity at all, and a database ``now()`` is the
        # *transaction's* start time at whatever precision the server keeps —
        # one second, on some. Two conversations opened in the same second would
        # then tie and fall back to a random-UUID tiebreak, which would strand
        # the one the user just created somewhere in the middle of their list.
        conversation.created_at = datetime.now(UTC)
        created = self._conversations.add(conversation)

        logger.info(
            "conversation_created",
            conversation_id=str(created.id),
            actor_id=str(actor.id),
            role=actor.role.value,
            has_case=created.case_id is not None,
            language=language,
            custom_title=created.title_is_custom,
        )
        return created

    def list_conversations(
        self, query: ConversationListQuery, *, actor: User
    ) -> tuple[list[Conversation], int]:
        """Return one page of **this user's** conversations, and the total.

        The scope is the repository's, applied in SQL, so the page totals count
        only what the caller owns — a count that included other people's threads
        would disclose how many of them exist.
        """
        return self._conversations.list_conversations(query, owner_id=actor.id)

    def get_conversation(self, conversation_id: uuid.UUID, *, actor: User) -> Conversation:
        """Return one of this user's conversations.

        Raises:
            ConversationNotFoundError: no such conversation belongs to this user.
                Deliberately the same answer for "does not exist", "was deleted",
                and "belongs to someone else" — see the section note in
                :mod:`core.exceptions`.
        """
        conversation = self._conversations.get(conversation_id, owner_id=actor.id)
        if conversation is None:
            logger.info(
                "conversation_access_denied",
                conversation_id=str(conversation_id),
                actor_id=str(actor.id),
                role=actor.role.value,
            )
            raise ConversationNotFoundError
        return conversation

    def update_conversation(
        self, conversation_id: uuid.UUID, payload: ConversationUpdate, *, actor: User
    ) -> Conversation:
        """Rename a conversation, archive it, or restore it.

        Omitted fields are left alone, which is why the caller passes a payload
        built with ``exclude_unset``: sending ``title: null`` and not sending
        ``title`` at all must not be the same request.

        Renaming sets ``title_is_custom``, permanently. A user who names a thread
        has said what it is about, and no later automatic titling may overrule
        them.

        Raises:
            ConversationNotFoundError: no such conversation belongs to this user.
        """
        conversation = self.get_conversation(conversation_id, actor=actor)
        changes = payload.model_dump(exclude_unset=True)

        if "title" in changes and changes["title"] is not None:
            conversation.title = changes["title"]
            conversation.title_is_custom = True

        if "status" in changes and changes["status"] is not None:
            conversation.status = ConversationStatus(changes["status"])

        updated = self._conversations.save(conversation)

        logger.info(
            "conversation_updated",
            conversation_id=str(updated.id),
            actor_id=str(actor.id),
            renamed="title" in changes,
            status=updated.status.value,
        )
        return updated

    def delete_conversation(self, conversation_id: uuid.UUID, *, actor: User) -> None:
        """Withdraw a conversation.

        Logical, for the reason :class:`~models.conversation.Conversation` records:
        the transcript carries the citations of advice a lawyer may have acted on.
        The conversation stops being readable through every endpoint immediately —
        the repository excludes deleted rows from every query — and a future
        retention job is what reclaims the storage.

        Idempotent: deleting an already-deleted conversation answers 404, because
        by then there is no conversation of that identifier belonging to anyone.

        Raises:
            ConversationNotFoundError: no such conversation belongs to this user.
        """
        conversation = self.get_conversation(conversation_id, actor=actor)
        self._conversations.soft_delete(conversation)

        logger.info(
            "conversation_deleted",
            conversation_id=str(conversation_id),
            actor_id=str(actor.id),
            role=actor.role.value,
            message_count=conversation.message_count,
        )

    # -------------------------------------------------------------- reading #

    def list_messages(
        self, conversation_id: uuid.UUID, *, offset: int, limit: int, actor: User
    ) -> tuple[list[ConversationMessage], int]:
        """Return one page of a conversation's transcript, oldest first.

        Resolving the conversation first is what scopes this: a message can only
        be reached through a conversation this user owns.

        Raises:
            ConversationNotFoundError: no such conversation belongs to this user.
        """
        conversation = self.get_conversation(conversation_id, actor=actor)
        return self._conversations.list_messages(conversation.id, offset=offset, limit=limit)

    # ------------------------------------------------------------- messaging #

    def send_message(
        self, conversation_id: uuid.UUID, payload: MessageCreate, *, actor: User
    ) -> MessageExchange:
        """Ask one question and store the answer.

        The whole exchange, in the order the spec's flow diagram gives it: resolve
        the conversation, resolve the question against its history, hand it to the
        RAG pipeline, persist both turns, and return them.

        **The question and its answer are written in one transaction**, so a
        transcript never contains a question whose answer was lost — a
        conversation the user could neither resume nor make sense of.

        **Metrics are recorded here and nowhere else** on this path, which is what
        guarantees exactly one record per message.

        Raises:
            AssistantDisabledError: the assistant is switched off here.
            ConversationNotFoundError: no such conversation belongs to this user.
            ConversationArchivedError: the conversation is archived.
            ConversationFullError: the conversation has reached its ceiling.
            InvalidQuestionError: the question carries nothing to answer.
            SearchAccessDeniedError: a filter named a case or document the caller
                is not party to. Raised by the search service beneath the
                pipeline and passed through **unchanged** — a second
                authorization rule here would be a second one to keep in step.
            RagUnavailableError: retrieval, the prompt library, or the language
                model could not answer.
        """
        self._require_enabled(actor=actor)
        conversation = self._open_conversation(conversation_id, actor=actor)

        started = time.monotonic()
        request, turns = self._build_request(conversation, payload, actor=actor)

        logger.info(
            "assistant_message_received",
            conversation_id=str(conversation.id),
            actor_id=str(actor.id),
            role=actor.role.value,
            streamed=False,
            context_turns=turns,
            question_fingerprint=question_fingerprint(payload.content),
            question_length=len(payload.content),
        )

        try:
            outcome = self._rag.answer(request, actor=actor)
        except Exception as exc:
            self._record_failure(exc, conversation=conversation, actor=actor, started=started)
            raise

        exchange = self._persist_exchange(
            conversation,
            question=payload.content,
            outcome=outcome,
            turns=turns,
            actor=actor,
        )

        self._record_success(
            outcome, conversation=exchange.conversation, actor=actor, started=started, streamed=False
        )
        return exchange

    def stream_message(
        self, conversation_id: uuid.UUID, payload: MessageCreate, *, actor: User
    ) -> Iterator[RagStreamEvent]:
        """Ask one question and emit the answer as it is produced.

        The same exchange as :meth:`send_message`, delegating to
        :meth:`~services.rag.RagService.stream` instead of
        :meth:`~services.rag.RagService.answer` — so retrieval, prompt
        construction, generation, verification, and citation are, again, entirely
        the pipeline's.

        **Two differences from the blocking path, and both are about a client
        that goes away mid-answer.**

        * The **question is persisted before the answer exists**. A browser that
          closes its connection halfway through a stream would otherwise lose the
          question it had already sent and seen echoed on screen. The blocking
          path writes both together because there is no window in which one can
          arrive without the other.
        * A run that fails after the question is stored leaves a **question with
          no answer**, which is exactly what happened and what the transcript
          should say. The user retries, which appends a new turn rather than
          silently rewriting the failed one.

        Falls back to a whole answer when the provider cannot stream — see
        :meth:`~services.rag.RagService._generate_streaming`. The caller cannot
        tell the difference except by the number of fragments, which is the point.

        Raises:
            The same exceptions as :meth:`send_message`, at the point in the
            iteration where the corresponding stage runs.
        """
        self._require_enabled(actor=actor)
        conversation = self._open_conversation(conversation_id, actor=actor)

        started = time.monotonic()
        request, turns = self._build_request(conversation, payload, actor=actor)

        logger.info(
            "assistant_message_received",
            conversation_id=str(conversation.id),
            actor_id=str(actor.id),
            role=actor.role.value,
            streamed=True,
            context_turns=turns,
            question_fingerprint=question_fingerprint(payload.content),
            question_length=len(payload.content),
        )

        question_message = self._persist_question(conversation, question=payload.content)

        if not settings.ASSISTANT_STREAMING_ENABLED:
            yield from self._unstreamed(
                conversation,
                request,
                question=question_message,
                turns=turns,
                actor=actor,
                started=started,
            )
            return

        try:
            for event in self._rag.stream(request, actor=actor):
                if event.kind is not RagStreamEventKind.FINAL:
                    yield event
                    continue

                outcome = event.outcome
                if outcome is None:  # pragma: no cover - defensive
                    raise RagUnavailableError(
                        RagFailureCode.MALFORMED_RESPONSE.value,
                        "The AI service returned an unusable answer. Try asking again.",
                    )

                self._persist_answer(
                    conversation, question=question_message, outcome=outcome, turns=turns
                )
                self._record_success(
                    outcome,
                    conversation=conversation,
                    actor=actor,
                    started=started,
                    streamed=True,
                )
                yield event
        except Exception as exc:
            self._record_failure(exc, conversation=conversation, actor=actor, started=started)
            raise

    def _unstreamed(
        self,
        conversation: Conversation,
        request: RagRequest,
        *,
        question: ConversationMessage,
        turns: int,
        actor: User,
        started: float,
    ) -> Iterator[RagStreamEvent]:
        """Serve the streaming endpoint from the blocking pipeline.

        What ``ASSISTANT_STREAMING_ENABLED=false`` actually does. The switch has
        to change something on the **server**, or it is documentation for a
        behaviour that does not exist: a client cannot be trusted to honour it,
        and an operator who turns streaming off because a proxy in front of the
        API buffers responses needs the API to stop streaming.

        The event sequence is deliberately identical in *shape* — one
        ``retrieval``, one ``delta`` carrying the whole answer, one ``final`` —
        so a client needs no branch for it. That is the same indistinguishability
        a provider which cannot stream produces, and it is why the frames are
        emitted at all rather than the endpoint answering something else: an
        operator's switch must not become a second response format.

        Raises:
            The same exceptions as :meth:`send_message`.
        """
        try:
            outcome = self._rag.answer(request, actor=actor)
        except Exception as exc:
            self._record_failure(exc, conversation=conversation, actor=actor, started=started)
            raise

        self._persist_answer(conversation, question=question, outcome=outcome, turns=turns)
        # Recorded as **not streamed**, because it was not. The metric exists to
        # make a silent degradation visible, and counting this would defeat it.
        self._record_success(
            outcome, conversation=conversation, actor=actor, started=started, streamed=False
        )

        yield RagStreamEvent(
            kind=RagStreamEventKind.RETRIEVAL, retrieved_count=outcome.retrieved_count
        )
        if outcome.answer:
            yield RagStreamEvent(kind=RagStreamEventKind.DELTA, text=outcome.answer)
        yield RagStreamEvent(kind=RagStreamEventKind.FINAL, outcome=outcome)

    # -------------------------------------------------------------- feedback #

    def submit_feedback(
        self,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        payload: FeedbackCreate,
        *,
        actor: User,
    ) -> MessageFeedback:
        """Rate one of the assistant's answers, or change an existing rating.

        **This writes to ``message_feedback`` and to nothing else**, which is how
        the spec's *"feedback should not modify conversation history"* is made
        structural rather than remembered: the transcript is read from a different
        table, and no statement in this method can reach it.

        Rating one's own question is refused rather than accepted, because it is
        not a judgement about the assistant and would put noise into the very
        evaluation data the spec asks to persist.

        Raises:
            ConversationNotFoundError: no such conversation belongs to this user.
            ConversationMessageNotFoundError: no such message in it.
            InvalidFeedbackTargetError: the message is the user's own.
        """
        message = self._resolve_message(conversation_id, message_id, actor=actor)

        if message.role is ConversationRole.USER:
            raise InvalidFeedbackTargetError

        existing = self._conversations.get_feedback(message.id)
        if existing is None:
            existing = MessageFeedback(message_id=message.id, rated_by=actor.id, rating=payload.rating)
        else:
            existing.rating = payload.rating
            existing.rated_by = actor.id

        existing.comment = payload.comment
        saved = self._conversations.save_feedback(existing)

        logger.info(
            "assistant_feedback_received",
            conversation_id=str(conversation_id),
            message_id=str(message.id),
            actor_id=str(actor.id),
            rating=saved.rating.value,
            # Whether a note was left, never the note: it is free text a user
            # typed about a client's matter.
            has_comment=saved.comment is not None,
        )
        return saved

    def withdraw_feedback(
        self, conversation_id: uuid.UUID, message_id: uuid.UUID, *, actor: User
    ) -> None:
        """Remove a rating.

        Idempotent: removing a rating that is not there is a success, because the
        end state the caller asked for is the state that already holds.

        Raises:
            ConversationNotFoundError: no such conversation belongs to this user.
            ConversationMessageNotFoundError: no such message in it.
        """
        message = self._resolve_message(conversation_id, message_id, actor=actor)
        existing = self._conversations.get_feedback(message.id)

        if existing is None:
            return

        self._conversations.delete_feedback(existing)
        logger.info(
            "assistant_feedback_withdrawn",
            conversation_id=str(conversation_id),
            message_id=str(message.id),
            actor_id=str(actor.id),
        )

    # --------------------------------------------------------------- health #

    def health(self) -> AssistantHealth:
        """Aggregate assistant health for the monitoring endpoint.

        No per-user scope is applied, deliberately: this is an operational view of
        the *platform*, gated on the administrative ``ai:monitor`` permission, and
        it reports counts, rates, and configuration only — never a conversation,
        a title, a question, an answer, a citation, or whose thread it was.
        """
        statuses = self._conversations.conversation_counts()
        feedback = self._conversations.feedback_counts()

        active = statuses.get(ConversationStatus.ACTIVE.value, 0)
        archived = statuses.get(ConversationStatus.ARCHIVED.value, 0)

        return AssistantHealth(
            metrics=self._metrics.snapshot(),
            total_conversations=active + archived,
            active_conversations=active,
            archived_conversations=archived,
            total_messages=self._conversations.message_count(),
            assistant_messages=self._conversations.assistant_message_count(),
            total_feedback=sum(feedback.values()),
            helpful_feedback=feedback.get("helpful", 0),
            not_helpful_feedback=feedback.get("not_helpful", 0),
            # Probed rather than read from the setting alone, so the view can tell
            # "turned off" apart from "turned on and silently producing nothing" —
            # a missing credential and a missing template look identical in a
            # transcript.
            suggestions_enabled=self._suggester.is_available(),
            streaming_enabled=settings.ASSISTANT_STREAMING_ENABLED,
            enabled=settings.ASSISTANT_ENABLED,
        )

    # ------------------------------------------------------------ the request #

    def _build_request(
        self, conversation: Conversation, payload: MessageCreate, *, actor: User
    ) -> tuple[RagRequest, int]:
        """Turn one message into a pipeline request, and say how much history it carries.

        Three decisions live here, and each is the assistant's rather than the
        pipeline's:

        * **the answer language is settled from the user's literal message**,
          never from the resolved text. A follow-up is resolved by prefixing a
          French or Arabic label to it, and detecting the language of *that*
          would let the platform's own preamble decide what language a user is
          answered in. Precedence is the request's, then the conversation's, then
          **the asker's own stored preference**, then the question's;
        * **the history budget is reserved out of the pipeline's question
          limit**, so a resolved follow-up can never be refused by the endpoint
          that built it;
        * **the conversation's case becomes the default filter** when the request
          names none. It narrows and cannot widen: the search service applies the
          caller's own case scope underneath regardless, and refuses a case they
          are not party to.
        """
        # ``21-localization.md``: respond in the user's preferred language by
        # default, and *"if the user explicitly requests another language during a
        # conversation, that request should override the default for that
        # interaction only"*. Both halves fall out of this one expression and of
        # what is **not** written anywhere near it: ``payload.language`` is a
        # parameter and nothing here persists it, so asking one question in English
        # cannot make a lawyer's account English — or even this thread English.
        language = resolve_answer_language(
            payload.content,
            payload.language
            or conversation.language
            or self._preferred_language(actor),
        )

        budget = min(
            settings.ASSISTANT_CONTEXT_MAX_CHARACTERS,
            settings.RAG_QUESTION_MAX_LENGTH - len(payload.content) - CONTEXT_OVERHEAD_CHARACTERS,
        )
        recent = self._conversations.recent_messages(
            conversation.id, limit=settings.ASSISTANT_CONTEXT_MESSAGES
        )
        resolved = resolve_question(
            payload.content,
            history=history_questions([(message.role.value, message.content) for message in recent]),
            language=language,
            max_characters=max(0, budget),
        )

        filters = payload.filters
        if filters is None:
            filters = (
                SearchFilterInput(case_id=conversation.case_id)
                if conversation.case_id is not None
                else SearchFilterInput()
            )

        return (
            RagRequest(
                question=resolved.text,
                language=language,
                top_k=payload.top_k,
                min_score=payload.min_score,
                filters=filters,
            ),
            resolved.turns,
        )

    def _preferred_language(self, actor: User) -> str | None:
        """The language this person **chose**, or ``None``.

        Deliberately the *choice* rather than the resolved default, and the
        difference is the whole reason
        :meth:`~services.localization.LanguageDirectory.chosen_language_for`
        exists. An account that has never opened the Settings page has expressed
        no opinion, and the assistant has something better than a platform default
        to fall back on: the question itself, which
        :func:`~core.rag.resolve_answer_language` reads. Returning a resolved
        default here would make that detection dead code the day this feature
        shipped — an Arabic question from an account with no stored preference
        would be answered in English.

        **Never raises and never blocks a question.** A settings lookup that failed
        resolves to ``None``, so the answer language is decided from the question
        as it was before Localization; the assistant's whole error vocabulary is
        about retrieval and the model, and localization has no business adding to
        it.
        """
        return chosen_language(self._languages, actor.id)

    # ---------------------------------------------------------- persistence #

    def _persist_exchange(
        self,
        conversation: Conversation,
        *,
        question: str,
        outcome: RagOutcome,
        turns: int,
        actor: User,
    ) -> MessageExchange:
        """Write the question and its answer together, and update the thread.

        One transaction, for the reason :meth:`send_message` records. The
        conversation's counters and title are updated in the same commit as the
        messages, so a list row can never describe a thread that does not contain
        what it says it does.
        """
        sequence = self._conversations.next_sequence(conversation.id)

        user_message = self._user_message(conversation, question, sequence=sequence)
        assistant_message = self._assistant_message(
            conversation, outcome=outcome, sequence=sequence + 1, turns=turns
        )
        assistant_message.suggestions = self._suggest(question=question, outcome=outcome)

        self._conversations.add_messages([user_message, assistant_message])
        updated = self._advance(conversation, last=assistant_message, added=2, question=question)

        logger.info(
            "assistant_message_answered",
            conversation_id=str(conversation.id),
            message_id=str(assistant_message.id),
            actor_id=str(actor.id),
            grounded=outcome.grounded,
            insufficient_evidence=outcome.insufficient_evidence,
            citation_count=len(outcome.citations),
            suggestion_count=len(assistant_message.suggestions),
            duration_ms=outcome.duration_ms,
            streamed=False,
        )

        return MessageExchange(
            conversation=updated,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    def _persist_question(
        self, conversation: Conversation, *, question: str
    ) -> ConversationMessage:
        """Write the user's message before the answer exists. Streaming only."""
        sequence = self._conversations.next_sequence(conversation.id)
        message = self._user_message(conversation, question, sequence=sequence)
        self._conversations.add_messages([message])
        self._advance(conversation, last=message, added=1, question=question)
        return message

    def _persist_answer(
        self,
        conversation: Conversation,
        *,
        question: ConversationMessage,
        outcome: RagOutcome,
        turns: int,
    ) -> ConversationMessage:
        """Write the answer to an already-stored question. Streaming only."""
        message = self._assistant_message(
            conversation, outcome=outcome, sequence=question.sequence + 1, turns=turns
        )
        message.suggestions = self._suggest(question=question.content, outcome=outcome)

        self._conversations.add_messages([message])
        self._advance(conversation, last=message, added=1, question=question.content)

        logger.info(
            "assistant_message_answered",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            grounded=outcome.grounded,
            insufficient_evidence=outcome.insufficient_evidence,
            citation_count=len(outcome.citations),
            suggestion_count=len(message.suggestions),
            duration_ms=outcome.duration_ms,
            streamed=True,
        )
        return message

    def _user_message(
        self, conversation: Conversation, question: str, *, sequence: int
    ) -> ConversationMessage:
        """Build the row for what the user typed.

        **The literal message, never the resolved one.** A user must read back
        exactly what they sent; the platform's follow-up preamble is machinery,
        and showing it in a transcript would be showing someone words they did
        not write. What was carried is recorded as a *count* on the answer
        instead.
        """
        return ConversationMessage(
            conversation_id=conversation.id,
            sequence=sequence,
            role=ConversationRole.USER,
            content=question,
            language=conversation.language,
        )

    def _assistant_message(
        self,
        conversation: Conversation,
        *,
        outcome: RagOutcome,
        sequence: int,
        turns: int,
    ) -> ConversationMessage:
        """Build the row for what the pipeline answered.

        Every field comes straight off the outcome. Nothing is recomputed, and in
        particular the citations are the pipeline's own objects serialized —
        which is what makes *"display citations without modifying them"* a
        property of the code.
        """
        return ConversationMessage(
            conversation_id=conversation.id,
            sequence=sequence,
            role=ConversationRole.ASSISTANT,
            content=outcome.answer,
            language=outcome.language,
            citations=_serialize_citations(outcome.citations),
            suggestions=[],
            grounded=outcome.grounded,
            insufficient_evidence=outcome.insufficient_evidence,
            truncated=outcome.truncated,
            provider=outcome.provider,
            model=outcome.model,
            prompt_name=outcome.prompt_name,
            prompt_version=outcome.prompt_version,
            duration_ms=outcome.duration_ms,
            retrieval_ms=outcome.retrieval_ms,
            generation_ms=outcome.generation_ms,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            total_tokens=outcome.total_tokens,
            retrieved_count=outcome.retrieved_count,
            context_count=outcome.context_count,
            context_turns=turns,
            top_score=max((citation.score for citation in outcome.citations), default=None),
        )

    def _advance(
        self,
        conversation: Conversation,
        *,
        last: ConversationMessage,
        added: int,
        question: str,
    ) -> Conversation:
        """Move the conversation forward: counters, preview, and its first title.

        The title is derived here, from the **first user question only**, and
        never overwrites one the user chose. Doing it on the way past is what
        makes titling free — no extra read, no extra write, and no model call.
        """
        conversation.message_count += added
        conversation.last_message_at = last.created_at or datetime.now(UTC)
        conversation.last_message_preview = message_preview(last.content)

        if not conversation.title_is_custom and conversation.message_count <= added:
            conversation.title = derive_title(
                question,
                language=resolve_language(conversation.language, last.language),
            )

        return self._conversations.save(conversation)

    # ------------------------------------------------------------ suggestions #

    def _suggest(self, *, question: str, outcome: RagOutcome) -> list[str]:
        """Ask for follow-up questions, and never let the asking matter.

        Wrapped rather than called directly because the suggester is the one
        collaborator here that may be a *network call to a metered service* — and
        an answer the user is already waiting for must not be lost to a failure in
        the convenience that follows it. :mod:`services.suggestions` already
        returns an empty list for every failure it can name; this catches the ones
        it cannot.
        """
        try:
            return self._suggester.suggest(
                question=question,
                answer=outcome.answer,
                citations=outcome.citations,
                language=outcome.language,
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning("assistant_suggestions_failed", exc_info=True)
            return []

    # -------------------------------------------------------------- helpers #

    def _require_enabled(self, *, actor: User) -> None:
        """Refuse new work when the assistant is switched off here.

        Existing conversations stay readable, deliberately: turning the feature
        off must not make a lawyer's record of what they were told disappear.

        Raises:
            AssistantDisabledError: always, when disabled.
        """
        if settings.ASSISTANT_ENABLED:
            return

        logger.info("assistant_rejected", reason="disabled", actor_id=str(actor.id))
        raise AssistantDisabledError

    def _open_conversation(self, conversation_id: uuid.UUID, *, actor: User) -> Conversation:
        """Resolve a conversation that may still be written to.

        Raises:
            ConversationNotFoundError: no such conversation belongs to this user.
            ConversationArchivedError: it is archived.
            ConversationFullError: it has reached the platform's ceiling.
        """
        conversation = self.get_conversation(conversation_id, actor=actor)

        if conversation.status is ConversationStatus.ARCHIVED:
            raise ConversationArchivedError

        # Two, because a message costs a question *and* an answer: stopping at
        # the ceiling minus one would accept a question the answer cannot join.
        if conversation.message_count + 2 > settings.ASSISTANT_MAX_MESSAGES:
            raise ConversationFullError(settings.ASSISTANT_MAX_MESSAGES)

        return conversation

    def _resolve_message(
        self, conversation_id: uuid.UUID, message_id: uuid.UUID, *, actor: User
    ) -> ConversationMessage:
        """Resolve a message through a conversation this user owns.

        Raises:
            ConversationNotFoundError: no such conversation belongs to this user.
            ConversationMessageNotFoundError: no such message in it.
        """
        conversation = self.get_conversation(conversation_id, actor=actor)
        message = self._conversations.get_message(message_id, conversation_id=conversation.id)
        if message is None:
            raise ConversationMessageNotFoundError
        return message

    def _record_success(
        self,
        outcome: RagOutcome,
        *,
        conversation: Conversation,
        actor: User,
        started: float,
        streamed: bool,
    ) -> None:
        """Count one answered message.

        The duration measured here is the **assistant's**, not the pipeline's: it
        includes resolving the conversation, reading its history, and persisting
        both turns, which is what the user actually waited for. The pipeline's own
        latency is reported separately on ``/rag/metrics``, and the gap between
        the two is this feature's overhead.
        """
        self._metrics.record_success(
            duration_ms=self._elapsed_ms(started),
            grounded=outcome.grounded,
            streamed=streamed,
        )

    def _record_failure(
        self,
        error: Exception,
        *,
        conversation: Conversation,
        actor: User,
        started: float,
    ) -> None:
        """Count one message that could not be answered, and say why.

        **A rejected request is not a failure of the assistant** and is not
        counted as one — an unanswerable question, an inaccessible filter, an
        archived conversation. The failure rate is a health signal, and a badly
        formed request says nothing about the platform's health. The same
        distinction :class:`~services.rag.RagService` draws, applied one layer up.
        """
        if isinstance(error, RagUnavailableError):
            code = _failure_code(error.error_code)
        elif isinstance(error, AppException):
            return
        else:
            code = RagFailureCode.UNKNOWN

        self._metrics.record_failure(duration_ms=self._elapsed_ms(started), code=code)

        logger.warning(
            "assistant_message_failed",
            conversation_id=str(conversation.id),
            actor_id=str(actor.id),
            error_code=code.value,
            duration_ms=self._elapsed_ms(started),
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """Milliseconds since ``started``.

        From :func:`time.monotonic` rather than from wall-clock timestamps: a
        clock adjustment mid-request would produce a negative latency, and a
        negative sample in a monitoring average is worse than a slightly
        imprecise one.
        """
        return max(0, int((time.monotonic() - started) * 1000))


def _failure_code(value: str) -> RagFailureCode:
    """Resolve a pipeline failure code, falling back to the catch-all."""
    try:
        return RagFailureCode(value)
    except ValueError:  # pragma: no cover - defensive
        return RagFailureCode.UNKNOWN


def _serialize_citations(citations: Sequence[RagCitationRead]) -> list[dict[str, Any]]:
    """Store the pipeline's citations exactly as it produced them.

    ``mode="json"`` because the column is JSON: a :class:`uuid.UUID` is not a
    JSON value, and letting the driver decide how to render one would make the
    stored shape depend on the database rather than on the schema.
    """
    return [citation.model_dump(mode="json") for citation in citations]


__all__ = ["AssistantHealth", "AssistantService", "MessageExchange"]
