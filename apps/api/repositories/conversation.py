"""AI Legal Assistant data access.

Single responsibility: reading and writing :class:`~models.conversation.Conversation`,
:class:`~models.conversation.ConversationMessage`, and
:class:`~models.conversation.MessageFeedback` rows. No authorization *policy* and
no decisions about what an answer is worth — those belong to
:class:`~services.assistant.AssistantService` and to the RAG pipeline beneath it.

**Ownership is not a policy here, it is the shape of every query.**
``13-ai-legal-assistant.md``: *"Users may only access their own conversations"*
and *"The AI Assistant must never expose conversations belonging to another
user"*. Every read below takes an ``owner_id`` and puts it in the ``WHERE``
clause — there is no method that fetches a conversation by identifier alone, and
therefore no call site anywhere in the platform that could forget to scope one.
That is why this feature has **no ``conversation_access.py``**: the other modules
need one because "may this caller reach this row" is a question about case
assignments that several services ask; here it is a single equality that the
query itself asserts.

Filtering, paging, and counting all execute **in the database**, so the cost of a
page does not grow with the length of a user's history — and so the page totals
count only conversations the caller owns.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from models.conversation import (
    Conversation,
    ConversationMessage,
    ConversationStatus,
    FeedbackRating,
    MessageFeedback,
)
from schemas.conversation import ConversationListQuery


class ConversationRepository:
    """Queries and writes for the assistant's three tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --------------------------------------------------------- conversations #

    def get(self, conversation_id: uuid.UUID, *, owner_id: uuid.UUID) -> Conversation | None:
        """Return this user's conversation with that identifier, or ``None``.

        **There is deliberately no ``get_by_id``.** A method that resolved a
        conversation without an owner would be the one call site that could
        return somebody else's, and the whole of this feature's authorization
        rests on that method not existing.

        Deleted conversations are excluded, so a withdrawn thread answers 404
        exactly as a nonexistent one does — deletion is logical (see
        :attr:`~models.conversation.Conversation.deleted_at`) so the row survives
        for a future retention job, not so it can still be read.
        """
        statement = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.owner_id == owner_id,
            Conversation.deleted_at.is_(None),
        )
        return self._session.execute(statement).scalars().first()

    def list_conversations(
        self, query: ConversationListQuery, *, owner_id: uuid.UUID
    ) -> tuple[list[Conversation], int]:
        """Return one page of this user's conversations and the total matching.

        Ordered by **last activity, then creation**, newest first. Two keys
        because a conversation with no messages has no activity timestamp at all,
        and ordering by a nullable column alone would drop every brand-new thread
        to the bottom of the list — which is precisely where the one the user just
        created must not be.

        The total is counted over the *filtered and scoped* set but before
        pagination, from the same clause as the page itself, so it cannot drift
        when a filter is added later.
        """
        filtered = self._apply_filters(select(Conversation), query, owner_id=owner_id)

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.created_at.desc(),
                Conversation.id.desc(),
            )
            .offset(query.offset)
            .limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().unique().all()), total

    def add(self, conversation: Conversation) -> Conversation:
        """Insert one conversation and return it with its generated columns populated."""
        self._session.add(conversation)
        self._session.commit()
        self._session.refresh(conversation)
        return conversation

    def save(self, conversation: Conversation) -> Conversation:
        """Persist changes to a conversation already in the session."""
        self._session.commit()
        self._session.refresh(conversation)
        return conversation

    def soft_delete(self, conversation: Conversation, *, at: datetime | None = None) -> Conversation:
        """Withdraw a conversation without destroying it.

        Logical for the reason the model records: the transcript carries the
        citations of advice a lawyer may have acted on, and a hard delete would
        destroy the record of what was said. Idempotent — deleting an already
        deleted conversation keeps the original timestamp, so "when was this
        withdrawn" stays answerable.
        """
        if conversation.deleted_at is None:
            conversation.deleted_at = at or datetime.now(UTC)
        return self.save(conversation)

    # -------------------------------------------------------------- messages #

    def next_sequence(self, conversation_id: uuid.UUID) -> int:
        """The position the next message takes, 1-based.

        Read rather than derived from ``message_count`` so the two cannot drift:
        the counter is a denormalization for the list row, and the sequence is the
        transcript's order. A race between two concurrent sends is caught by
        ``uq_conversation_messages_conversation_id_sequence`` rather than by this
        read — which is the point of having the constraint.
        """
        highest = self._session.execute(
            select(func.max(ConversationMessage.sequence)).where(
                ConversationMessage.conversation_id == conversation_id
            )
        ).scalar_one_or_none()
        return int(highest or 0) + 1

    def add_messages(self, messages: Sequence[ConversationMessage]) -> list[ConversationMessage]:
        """Append messages and return them with their generated columns populated.

        A question and its answer are written in **one transaction**, deliberately:
        a transcript containing a question whose answer was lost to a crash is a
        conversation the user cannot resume and cannot make sense of.
        """
        for message in messages:
            self._session.add(message)
        self._session.commit()
        for message in messages:
            self._session.refresh(message)
        return list(messages)

    def get_message(
        self, message_id: uuid.UUID, *, conversation_id: uuid.UUID
    ) -> ConversationMessage | None:
        """Return one message of a conversation, or ``None``.

        Scoped by conversation as well as by identifier, so reaching a message
        requires first having resolved a conversation the caller owns — the same
        reasoning that gives :meth:`get` no unscoped variant.
        """
        statement = select(ConversationMessage).where(
            ConversationMessage.id == message_id,
            ConversationMessage.conversation_id == conversation_id,
        )
        return self._session.execute(statement).scalars().first()

    def list_messages(
        self, conversation_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[list[ConversationMessage], int]:
        """Return one page of a transcript, oldest first, and the total.

        Oldest first because a transcript is *read* in the order it happened —
        the opposite of the timeline, which is a feed of what changed most
        recently.
        """
        base = select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation_id
        )

        total = self._session.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()

        page = base.order_by(ConversationMessage.sequence.asc()).offset(offset).limit(limit)
        return list(self._session.execute(page).scalars().unique().all()), total

    def recent_messages(
        self, conversation_id: uuid.UUID, *, limit: int
    ) -> list[ConversationMessage]:
        """The last few messages of a conversation, oldest first.

        What :func:`~core.assistant.resolve_question` reads to resolve a
        follow-up. Fetched newest-first with a ``LIMIT`` and reversed in Python
        rather than ordered ascending, because the alternative — reading the
        whole thread to take its tail — is a query whose cost grows with the
        conversation, on the platform's most frequent write path.
        """
        if limit <= 0:
            return []

        statement = (
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.sequence.desc())
            .limit(limit)
        )
        return list(reversed(list(self._session.execute(statement).scalars().unique().all())))

    # -------------------------------------------------------------- feedback #

    def get_feedback(self, message_id: uuid.UUID) -> MessageFeedback | None:
        """Return the rating left on a message, or ``None``."""
        statement = select(MessageFeedback).where(MessageFeedback.message_id == message_id)
        return self._session.execute(statement).scalars().first()

    def save_feedback(self, feedback: MessageFeedback) -> MessageFeedback:
        """Insert or update one rating and return it."""
        self._session.add(feedback)
        self._session.commit()
        self._session.refresh(feedback)
        return feedback

    def delete_feedback(self, feedback: MessageFeedback) -> None:
        """Withdraw a rating.

        A **hard** delete, and the only one in this repository. Everything else
        here is a record of what was said and is kept; a rating is an opinion its
        author is entitled to take back, and a withdrawn rating that lingered as a
        soft-deleted row would keep skewing the very statistics it was removed
        from.
        """
        self._session.delete(feedback)
        self._session.commit()

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when a write fails: a failed flush leaves the session unusable
        until the transaction is rolled back, and the request still has to return
        something.
        """
        self._session.rollback()

    # ------------------------------------------------------------- reporting #

    def conversation_counts(self) -> dict[str, int]:
        """Conversations per status, platform-wide, excluding deleted ones.

        Not scoped to a caller: this feeds the administrative monitoring view,
        which is gated on ``ai:monitor`` and reports **counts only** — never a
        title, a question, an answer, or whose conversation it was.
        """
        rows = self._session.execute(
            select(Conversation.status, func.count())
            .where(Conversation.deleted_at.is_(None))
            .group_by(Conversation.status)
        ).all()
        return {str(status.value): int(count) for status, count in rows}

    def message_count(self) -> int:
        """Messages across every conversation that has not been deleted."""
        statement = (
            select(func.count())
            .select_from(ConversationMessage)
            .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
            .where(Conversation.deleted_at.is_(None))
        )
        return int(self._session.execute(statement).scalar_one())

    def assistant_message_count(self) -> int:
        """Answers across every conversation that has not been deleted.

        The denominator for "what share of answers were rated": rating a question
        is impossible, so counting every message would make the coverage figure
        permanently look half what it is.
        """
        statement = (
            select(func.count())
            .select_from(ConversationMessage)
            .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
            .where(
                Conversation.deleted_at.is_(None),
                ConversationMessage.role == "assistant",
            )
        )
        return int(self._session.execute(statement).scalar_one())

    def feedback_counts(self) -> dict[str, int]:
        """Ratings per value, platform-wide.

        Counted from the feedback table alone rather than joined back to live
        conversations, deliberately: a rating is evaluation evidence about an
        *answer*, and deleting the conversation it was left in does not make the
        answer stop having been unhelpful.
        """
        rows = self._session.execute(
            select(MessageFeedback.rating, func.count()).group_by(MessageFeedback.rating)
        ).all()
        counts = {str(rating.value): int(count) for rating, count in rows}
        return {member.value: counts.get(member.value, 0) for member in FeedbackRating}

    # -------------------------------------------------------------- helpers #

    @staticmethod
    def _apply_filters(
        statement: Select[tuple[Conversation]],
        query: ConversationListQuery,
        *,
        owner_id: uuid.UUID,
    ) -> Select[tuple[Conversation]]:
        """Narrow a conversation query by its owner and the requested filters.

        The owner condition is applied **first and unconditionally**, and every
        filter is one more ``AND`` — so no combination of user-supplied filters
        can widen the set beyond one user's own threads. The same shape
        :mod:`repositories.search` uses to keep a metadata filter from widening a
        case scope.
        """
        statement = statement.where(
            Conversation.owner_id == owner_id, Conversation.deleted_at.is_(None)
        )

        # Omitted means active only. An archived thread is out of the working set
        # by definition, and returning it by default would make archiving do
        # nothing the user can see.
        statement = statement.where(
            Conversation.status == (query.status or ConversationStatus.ACTIVE)
        )

        if query.case_id is not None:
            statement = statement.where(Conversation.case_id == query.case_id)

        if query.search:
            # ILIKE with escaped wildcards: a term containing % or _ must match
            # those characters literally rather than turning into a pattern.
            pattern = f"%{_escape_like(query.search)}%"
            statement = statement.where(Conversation.title.ilike(pattern, escape="\\"))

        return statement


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
