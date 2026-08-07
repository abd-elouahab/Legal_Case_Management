"""AI Legal Assistant ORM models.

The fifth stage of the AI pipeline, and the first one that **persists what a user
said**. Everything below it is derived from a document: OCR extracts, indexing
embeds, search retrieves, and the RAG pipeline answers — none of them stores a
question, and ``12-rag-pipeline.md`` deliberately gave the pipeline no entity at
all. ``architecture.md`` already lists *AI Conversations* under PostgreSQL, and
this module is that listing.

**Three tables, and each earns its own.**

* :class:`Conversation` is the thread: who owns it, what it is called, whether it
  is archived, and the counters a list row is built from.
* :class:`ConversationMessage` is one turn. One row per message rather than a
  JSON blob on the conversation, because a message is what feedback points at,
  what a future edit would revise, and what pagination pages — none of which a
  blob can express.
* :class:`MessageFeedback` is a rating of one assistant message.
  ``13-ai-legal-assistant.md``: *"Feedback should not modify conversation
  history."* Storing a rating on the message row would make that a rule someone
  has to remember; storing it in its own table makes it **structural** — rating
  an answer writes to a different table than the one the transcript is read
  from, so it cannot alter a transcript even by accident.

**A conversation belongs to exactly one user, and that is its whole access
story.** The spec is explicit — *"Each conversation belongs to exactly one
authenticated user"*, *"The AI Assistant must never expose conversations
belonging to another user"* — and there is no case, no assignment, and no
sharing to reason about. So unlike documents, OCR results, indexes, and
timelines, there is **no per-resource access module** here: every read in
:mod:`repositories.conversation` is keyed by ``(id, owner_id)``, which means
there is no query in the platform that can return another user's conversation.

**Nothing here retrieves, prompts, or cites.** A message *records* what the RAG
pipeline produced — its answer, its citations, its provenance — and the pipeline
remains the only thing that can produce any of it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from db.base import Base
from models.user import User

#: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else — the same variant
#: :mod:`models.timeline` uses, and for the same two reasons: JSONB stores parsed
#: and is indexable, and the SQLite test database has no JSONB at all.
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class ConversationRole(StrEnum):
    """Who wrote a message.

    Declared here rather than in :mod:`core.assistant` for the reason
    :class:`~models.user.UserRole` is declared in :mod:`models.user`: it is
    *persisted*, so the storage enum is the canonical definition and everything
    else re-exports it. :mod:`core.assistant` does exactly that, so domain code
    has a single intention-revealing import site.

    Two members and deliberately no third. A ``system`` role would be the
    platform's own instructions — which live in a **prompt template**, versioned
    on disk, and are the RAG pipeline's business rather than a row in a
    conversation a user can read back.
    """

    #: Written by the authenticated user.
    USER = "user"
    #: Produced by the RAG pipeline on the platform's behalf.
    ASSISTANT = "assistant"


class ConversationStatus(StrEnum):
    """Whether a conversation is in the user's working set.

    Two members, because the spec lists exactly two operations that change it:
    *archive* and *delete*. Deletion is not a status here — see
    :attr:`Conversation.deleted_at`.
    """

    #: In the user's list, and open to new messages.
    ACTIVE = "active"
    #: Kept and readable, but out of the default list and closed to new messages.
    #: Archiving a thread the user considers finished is what keeps the list a
    #: working set rather than an ever-growing log.
    ARCHIVED = "archived"


class FeedbackRating(StrEnum):
    """How a user rated one assistant message.

    Exactly the two ``13-ai-legal-assistant.md`` requires *"at minimum"*. A
    numeric scale was considered and rejected: two options is what people
    actually answer, and a five-point scale on a legal answer invites a judgement
    about *correctness* that a reader is not in a position to make in one click.
    A third member is a value added here and a migration — the enum is closed on
    purpose so adding one is a deliberate act.
    """

    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"


class Conversation(Base):
    """One assistant thread, owned by exactly one user."""

    __tablename__ = "conversations"

    __table_args__ = (
        # The list query: this user's conversations, newest activity first,
        # filtered by status and excluding deleted rows. One composite index
        # rather than three single-column ones, because every read here starts
        # from the owner and no read starts from anything else.
        Index(
            "ix_conversations_owner_id_status_last_message_at",
            "owner_id",
            "status",
            "last_message_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: Who owns this conversation. ``CASCADE`` rather than the ``SET NULL`` every
    #: other user reference on the platform uses, and it is the one place that
    #: differs: an audit record must outlive the account that caused it, but a
    #: conversation **is** the account's private working material and has no
    #: meaning once nobody owns it. A null owner here would also be a row that
    #: every ``owner_id = :caller`` query silently excludes — unreachable data
    #: that still contains a user's questions.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Short, descriptive, and editable, exactly as the spec asks. Derived from
    #: the first question by :func:`~core.assistant.derive_title` and replaced by
    #: whatever the user renames it to.
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Whether the user themselves named this conversation. Recorded so that
    #: automatic titling never overwrites a title someone chose — without it, a
    #: future "re-title from the conversation" job would have no way to tell a
    #: generated name from a deliberate one.
    title_is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    status: Mapped[ConversationStatus] = mapped_column(
        Enum(
            ConversationStatus,
            name="conversation_status",
            # Persist the enum *values* rather than the Python member names,
            # matching `user_role`, `case_status`, `document_category`,
            # `ocr_status`, and `index_status`.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ConversationStatus.ACTIVE,
        server_default=ConversationStatus.ACTIVE.value,
    )

    #: The language the assistant answers in for this thread, when the user
    #: pinned one. ``None`` detects it per question, which is the right default
    #: for someone typing in their own language and the wrong one for a French
    #: interface asking about an Arabic filing.
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)

    #: The case this conversation is pinned to, when it was opened from a case
    #: workspace. Purely a **default filter** for retrieval and a label for the
    #: list — it grants nothing. Every retrieval still runs through the search
    #: service, which refuses a case the caller is not party to, so a pinned case
    #: can only ever narrow what an answer is built from. ``SET NULL`` because
    #: losing the case must not cost the conversation.
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # ------------------------------------------------------------- counters #

    #: Messages in the thread, both roles. Maintained by the service rather than
    #: counted per row, because it is read once per list row and a ``COUNT(*)``
    #: per row is the classic N+1 on a page of twenty.
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: When the last message was added, and a preview of it. Denormalized for the
    #: same reason: a conversation list is "title, last thing said, when" and
    #: reading that from the messages table would be a correlated subquery per
    #: row. Both have a single writer — the service that appends the message.
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_message_preview: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # -------------------------------------------------------------- deletion #

    #: When this conversation was deleted, or ``None``.
    #:
    #: Logical, like a document's, and for a reason particular to this feature:
    #: the messages carry the citations of an answer a user may have acted on, so
    #: a delete that destroyed the row would destroy the record of advice that
    #: was given. A deleted conversation is excluded from every read — the API
    #: answers 404 for it — and a future retention job is what actually reclaims
    #: the rows.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # -------------------------------------------------------- relationships #

    owner: Mapped[User] = relationship("User", foreign_keys=[owner_id], lazy="selectin")

    # ------------------------------------------------------------- derived #

    @property
    def is_archived(self) -> bool:
        """Whether the conversation is out of the working set."""
        return self.status is ConversationStatus.ARCHIVED

    @property
    def is_deleted(self) -> bool:
        """Whether the conversation has been withdrawn."""
        return self.deleted_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the title and never a preview: both quote a user's question, and
        # a repr ends up in whatever log line interpolates the object. The same
        # rule `DocumentIndex.__repr__` follows for filenames.
        return (
            f"<Conversation id={self.id!s} owner_id={self.owner_id!s} "
            f"status={self.status.value!r} messages={self.message_count}>"
        )


class ConversationMessage(Base):
    """One turn: what was asked, or what the pipeline answered."""

    __tablename__ = "conversation_messages"

    __table_args__ = (
        # The transcript query, and the only ordering this table is ever read in.
        # `sequence` rather than `created_at` because two messages of one turn are
        # written milliseconds apart and a timestamp tie would order a question
        # after its own answer.
        Index(
            "ix_conversation_messages_conversation_id_sequence",
            "conversation_id",
            "sequence",
        ),
        # One message per position in a conversation. The transcript's integrity
        # made a database rule rather than a service one: without it, two
        # concurrent sends could both append at position 7 and the thread would
        # have no defined order.
        UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_conversation_messages_conversation_id_sequence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Position in the thread, 1-based and contiguous. Stored rather than derived
    #: so ordering survives a future edit that changes a message's timestamp —
    #: which is exactly the "support future message editing without redesign" the
    #: spec asks for.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    role: Mapped[ConversationRole] = mapped_column(
        Enum(
            ConversationRole,
            name="conversation_role",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    #: What was written. The user's question verbatim, or the pipeline's answer
    #: verbatim — never a summary of either, and never the prompt that was sent
    #: to the model, which is the pipeline's internal business and would put the
    #: retrieved passages of a case file into a second table.
    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: The language this message is written in, as the pipeline settled it.
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # ------------------------------------------------- editing, when it comes #

    #: When this message was last revised, and what it said before.
    #:
    #: Both are ``None`` today and nothing writes them: ``13-ai-legal-assistant.md``
    #: asks the implementation to *"support future message editing without
    #: redesign"*, and this is that support — an edit becomes an ``UPDATE`` of
    #: ``content`` plus these two columns, with no change to the transcript's
    #: shape, its ordering, or anything reading it. Present rather than added
    #: later because adding them later is the migration the spec is asking to
    #: avoid.
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------ what produced it #

    #: The sources the answer was grounded in, exactly as the RAG pipeline
    #: returned them — document, version, page, case, excerpt, and marker.
    #:
    #: JSON rather than a fourth table, and the reason is a boundary rather than
    #: convenience: a citation is **the pipeline's output**, and modelling it
    #: relationally here would create a second, normalized definition of a
    #: citation that this feature would then own and have to keep in step. The
    #: spec says the assistant *"should display citations without modifying
    #: them"*; storing them as an opaque record is what makes that structural.
    #: Nothing queries inside this column.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON_TYPE, nullable=False, default=list, server_default="[]"
    )

    #: Suggested follow-up questions offered after this answer. Stored so a
    #: reloaded transcript still shows them: regenerating on read would cost a
    #: model call per page view and could suggest something different each time.
    suggestions: Mapped[list[str]] = mapped_column(
        _JSON_TYPE, nullable=False, default=list, server_default="[]"
    )

    #: Whether the answer was built from retrieved passages, and whether the
    #: pipeline reported that the documents do not support one. Both come
    #: straight off the pipeline's response and are stored rather than inferred
    #: from the citation list, because "no citations" and "declined to answer"
    #: are different things a reader must be able to tell apart.
    grounded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    insufficient_evidence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    #: Whether the answer hit the model's output ceiling and stops mid-thought.
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: How the answer was produced. Recorded per message rather than read from
    #: configuration, for the reason an index records its embedding model:
    #: configuration is *current* and a message is *historical*, and an
    #: evaluation run comparing two prompts or two models cannot group answers
    #: that do not say which produced them.
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Timings and usage, as reported. Nullable throughout because a user message
    #: has none and a no-evidence answer never called a model — ``0`` would read
    #: as "this answer was free", which is a different and false statement.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieval_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Passages retrieved, and how many reached the prompt. Kept even though the
    #: citations are stored, because a grounded answer returns citations only for
    #: the sources it was given, and "retrieved 8, used 3" is what tells an
    #: operator whether the context budget is doing the limiting.
    retrieved_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How many earlier questions were carried into this one — see
    #: :func:`~core.assistant.resolve_question`. Zero means the question stood on
    #: its own, which is worth being able to see when an answer surprises someone.
    context_turns: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: Relevance of the best passage the answer rests on. One number rather than
    #: a distribution: it is the "is this answer standing on anything solid"
    #: signal, and the citations carry the per-source scores.
    top_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # -------------------------------------------------------- relationships #

    conversation: Mapped[Conversation] = relationship("Conversation", lazy="select")

    feedback: Mapped[MessageFeedback | None] = relationship(
        "MessageFeedback",
        back_populates="message",
        uselist=False,
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    # ------------------------------------------------------------- derived #

    @property
    def is_from_user(self) -> bool:
        """Whether the user wrote this message."""
        return self.role is ConversationRole.USER

    @property
    def is_edited(self) -> bool:
        """Whether this message has been revised since it was sent."""
        return self.edited_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the content: it is a lawyer's question or an answer about a
        # client's matter, and neither belongs in a log line.
        return (
            f"<ConversationMessage id={self.id!s} conversation_id={self.conversation_id!s} "
            f"sequence={self.sequence} role={self.role.value!r}>"
        )


class MessageFeedback(Base):
    """One user's rating of one assistant message."""

    __tablename__ = "message_feedback"

    __table_args__ = (
        # One rating per message. A user changes their mind by updating this row,
        # never by appending a second one — an answer with two contradictory
        # ratings from the same person is not an evaluation signal, it is noise.
        UniqueConstraint("message_id", name="uq_message_feedback_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("conversation_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Who rated it. Always the conversation's owner today — nobody else can
    #: reach the message — but stored rather than inferred, because a rating is
    #: evidence for a future evaluation and evidence that says "somebody" is not
    #: evidence. ``SET NULL`` so removing an account keeps the rating: the
    #: judgement about the answer survives the person who made it, which is what
    #: an aggregate needs.
    rated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    rating: Mapped[FeedbackRating] = mapped_column(
        Enum(
            FeedbackRating,
            name="feedback_rating",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    #: An optional note. Free text, and the one field on this feature that a user
    #: might paste a document into — so it is length-capped in the schema and,
    #: like everything else here, never logged.
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # -------------------------------------------------------- relationships #

    message: Mapped[ConversationMessage] = relationship(
        "ConversationMessage", back_populates="feedback", lazy="select"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the comment, which is free text a user typed.
        return (
            f"<MessageFeedback id={self.id!s} message_id={self.message_id!s} "
            f"rating={self.rating.value!r}>"
        )
