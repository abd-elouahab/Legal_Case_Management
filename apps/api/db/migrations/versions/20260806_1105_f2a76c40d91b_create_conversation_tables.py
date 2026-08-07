"""create conversation tables

Adds the AI Legal Assistant storage layer from ``13-ai-legal-assistant.md``:
conversations, the messages in them, and the feedback users leave on answers.
``architecture.md`` already listed *AI Conversations* under PostgreSQL; this is
the first migration in the AI pipeline to write anything a **user** said, because
OCR, indexing, search, and the RAG pipeline all persist only what is derived from
a document.

Notes on the choices this migration encodes:

* **Three tables, not one.** A message could have been a JSON array on the
  conversation, and that would have been wrong three times over: feedback points
  at a *message*, pagination pages messages, and the "support future message
  editing without redesign" the spec asks for is an ``UPDATE`` of one row rather
  than a rewrite of an array. And feedback gets its own table specifically so
  that *"feedback should not modify conversation history"* is structural — a
  rating writes to a table the transcript is not read from.
* **``conversations.owner_id`` is ``ON DELETE CASCADE``**, and it is the only
  user reference on the platform that is not ``SET NULL``. Every other one is an
  audit trail that must outlive the account; a conversation *is* the account's
  private working material, and an owner-less row would be data no
  ``owner_id = :caller`` query can ever return while still containing a lawyer's
  questions. ``message_feedback.rated_by`` keeps the platform's usual ``SET
  NULL``, because a judgement about an answer is evidence for a future
  evaluation and outlives the person who made it.
* **``conversations.case_id`` is ``SET NULL``.** A pinned case is a default
  filter and a label, never a grant: retrieval still runs through the search
  service, which refuses a case the caller is not party to. Losing the case must
  not cost the conversation.
* **``uq_conversation_messages_conversation_id_sequence``** is the transcript's
  integrity made a database rule. The service assigns the next position, but
  check-then-insert has a race in the middle, and two concurrent sends both
  landing at position 7 is a thread with no defined order — the same reasoning as
  ``uq_document_indexes_document_id_document_version``.
* **``uq_message_feedback_message_id``** makes changing your mind an ``UPDATE``.
  Two contradictory ratings of one answer by one person is not an evaluation
  signal, it is noise.
* **Three PostgreSQL enums** — ``conversation_status``, ``conversation_role``,
  ``feedback_rating`` — like ``ocr_status`` and ``index_status`` and unlike
  ``timeline_events.event_type``: closed, small vocabularies the platform itself
  defines, not open registries future modules extend. The downgrade therefore has
  to drop the types as well as the tables, or a re-upgrade fails with "type
  already exists" — the trap every enum migration here documents, and which the
  OCR migration hit for real because the SQLite test database has no
  ``CREATE TYPE`` to fail on.
* **``citations`` and ``suggestions`` are JSON**, with the same
  ``JSONB``-on-PostgreSQL variant ``timeline_events.metadata`` uses. A citation
  is the *RAG pipeline's* output and the spec says the assistant displays it
  *"without modifying"* it; a normalized table here would be a second definition
  of a citation that this feature would then own. Nothing queries inside either
  column.
* **``ix_conversations_owner_id_status_last_message_at``** is the list query,
  whole: every read of this table starts from the owner, filters by status, and
  orders by recent activity. One composite index rather than three single-column
  ones for that reason.
* **No index on ``conversations.deleted_at``**, deliberately, even though every
  read filters on it. It is a two-valued column on a table already narrowed to
  one owner, so the composite index above has done the work before it is
  evaluated — the same trade ``documents.deleted_at`` makes.

Revision ID: f2a76c40d91b
Revises: c47f2a91b8de
Create Date: 2026-08-06 11:05:38.421907

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "f2a76c40d91b"
down_revision: str | None = "c47f2a91b8de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The vocabularies, in the order their Python enums declare them.
CONVERSATION_STATUSES = ("active", "archived")
CONVERSATION_ROLES = ("user", "assistant")
FEEDBACK_RATINGS = ("helpful", "not_helpful")

#: Held at module level so the downgrade can drop the types it created. Note that
#: none is created explicitly in :func:`upgrade`: ``create_table`` emits
#: ``CREATE TYPE`` ahead of ``CREATE TABLE`` for every enum column, so creating
#: them first as well raises "type already exists" on the very first upgrade.
conversation_status_enum = sa.Enum(*CONVERSATION_STATUSES, name="conversation_status")
conversation_role_enum = sa.Enum(*CONVERSATION_ROLES, name="conversation_role")
feedback_rating_enum = sa.Enum(*FEEDBACK_RATINGS, name="feedback_rating")

#: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else — the variant
#: ``timeline_events.metadata`` established, and the reason is the same: JSONB
#: stores parsed and is indexable, and the SQLite test database has no JSONB.
JSON_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # The enum types are created by `create_table` itself — see the note above.
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "title_is_custom", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column(
            "status", conversation_status_enum, server_default="active", nullable=False
        ),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_preview", sa.String(length=255), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_conversations_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            name=op.f("fk_conversations_case_id_cases"),
            ondelete="SET NULL",
        ),
    )

    op.create_index(op.f("ix_conversations_owner_id"), "conversations", ["owner_id"], unique=False)
    op.create_index(op.f("ix_conversations_case_id"), "conversations", ["case_id"], unique=False)
    op.create_index(
        "ix_conversations_owner_id_status_last_message_at",
        "conversations",
        ["owner_id", "status", "last_message_at"],
        unique=False,
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", conversation_role_enum, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_content", sa.Text(), nullable=True),
        sa.Column("citations", JSON_TYPE, server_default="[]", nullable=False),
        sa.Column("suggestions", JSON_TYPE, server_default="[]", nullable=False),
        sa.Column("grounded", sa.Boolean(), nullable=True),
        sa.Column("insufficient_evidence", sa.Boolean(), nullable=True),
        sa.Column("truncated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_name", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("retrieval_ms", sa.Integer(), nullable=True),
        sa.Column("generation_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("retrieved_count", sa.Integer(), nullable=True),
        sa.Column("context_count", sa.Integer(), nullable=True),
        sa.Column("context_turns", sa.Integer(), server_default="0", nullable=False),
        sa.Column("top_score", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_messages")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_conversation_messages_conversation_id_sequence",
        ),
    )

    op.create_index(
        op.f("ix_conversation_messages_conversation_id"),
        "conversation_messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_messages_conversation_id_sequence",
        "conversation_messages",
        ["conversation_id", "sequence"],
        unique=False,
    )

    op.create_table(
        "message_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("rated_by", sa.Uuid(), nullable=True),
        sa.Column("rating", feedback_rating_enum, nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_feedback")),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["conversation_messages.id"],
            name=op.f("fk_message_feedback_message_id_conversation_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rated_by"],
            ["users.id"],
            name=op.f("fk_message_feedback_rated_by_users"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("message_id", name="uq_message_feedback_message_id"),
    )

    op.create_index(
        op.f("ix_message_feedback_message_id"), "message_feedback", ["message_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_message_feedback_message_id"), table_name="message_feedback")
    op.drop_table("message_feedback")

    op.drop_index(
        "ix_conversation_messages_conversation_id_sequence", table_name="conversation_messages"
    )
    op.drop_index(
        op.f("ix_conversation_messages_conversation_id"), table_name="conversation_messages"
    )
    op.drop_table("conversation_messages")

    op.drop_index("ix_conversations_owner_id_status_last_message_at", table_name="conversations")
    op.drop_index(op.f("ix_conversations_case_id"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_owner_id"), table_name="conversations")
    op.drop_table("conversations")

    # Dropped explicitly: PostgreSQL keeps an enum type after its last column is
    # gone, and leaving one behind makes a re-upgrade fail with "type already
    # exists". A true inverse has to remove all three.
    feedback_rating_enum.drop(op.get_bind(), checkfirst=True)
    conversation_role_enum.drop(op.get_bind(), checkfirst=True)
    conversation_status_enum.drop(op.get_bind(), checkfirst=True)
