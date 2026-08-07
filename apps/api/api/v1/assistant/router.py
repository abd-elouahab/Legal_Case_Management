"""AI Legal Assistant endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.assistant.AssistantService`, and shape the HTTP response. No
business logic lives here, and **no route calls the RAG pipeline directly** — the
assistant owns conversations, the pipeline owns answers, and one path from the
first to the second keeps them that way.

**Asking is a POST, and that is a privacy decision rather than a stylistic one.**
A query string is written to the reverse proxy's access log, to the browser's
history, and to the ``Referer`` header of anything the page loads next. A question
put to a legal assistant names what a lawyer is looking for and in whose matter,
and a ``GET`` would put every one of them into three logs the application does not
control. The same reasoning ``11-semantic-search.md`` gives for search.

Authorization is layered, and the three layers answer different questions:

* the ``ai:*`` **capability** is required by the reusable dependencies in
  :mod:`api.authorization`, declared next to the route so it appears in the
  OpenAPI schema. Because ``CurrentUser`` resolves first, an anonymous caller gets
  **401** and only an authenticated-but-unentitled one gets **403**;
* the **conversation** — whose thread this is — is applied by the repository's
  queries, which are all keyed by owner, so a conversation belonging to somebody
  else is simply not found;
* the **documents** an answer may be built from are scoped by
  :class:`~services.search.SearchService` inside the vector query, because the
  pipeline retrieves through nothing else.

**Sending a message requires two permissions, and that is not belt-and-braces.**
``ai:chat`` is the conversational surface and ``ai:ask`` is putting a question to
the pipeline; a message does both, so a deployment that grants one and withholds
the other must not be able to reach the pipeline through this door. Reading a
transcript needs only ``ai:chat``, because reading what was already answered asks
nothing new of the pipeline.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse

from api.authorization import require_all_permissions, require_permission
from api.deps import AssistantServiceDep
from core.exceptions import AppException
from core.permissions import Permission
from models.conversation import Conversation, ConversationMessage
from models.user import User
from schemas.conversation import (
    AssistantMetricsRead,
    ConversationCreate,
    ConversationDetailRead,
    ConversationListQuery,
    ConversationMessagePage,
    ConversationPage,
    ConversationRead,
    ConversationUpdate,
    FeedbackCreate,
    FeedbackRead,
    MessageCreate,
    MessageListQuery,
    MessageRead,
    MessageSendResponse,
)
from services.rag import RagStreamEvent, RagStreamEventKind

logger = structlog.get_logger(__name__)

#: Mounted under ``/assistant``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

Chatter = Annotated[User, Depends(require_permission(Permission.AI_CHAT))]
Asker = Annotated[
    User, Depends(require_all_permissions(Permission.AI_CHAT, Permission.AI_ASK))
]
AiMonitor = Annotated[User, Depends(require_permission(Permission.AI_MONITOR))]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": (
            "The account is disabled, lacks the required permission, or filtered by a case or "
            "document it is not assigned to."
        ),
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such conversation belongs to this caller — it does not exist, it was deleted, or "
            "it is somebody else's. The three are deliberately indistinguishable."
        ),
    }
}
_CONFLICT: dict[int | str, dict[str, object]] = {
    status.HTTP_409_CONFLICT: {
        "description": "The conversation is archived, or has reached its message limit.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "Request validation failed, or a metadata filter matches too many documents to "
            "retrieve from at once."
        ),
    }
}
_UNAVAILABLE: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": (
            "The assistant is disabled on this deployment, or retrieval, the prompt library, or "
            "the language model could not answer. The `error` field names which."
        ),
    }
}


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


def _to_message(message: ConversationMessage) -> MessageRead:
    """Project one stored message onto the wire shape.

    The citations round-trip through :class:`~schemas.rag.RagCitationRead` — the
    pipeline's own type, reused verbatim — so what a client receives is
    structurally identical to what ``POST /rag/answer`` returns. That is the
    spec's *"display citations without modifying them"*, made a property of the
    types rather than a promise.
    """
    return MessageRead.model_validate(message)


def _to_conversation(conversation: Conversation) -> ConversationRead:
    """Project one conversation onto its list-row shape."""
    return ConversationRead.model_validate(conversation)


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #


@router.post(
    "/conversations",
    response_model=ConversationDetailRead,
    status_code=status.HTTP_201_CREATED,
    summary="Open a new conversation",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION, **_UNAVAILABLE},
)
def create_conversation(
    actor: Chatter, assistant: AssistantServiceDep, payload: ConversationCreate
) -> ConversationDetailRead:
    """Start a conversation with the AI legal assistant.

    Every field is optional: the common path is pressing "New conversation" with
    nothing to say about it yet, and the title arrives from the first question.

    `case_id` pins the conversation to a case. It is a **default retrieval filter
    and a label, never a grant** — every answer is still built only from documents
    the caller may already read, and a case they are not party to is refused when
    the first question is asked.

    Supplying `first_message` asks the opening question in the same request, which
    saves a round trip and is otherwise identical to sending it afterwards. That
    is the one path where this endpoint answers slowly, because it waits for the
    pipeline.
    """
    conversation = assistant.create_conversation(payload, actor=actor)

    if payload.first_message is None:
        return ConversationDetailRead(
            **_to_conversation(conversation).model_dump(), messages=[], has_more_messages=False
        )

    exchange = assistant.send_message(
        conversation.id, MessageCreate(content=payload.first_message), actor=actor
    )
    return ConversationDetailRead(
        **_to_conversation(exchange.conversation).model_dump(),
        messages=[_to_message(exchange.user_message), _to_message(exchange.assistant_message)],
        has_more_messages=False,
    )


@router.get(
    "/conversations",
    response_model=ConversationPage,
    status_code=status.HTTP_200_OK,
    summary="List your conversations",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def list_conversations(
    actor: Chatter,
    assistant: AssistantServiceDep,
    query: Annotated[ConversationListQuery, Query()],
) -> ConversationPage:
    """List the caller's own conversations, most recently active first.

    **Only the caller's.** The scope is applied in the database query, so the page
    totals count only what they own — a total that included other people's threads
    would disclose how many of them exist.

    Omitting `status` returns active conversations only: an archived thread is out
    of the working set by definition, and returning it by default would make
    archiving do nothing visible.
    """
    conversations, total = assistant.list_conversations(query, actor=actor)

    return ConversationPage.build(
        [_to_conversation(conversation) for conversation in conversations],
        total=total,
        page=query.page,
        page_size=query.page_size,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailRead,
    status_code=status.HTTP_200_OK,
    summary="Open one conversation",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_conversation(
    conversation_id: uuid.UUID, actor: Chatter, assistant: AssistantServiceDep
) -> ConversationDetailRead:
    """Return one conversation together with the first page of its transcript.

    One request rather than two, because opening a conversation always needs both
    and a client that had to fetch them separately would render an empty thread
    for one round trip. `has_more_messages` says whether to page for the rest.

    A conversation that does not exist, one that was deleted, and one belonging to
    another user all answer **404** — telling them apart is precisely the
    disclosure this feature must not make.
    """
    conversation = assistant.get_conversation(conversation_id, actor=actor)
    query = MessageListQuery()
    messages, total = assistant.list_messages(
        conversation.id, offset=0, limit=query.page_size, actor=actor
    )

    return ConversationDetailRead(
        **_to_conversation(conversation).model_dump(),
        messages=[_to_message(message) for message in messages],
        has_more_messages=total > len(messages),
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationRead,
    status_code=status.HTTP_200_OK,
    summary="Rename, archive, or restore a conversation",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_VALIDATION},
)
def update_conversation(
    conversation_id: uuid.UUID,
    actor: Chatter,
    assistant: AssistantServiceDep,
    payload: ConversationUpdate,
) -> ConversationRead:
    """Change a conversation's name or its status.

    Omitted fields are left alone, so a client that only archives does not have to
    resend the title it is not changing.

    Renaming marks the title as the user's, permanently: automatic titling never
    overwrites a name somebody chose.

    Archiving takes the thread out of the working list and closes it to new
    messages. **Nothing is deleted**, and restoring it (`status: active`) makes it
    writable again.
    """
    return _to_conversation(assistant.update_conversation(conversation_id, payload, actor=actor))


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a conversation",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def delete_conversation(
    conversation_id: uuid.UUID, actor: Chatter, assistant: AssistantServiceDep
) -> Response:
    """Withdraw a conversation.

    It stops being readable through every endpoint immediately. The row is kept
    rather than destroyed, because the transcript carries the citations of advice
    a lawyer may have acted on — a future retention job is what reclaims it.

    Answers **204** with no body, and a second delete answers **404**: by then
    there is no conversation of that identifier belonging to anyone.
    """
    assistant.delete_conversation(conversation_id, actor=actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ConversationMessagePage,
    status_code=status.HTTP_200_OK,
    summary="Read a conversation's transcript",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def list_messages(
    conversation_id: uuid.UUID,
    actor: Chatter,
    assistant: AssistantServiceDep,
    query: Annotated[MessageListQuery, Query()],
) -> ConversationMessagePage:
    """Return one page of a conversation, oldest first.

    Oldest first because a transcript is *read* in the order it happened — the
    opposite of the case timeline, which is a feed of what changed most recently.

    Requires only `ai:chat`: reading what was already answered asks nothing new of
    the pipeline.
    """
    messages, total = assistant.list_messages(
        conversation_id, offset=query.offset, limit=query.page_size, actor=actor
    )

    return ConversationMessagePage.build(
        [_to_message(message) for message in messages],
        total=total,
        page=query.page,
        page_size=query.page_size,
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageSendResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ask a question and receive a grounded answer",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_VALIDATION,
        **_UNAVAILABLE,
    },
)
def send_message(
    conversation_id: uuid.UUID,
    actor: Asker,
    assistant: AssistantServiceDep,
    payload: MessageCreate,
) -> MessageSendResponse:
    """Send one message and receive the assistant's answer, whole.

    The answer is produced by the **RAG pipeline**: the question is retrieved
    against with the same model the documents were indexed with, the most relevant
    passages are assembled into a versioned prompt, the configured language model
    is invoked once, and the answer comes back with a citation per source —
    document, version, page, and case.

    **The answer is grounded or it is not given.** When the documents do not
    support one, `grounded` is `false`, `insufficient_evidence` is `true`, and the
    text is the platform's own message rather than anything a model improvised.
    That is a **successful** message: it is stored, shown, and can be rated.

    **A short question is read against the one before it.** "And when is it due?"
    is resolved against what was just asked, so a follow-up retrieves the right
    material; `context_turns` on the answer says how many earlier questions were
    carried, and `0` means it stood on its own.

    **Authorization is inherited, not re-implemented.** Retrieval runs through the
    semantic search service, so a passage can only reach the model if the caller
    could already open the document it came from.

    Both messages come back, because the question is stored with a server-assigned
    identifier and sequence that the client would otherwise have to invent and
    reconcile.
    """
    exchange = assistant.send_message(conversation_id, payload, actor=actor)

    return MessageSendResponse(
        conversation=_to_conversation(exchange.conversation),
        user_message=_to_message(exchange.user_message),
        assistant_message=_to_message(exchange.assistant_message),
    )


@router.post(
    "/conversations/{conversation_id}/messages/stream",
    response_class=StreamingResponse,
    status_code=status.HTTP_200_OK,
    summary="Ask a question and stream the answer",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_VALIDATION,
        **_UNAVAILABLE,
        status.HTTP_200_OK: {
            "content": {"text/event-stream": {}},
            "description": (
                "Server-sent events: one `retrieval`, any number of `delta`, and one `final` "
                "carrying the complete answer and its citations. An `error` event replaces the "
                "`final` one when generation fails after the response has begun."
            ),
        },
    },
)
def stream_message(
    conversation_id: uuid.UUID,
    actor: Asker,
    assistant: AssistantServiceDep,
    payload: MessageCreate,
) -> StreamingResponse:
    """Send one message and receive the answer progressively, as Server-Sent Events.

    Identical to the endpoint above in everything except delivery. Three event
    types arrive, in this order:

    * **`retrieval`** — once, carrying `retrieved_count`. It is what lets a client
      show that the platform is reading documents before the first word appears,
      which is the part of a slow request that is otherwise silent.
    * **`delta`** — a fragment of the answer. Zero of these arrive when retrieval
      found nothing, because no model is called at all; exactly one arrives when
      the provider cannot stream and the answer falls back to being produced
      whole.
    * **`final`** — once, carrying the complete answer, its citations, its
      suggested follow-ups, and both stored messages.

    **Render the `final` event, not the accumulated deltas.** The final answer is
    authoritative: a citation marker pointing at a source that was never supplied
    is removed from it, and a model that declined has its internal token replaced
    by the platform's own sentence. The deltas are a progress indicator that
    happens to be readable.

    **Errors before the first event keep their HTTP status** — 403 for an
    inaccessible filter, 404 for an unknown conversation, 409 for an archived one,
    422 for an unanswerable question, 503 for a dependency outage. The stream is
    started only once retrieval has succeeded, so those never have to be smuggled
    into an event. A failure *after* that arrives as an `error` event, because the
    status line has already been sent.
    """
    events = assistant.stream_message(conversation_id, payload, actor=actor)

    # Primed here, deliberately: a generator does nothing until it is iterated, so
    # constructing the response first would send `200 text/event-stream` and only
    # then discover that the conversation is archived. Pulling the first event —
    # which is emitted once retrieval has run — moves every request-rejection
    # error back in front of the status line, where it belongs.
    first = next(events)

    return StreamingResponse(
        _sse(first, events),
        media_type="text/event-stream",
        headers={
            # A streamed answer is one user's legal question. Nothing between here
            # and them may keep a copy, and `X-Accel-Buffering` is what stops an
            # nginx in front of this from holding the whole stream and delivering
            # it in one piece — which would make the feature silently pointless.
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(first: RagStreamEvent, rest: Iterator[RagStreamEvent]) -> Iterator[str]:
    """Render pipeline events as a Server-Sent Events stream.

    A failure mid-stream becomes an ``error`` event rather than an exception that
    tears the connection down: the status line has already been sent, so an
    aborted body is indistinguishable from a network problem to the client, and a
    reader who was watching an answer appear deserves to be told what happened.
    The message is the exception's own — every one of them is written for a user
    (see :mod:`core.rag`) and none quotes a question, a passage, or an SDK.
    """
    yield _sse_event(first)

    try:
        for event in rest:
            yield _sse_event(event)
    except AppException as exc:
        logger.warning("assistant_stream_failed", error_code=exc.error_code)
        yield _sse_frame("error", {"error": exc.error_code, "message": exc.message})
    except Exception:
        logger.exception("assistant_stream_failed")
        yield _sse_frame(
            "error", {"error": "internal_error", "message": "An unexpected error occurred."}
        )


def _sse_event(event: RagStreamEvent) -> str:
    """Render one pipeline event as an SSE frame."""
    if event.kind is RagStreamEventKind.RETRIEVAL:
        return _sse_frame("retrieval", {"retrieved_count": event.retrieved_count})

    if event.kind is RagStreamEventKind.DELTA:
        return _sse_frame("delta", {"text": event.text})

    outcome = event.outcome
    if outcome is None:  # pragma: no cover - defensive
        return _sse_frame("error", {"error": "internal_error", "message": "No answer was produced."})

    return _sse_frame(
        "final",
        {
            "answer": outcome.answer,
            "language": outcome.language,
            "grounded": outcome.grounded,
            "insufficient_evidence": outcome.insufficient_evidence,
            "truncated": outcome.truncated,
            "citations": [citation.model_dump(mode="json") for citation in outcome.citations],
            "retrieved_count": outcome.retrieved_count,
            "context_count": outcome.context_count,
            "duration_ms": outcome.duration_ms,
            "retrieval_ms": outcome.retrieval_ms,
            "generation_ms": outcome.generation_ms,
        },
    )


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    """One SSE frame: a named event and a single JSON line.

    ``ensure_ascii=False`` because an Arabic or French answer escaped into
    ``\\uXXXX`` triples its size and is unreadable in a network inspector, and the
    stream is already declared UTF-8. One ``data:`` line, so a newline inside the
    answer cannot be mistaken for the end of the frame — JSON encodes it as
    ``\\n``, which is exactly why the payload is JSON rather than raw text.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #


@router.put(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=FeedbackRead,
    status_code=status.HTTP_200_OK,
    summary="Rate an answer",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_VALIDATION},
)
def submit_feedback(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    actor: Chatter,
    assistant: AssistantServiceDep,
    payload: FeedbackCreate,
) -> FeedbackRead:
    """Rate one of the assistant's answers `helpful` or `not_helpful`.

    A `PUT` because a rating is a **property of the message** rather than a new
    thing each time: sending it twice leaves one rating, the second one. Changing
    your mind is the same call with a different value.

    **Rating does not change the conversation.** Feedback is stored separately
    from the transcript, so an answer reads exactly the same before and after it
    is rated — which is what the spec requires and what makes the stored history
    usable as evaluation evidence.

    Only the assistant's messages can be rated; rating your own question answers
    **422**.
    """
    return FeedbackRead.model_validate(
        assistant.submit_feedback(conversation_id, message_id, payload, actor=actor)
    )


@router.delete(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw a rating",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def withdraw_feedback(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    actor: Chatter,
    assistant: AssistantServiceDep,
) -> Response:
    """Remove a rating you left on an answer.

    Idempotent: removing a rating that is not there answers **204**, because the
    end state asked for is the one that already holds. The rating is genuinely
    deleted rather than kept — it is an opinion its author is entitled to take
    back, and a withdrawn rating that lingered would keep skewing the statistics
    it was removed from.
    """
    assistant.withdraw_feedback(conversation_id, message_id, actor=actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


@router.get(
    "/metrics",
    response_model=AssistantMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="AI assistant metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_assistant_metrics(actor: AiMonitor, assistant: AssistantServiceDep) -> AssistantMetricsRead:
    """Return platform-wide assistant health.

    The six figures the spec's Monitoring section names — **active conversations,
    average response time, average conversation length, successful requests,
    failed requests, and user feedback statistics** — plus the rates and a
    breakdown of failures by cause.

    Two halves with different provenance, and the difference is stated rather than
    hidden: the **conversation and feedback figures are read from the database**
    and cover everything; the **request counters live in the API process**, so
    they reset when it restarts and each instance counts only its own traffic.
    `since` is the honest caveat for the second half.

    An operational view, so it is gated on `ai:monitor` and reports **counts,
    rates, and configuration only** — never a conversation, a title, a question,
    an answer, a citation, or whose thread it was.
    """
    health = assistant.health()
    metrics = health.metrics

    return AssistantMetricsRead(
        since=metrics.since,
        total_conversations=health.total_conversations,
        active_conversations=health.active_conversations,
        archived_conversations=health.archived_conversations,
        total_messages=health.total_messages,
        average_conversation_length=(
            round(health.total_messages / health.total_conversations, 2)
            if health.total_conversations > 0
            else None
        ),
        total_requests=metrics.total_requests,
        successful_requests=metrics.successful_requests,
        failed_requests=metrics.failed_requests,
        success_rate=metrics.success_rate,
        failure_rate=metrics.failure_rate,
        streamed_requests=metrics.streamed_requests,
        average_response_ms=metrics.average_response_ms,
        grounded_answers=metrics.grounded_answers,
        insufficient_evidence=metrics.insufficient_evidence,
        grounding_rate=metrics.grounding_rate,
        total_feedback=health.total_feedback,
        helpful_feedback=health.helpful_feedback,
        not_helpful_feedback=health.not_helpful_feedback,
        # `None` rather than `0` when nobody has rated anything: zero would read
        # as "every answer was unhelpful", which is a different and much more
        # alarming statement than "nobody has said".
        helpful_rate=(
            round(health.helpful_feedback / health.total_feedback * 100, 2)
            if health.total_feedback > 0
            else None
        ),
        # The denominator for the figure above, and the reason it is reported: a
        # 90% helpful rate over four ratings is not a measurement.
        rated_messages_rate=(
            round(health.total_feedback / health.assistant_messages * 100, 2)
            if health.assistant_messages > 0
            else None
        ),
        failures_by_code=metrics.failures_by_code,
        suggestions_enabled=health.suggestions_enabled,
        streaming_enabled=health.streaming_enabled,
        enabled=health.enabled,
    )


__all__ = ["router"]
