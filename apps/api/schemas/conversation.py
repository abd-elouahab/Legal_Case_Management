"""AI Legal Assistant request and response schemas.

Two responsibilities, the same two the case, document, OCR, indexing, search, and
RAG schemas carry:

* **Validation** — a message's length, a title's, a page's bounds, the answer
  language, the retrieval filters, and a feedback rating are all enforced here,
  so routes stay thin and every rejection comes back in the standard envelope
  with a per-field message.
* **Serialization** — a conversation is returned with the counters a list row
  needs, and a message with its citations, its provenance, and its timings.

**The citation schema is :class:`~schemas.rag.RagCitationRead`, reused
verbatim.** ``13-ai-legal-assistant.md``: *"The AI Assistant should display
citations without modifying them."* A parallel citation model here would be a
second vocabulary to keep in step with the pipeline's, and the first change that
touched only one of them would produce a citation the assistant renders and the
pipeline never emits. Reusing the type makes "unmodified" a fact about the code
rather than a promise in a document.

**The retrieval filters are :class:`~schemas.search.SearchFilterInput`, reused
for the same reason** — they are what the pipeline passes to the search service,
and a copy here would be a copy of the *authorization surface*.

**A message is sent as a POST body, never a query string**, exactly as a search
and a question are: a URL is written to the reverse proxy's access log, the
browser's history, and the ``Referer`` header of anything the page loads next,
and a question about a client's matter is at least as revealing as the passage it
retrieves.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from core.assistant import ConversationRole, normalize_title
from core.config import settings
from core.rag import MIN_QUESTION_LENGTH, SUPPORTED_ANSWER_LANGUAGES, normalize_question
from models.conversation import ConversationStatus, FeedbackRating
from schemas.rag import RagCitationRead
from schemas.search import SearchFilterInput

#: Default and maximum page sizes, taken from configuration rather than restated,
#: so a deployment that changes one does not have to remember this file.
DEFAULT_PAGE_SIZE = settings.ASSISTANT_PAGE_SIZE
MAX_PAGE_SIZE = settings.ASSISTANT_MAX_PAGE_SIZE
DEFAULT_MESSAGE_PAGE_SIZE = settings.ASSISTANT_MESSAGE_PAGE_SIZE
MAX_MESSAGE_PAGE_SIZE = settings.ASSISTANT_MAX_MESSAGE_PAGE_SIZE

#: Longest note a user may attach to a rating. Generous enough for a sentence
#: explaining *why* an answer was unhelpful — which is the part that is actually
#: useful for evaluation — and short enough that the field cannot become a place
#: to paste a document.
MAX_FEEDBACK_COMMENT_LENGTH = 1000

__all__ = [
    "DEFAULT_MESSAGE_PAGE_SIZE",
    "DEFAULT_PAGE_SIZE",
    "MAX_FEEDBACK_COMMENT_LENGTH",
    "MAX_MESSAGE_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "AssistantMetricsRead",
    "ConversationCreate",
    "ConversationDetailRead",
    "ConversationListQuery",
    "ConversationMessagePage",
    "ConversationPage",
    "ConversationRead",
    "ConversationUpdate",
    "FeedbackCreate",
    "FeedbackRead",
    "MessageCreate",
    "MessageListQuery",
    "MessageRead",
    "MessageSendResponse",
]


def _validate_language(value: str | None) -> str | None:
    """Accept only a language the platform actually answers in.

    Rejected rather than silently ignored, exactly as
    :class:`~schemas.rag.RagRequest` rejects it: a caller who asked for German
    and received French would have no way to tell that the request was understood
    and overruled rather than honoured.
    """
    if value is None:
        return None

    wanted = value.strip().lower()
    if not wanted:
        return None
    if wanted not in SUPPORTED_ANSWER_LANGUAGES:
        supported = ", ".join(sorted(SUPPORTED_ANSWER_LANGUAGES))
        raise ValueError(f"Answers are available in: {supported}.")
    return wanted


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class ConversationCreate(BaseModel):
    """Open a new conversation.

    Every field is optional, deliberately: the common path is a user pressing
    "New conversation" with nothing to say about it yet, and requiring a title
    up front would ask them to name a thread before it has a subject. The title
    arrives from the first question instead (see
    :func:`~core.assistant.derive_title`).
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(
        default=None,
        max_length=settings.ASSISTANT_TITLE_MAX_LENGTH,
        description=(
            "Name for the conversation. Omitted takes the first question's opening words, which "
            "the user can rename at any time."
        ),
    )
    language: str | None = Field(
        default=None,
        description=(
            "ISO 639-1 code answers in this conversation must be written in (`ar`, `fr`, or `en`). "
            "Omitted detects the language of each question, which is right for someone typing in "
            "their own language and wrong for a French interface asking about an Arabic filing."
        ),
    )
    case_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Case this conversation is about. A default retrieval filter and a label — never a "
            "grant: every answer is still built only from documents the caller may already read, "
            "and a case they are not party to is refused."
        ),
    )
    first_message: str | None = Field(
        default=None,
        max_length=settings.RAG_QUESTION_MAX_LENGTH,
        description=(
            "Opening question, when the conversation is started by asking something. Saves a "
            "round trip; sending it separately afterwards is equivalent."
        ),
    )

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_title(value)
        return normalized or None

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str | None) -> str | None:
        return _validate_language(value)

    @field_validator("first_message")
    @classmethod
    def _normalize_first_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_question(value)
        return normalized or None


class ConversationUpdate(BaseModel):
    """Rename a conversation, or archive and restore it.

    One schema for both because they are the same operation to a client — a
    PATCH of the fields it may change — and because splitting them would give
    "archive" a request body of its own with exactly one field in it.

    Every field is optional and **omission means "leave it alone"**, which is why
    ``exclude_unset`` is what the service reads: sending ``title: null`` and not
    sending ``title`` at all must not be the same request, or a client updating
    only the status would silently clear the name.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(
        default=None,
        max_length=settings.ASSISTANT_TITLE_MAX_LENGTH,
        description="New name for the conversation.",
    )
    status: ConversationStatus | None = Field(
        default=None,
        description=(
            "`archived` takes the conversation out of the working list and closes it to new "
            "messages; `active` restores it. Nothing is deleted either way."
        ),
    )

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_title(value)
        if not normalized:
            raise ValueError("Enter a name for this conversation.")
        return normalized


class MessageCreate(BaseModel):
    """Send one message and receive the assistant's answer.

    The retrieval controls (``filters``, ``top_k``, ``min_score``) are the
    pipeline's own, passed through rather than reinterpreted: the assistant does
    not decide what may be retrieved, it forwards a request to the service that
    does.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        min_length=1,
        max_length=settings.RAG_QUESTION_MAX_LENGTH,
        description=(
            "The question, in natural language. Arabic, French, and English share one retrieval "
            "space, so a question in one language finds passages written in the others."
        ),
    )
    language: str | None = Field(
        default=None,
        description=(
            "ISO 639-1 code this answer must be written in. Omitted falls back to the "
            "conversation's language, and then to the language of the question."
        ),
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=settings.SEARCH_MAX_LIMIT,
        description=(
            f"Passages to ground the answer in (max {settings.SEARCH_MAX_LIMIT}). Omitted uses the "
            f"deployment's default."
        ),
    )
    min_score: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Similarity floor for retrieved passages. Omitted uses the deployment default.",
    )
    filters: SearchFilterInput | None = Field(
        default=None,
        description=(
            "Which documents this answer may be built from. The same filters semantic search "
            "accepts, with the same guarantee: they narrow the caller's authorized scope and can "
            "never widen it. Omitted falls back to the conversation's case, when it has one."
        ),
    )

    @field_validator("content")
    @classmethod
    def _normalize(cls, value: str) -> str:
        """Normalise the message and reject one that carries nothing to answer.

        The *same* normaliser the pipeline and the search service apply, so the
        text stored in the transcript is byte-identical to the text that will be
        embedded — an Arabic or French question typed in the decomposed form must
        not be stored one way and retrieved another.
        """
        normalized = normalize_question(value)
        if len(normalized) < MIN_QUESTION_LENGTH or not any(
            character.isalnum() for character in normalized
        ):
            raise ValueError(f"Enter a question of at least {MIN_QUESTION_LENGTH} characters.")
        return normalized

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str | None) -> str | None:
        return _validate_language(value)


class FeedbackCreate(BaseModel):
    """Rate one of the assistant's answers."""

    model_config = ConfigDict(extra="forbid")

    rating: FeedbackRating = Field(
        description="`helpful` or `not_helpful` — the two the spec requires at minimum."
    )
    comment: str | None = Field(
        default=None,
        max_length=MAX_FEEDBACK_COMMENT_LENGTH,
        description=(
            "Optional note. Why an answer was unhelpful is the part that is actually useful for "
            "evaluation, which is why the field exists at all."
        ),
    )

    @field_validator("comment")
    @classmethod
    def _normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


class ConversationListQuery(BaseModel):
    """Validated query parameters for ``GET /assistant/conversations``.

    Deliberately small. A conversation list is one user's own threads, ordered by
    recency, and the only questions anyone asks of it are "which ones are still
    open" and "where was the one about X" — so it has a status filter and a
    search, and no sort selector: a thread list ordered by anything other than
    recent activity is a list nobody can find anything in.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Conversations per page (max {MAX_PAGE_SIZE}).",
    )
    status: ConversationStatus | None = Field(
        default=None,
        description=(
            "Only conversations in this state. Omitted returns active ones only — an archived "
            "thread is out of the working set by definition, and returning it by default would "
            "make archiving do nothing visible."
        ),
    )
    case_id: uuid.UUID | None = Field(
        default=None, description="Only conversations pinned to this case."
    )
    search: str | None = Field(
        default=None,
        max_length=settings.ASSISTANT_TITLE_MAX_LENGTH,
        description="Match against the conversation title, case-insensitively.",
    )

    @field_validator("search")
    @classmethod
    def _normalize_search(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size


class MessageListQuery(BaseModel):
    """Validated query parameters for ``GET /assistant/conversations/{id}/messages``."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_MESSAGE_PAGE_SIZE,
        ge=1,
        le=MAX_MESSAGE_PAGE_SIZE,
        description=f"Messages per page (max {MAX_MESSAGE_PAGE_SIZE}).",
    )

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class FeedbackRead(BaseModel):
    """One user's rating of one answer."""

    model_config = ConfigDict(from_attributes=True)

    rating: FeedbackRating = Field(description="How the answer was rated.")
    comment: str | None = Field(default=None, description="The note left with the rating, if any.")
    created_at: datetime = Field(description="When the rating was first left.")
    updated_at: datetime = Field(description="When it was last changed.")


class MessageRead(BaseModel):
    """One turn of a conversation, as returned to its owner.

    A user message carries content and nothing else of substance; an assistant
    message carries the answer, the citations the pipeline produced, the
    suggested next questions, and the provenance an evaluation needs. One schema
    for both because they are one list to a client, and because a second schema
    would make rendering a transcript a type test per row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique message identifier.")
    conversation_id: uuid.UUID = Field(description="Conversation this message belongs to.")
    sequence: int = Field(description="Position in the thread, 1-based and contiguous.")
    role: ConversationRole = Field(description="`user` or `assistant`.")
    content: str = Field(description="What was written.")
    language: str | None = Field(default=None, description="ISO 639-1 code it is written in.")

    citations: list[RagCitationRead] = Field(
        default_factory=list,
        description=(
            "Sources the answer was grounded in, exactly as the RAG pipeline produced them — "
            "document, version, page, case, and the excerpt the model read. Empty on a user "
            "message, and empty whenever the answer is not grounded."
        ),
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description=(
            "Suggested follow-up questions offered after this answer. Stored rather than "
            "regenerated, so a reloaded transcript shows the same ones."
        ),
    )

    grounded: bool | None = Field(
        default=None,
        description=(
            "Whether the answer was produced from retrieved passages. `false` means the assistant "
            "declined — never that it answered from the model's own knowledge."
        ),
    )
    insufficient_evidence: bool | None = Field(
        default=None,
        description="Whether the pipeline reported that the documents do not support an answer.",
    )
    truncated: bool = Field(
        default=False, description="Whether the answer stops at the model's output ceiling."
    )

    provider: str | None = Field(default=None, description="Provider that generated the answer.")
    model: str | None = Field(default=None, description="Model that generated it.")
    prompt_name: str | None = Field(default=None, description="Prompt template that produced it.")
    prompt_version: int | None = Field(default=None, description="Version of that template.")

    duration_ms: int | None = Field(default=None, description="Wall-clock time the answer took.")
    retrieval_ms: int | None = Field(default=None, description="Of which, time spent retrieving.")
    generation_ms: int | None = Field(
        default=None,
        description=(
            "Of which, time spent inside the model. Absent when none was called, which is what "
            "happens when retrieval found nothing."
        ),
    )
    prompt_tokens: int | None = Field(default=None, description="Tokens the prompt occupied.")
    completion_tokens: int | None = Field(default=None, description="Tokens the answer occupied.")
    total_tokens: int | None = Field(default=None, description="Total tokens billed.")

    retrieved_count: int | None = Field(
        default=None, description="Passages retrieved for this answer."
    )
    context_count: int | None = Field(
        default=None, description="Of those, how many fitted the context budget."
    )
    context_turns: int = Field(
        default=0,
        description=(
            "Earlier questions carried into this one so a follow-up could be resolved. `0` means "
            "the question stood on its own."
        ),
    )
    top_score: float | None = Field(
        default=None, description="Relevance of the best passage the answer rests on."
    )

    edited_at: datetime | None = Field(
        default=None, description="When this message was last revised, if it was."
    )
    created_at: datetime = Field(description="When it was added to the conversation.")

    feedback: FeedbackRead | None = Field(
        default=None, description="This user's rating of the answer, when they have left one."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Sources attached to this message.",
    )
    @property
    def citation_count(self) -> int:
        """Derived rather than stored, so it cannot disagree with `citations`."""
        return len(self.citations)

    @computed_field(  # type: ignore[prop-decorator]
        description="Distinct documents this answer cites.",
    )
    @property
    def document_count(self) -> int:
        """What a "sources: 3 documents" line reads.

        Distinct documents rather than citations, because two passages of one
        contract are one source to a lawyer.
        """
        return len({citation.document_id for citation in self.citations})


class ConversationRead(BaseModel):
    """One conversation as a list row: what it is called, and where it got to."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique conversation identifier.")
    title: str = Field(description="Name of the conversation.")
    title_is_custom: bool = Field(
        default=False, description="Whether the user named it themselves."
    )
    status: ConversationStatus = Field(description="`active` or `archived`.")
    language: str | None = Field(
        default=None, description="Language answers in this conversation are written in, if pinned."
    )
    case_id: uuid.UUID | None = Field(default=None, description="Case it is pinned to, if any.")

    message_count: int = Field(description="Messages in the thread, both roles.")
    last_message_at: datetime | None = Field(
        default=None, description="When the last message was added."
    )
    last_message_preview: str | None = Field(
        default=None, description="Opening words of the last message, for the list row."
    )

    created_at: datetime = Field(description="When the conversation was opened.")
    updated_at: datetime = Field(description="When it last changed.")


class ConversationDetailRead(ConversationRead):
    """A conversation together with the first page of its transcript.

    One request rather than two, because opening a conversation always needs
    both and a client that had to fetch them separately would render an empty
    thread for one round trip.
    """

    messages: list[MessageRead] = Field(
        default_factory=list, description="Messages, oldest first."
    )
    has_more_messages: bool = Field(
        default=False,
        description=(
            "Whether the thread has messages beyond the ones returned. `true` means fetch the "
            "remainder from the messages endpoint."
        ),
    )


class ConversationPage(BaseModel):
    """One page of the conversation list."""

    items: list[ConversationRead] = Field(description="Conversations on this page.")
    total_records: int = Field(
        description="Total conversations matching the filters, across all pages."
    )
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of conversations per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(
        cls, items: list[ConversationRead], *, total: int, page: int, page_size: int
    ) -> ConversationPage:
        """Assemble a page, deriving ``total_pages`` from the total and size.

        An empty result still reports one page, so a client never renders
        "page 1 of 0".
        """
        return cls(
            items=items,
            total_records=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)) if page_size else 1,
        )


class ConversationMessagePage(BaseModel):
    """One page of a conversation's transcript."""

    items: list[MessageRead] = Field(description="Messages on this page, oldest first.")
    total_records: int = Field(description="Total messages in the conversation.")
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of messages per page.")
    total_pages: int = Field(description="Number of pages available.")

    @classmethod
    def build(
        cls, items: list[MessageRead], *, total: int, page: int, page_size: int
    ) -> ConversationMessagePage:
        """Assemble a page, deriving ``total_pages`` from the total and size."""
        return cls(
            items=items,
            total_records=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)) if page_size else 1,
        )


class MessageSendResponse(BaseModel):
    """What one exchange returns: the question as stored, and the answer.

    Both, rather than only the answer, because the question is persisted with a
    server-assigned identifier and sequence — a client that received only the
    answer would have to re-fetch the transcript to learn where its own message
    landed, or invent a local one and reconcile it later.
    """

    conversation: ConversationRead = Field(
        description="The conversation as it now stands, including its new counters and title."
    )
    user_message: MessageRead = Field(description="The question, as it was stored.")
    assistant_message: MessageRead = Field(
        description="The answer, with its citations and suggested follow-ups."
    )


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class AssistantMetricsRead(BaseModel):
    """Platform-wide assistant health, as the spec's "Monitoring" section describes it.

    The six figures it names — **active conversations, average response time,
    average conversation length, successful requests, failed requests, and user
    feedback statistics** — plus the rates and the failure breakdown.

    Note what is counted where, because the two halves come from different
    places and have different limits: the conversation and feedback figures are
    **queried from the database**, so they cover the whole platform for all time;
    the request counters accumulate **in the API process**, so they reset on
    restart and each instance counts only its own traffic. ``since`` is the
    honest caveat for the second half rather than decoration — the same posture
    :class:`~schemas.rag.RagMetricsRead` and
    :class:`~schemas.search.SearchMetricsRead` take.
    """

    since: datetime = Field(
        description=(
            "When this process started counting requests. The request figures cover traffic since "
            "then, on this instance only; the conversation and feedback figures are read from the "
            "database and cover everything."
        )
    )

    total_conversations: int = Field(description="Conversations on the platform, excluding deleted.")
    active_conversations: int = Field(description="Of those, not archived.")
    archived_conversations: int = Field(description="Of those, archived.")
    total_messages: int = Field(description="Messages across every conversation.")
    average_conversation_length: float | None = Field(
        default=None,
        description=(
            "Mean messages per conversation, both roles. A thread of two is one question and its "
            "answer, so a mean near two means people are not following up."
        ),
    )

    total_requests: int = Field(description="Messages attempted in the window.")
    successful_requests: int = Field(
        description=(
            "Messages that produced an answer. One that reported the documents do not support an "
            "answer counts here: declining is the assistant working, not failing."
        )
    )
    failed_requests: int = Field(
        description="Messages that could not be answered because a dependency failed."
    )
    success_rate: float = Field(description="Percentage of messages that answered, 0-100.")
    failure_rate: float = Field(description="Percentage that failed. Complements `success_rate`.")

    streamed_requests: int = Field(
        description="Of the successful ones, how many were delivered as a stream."
    )

    average_response_ms: float | None = Field(
        default=None,
        description=(
            "Mean end-to-end time to an answer. Failures are excluded: a request that timed out "
            "against an unresponsive provider would make the platform look slow when what it is, "
            "is blocked."
        ),
    )

    grounded_answers: int = Field(description="Successful answers built from retrieved sources.")
    insufficient_evidence: int = Field(
        description="Successful answers reporting that the documents do not support one."
    )
    grounding_rate: float = Field(
        description=(
            "Percentage of successful answers that were grounded. The number to watch, and not an "
            "AI metric: a falling rate means the corpus no longer covers what people ask."
        )
    )

    total_feedback: int = Field(description="Ratings left on answers, across the platform.")
    helpful_feedback: int = Field(description="Of those, `helpful`.")
    not_helpful_feedback: int = Field(description="Of those, `not_helpful`.")
    helpful_rate: float | None = Field(
        default=None,
        description=(
            "Percentage of ratings that were `helpful`. `null` rather than `0` when nobody has "
            "rated anything — zero would read as 'every answer was unhelpful'."
        ),
    )
    rated_messages_rate: float | None = Field(
        default=None,
        description=(
            "Percentage of assistant messages that carry a rating. The denominator for the figure "
            "above: a 90% helpful rate over four ratings is not a measurement."
        ),
    )

    failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Failed messages grouped by cause. A failure rate says something is wrong; this says "
            "what — a missing credential, an unreachable vector database, and a model timing out "
            "read identically otherwise."
        ),
    )

    suggestions_enabled: bool = Field(
        description="Whether answers are followed by suggested next questions."
    )
    streaming_enabled: bool = Field(
        description="Whether answers may be streamed when the provider supports it."
    )
    enabled: bool = Field(
        description="Whether the assistant is permitted to hold conversations on this deployment."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Mean end-to-end response time in seconds, for display.",
    )
    @property
    def average_response_seconds(self) -> float | None:
        """Formatted once, server-side, so every surface expresses it identically."""
        if self.average_response_ms is None:
            return None
        return round(self.average_response_ms / 1000, 3)
