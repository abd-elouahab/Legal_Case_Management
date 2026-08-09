"""create notification tables

Adds the notification storage layer from ``16-notifications.md``: one row per
thing one person was told, and one row per preference they have expressed.
``architecture.md`` has listed *Notifications* under PostgreSQL since the storage
model was written; this migration is that listing, and it is the point at which
**persistence of an event** enters the platform — real-time synchronization is
ephemeral by design, while ``code-standards.md`` requires notifications to be
*"persistent and recoverable"*.

Notes on the choices this migration encodes:

* **No prose is stored.** There is no ``title`` and no ``message`` column. A row
  keeps its ``rule_key`` and a small ``context``; the wording is rendered per
  request by ``core/notifications.py`` in the language the reader asks for. That
  is what makes an Arabic reader's *whole history* Arabic rather than a feed
  frozen per row, what makes *"never log confidential notification contents"*
  trivially true, and what lets a future email sender render from the same module
  instead of restating the wording.
* **Two enums and two open registries**, which is the same split
  ``reports`` and ``timeline_events`` make from opposite ends.
  ``notification_type`` and ``notification_priority`` are PostgreSQL enums:
  closed, four-valued, platform-defined, and each needs an icon and a colour in
  the client, so a fifth arriving without a migration would be a value nothing
  could render. ``category`` and ``rule_key`` are ``VARCHAR``: the spec requires
  future categories and future events *"without redesign"*, and an ``ALTER TYPE``
  per new event type is exactly that redesign. The downgrade drops the two types
  as well as the tables, or a re-upgrade fails with "type already exists" — the
  trap every enum migration here documents.
* **``recipient_id`` and ``user_id`` are ``ON DELETE CASCADE``**, matching
  ``conversations.owner_id`` and ``reports.requested_by``: every read of either
  table is keyed by the person, so an owner-less row is unreachable data nothing
  can serve or clean up. ``actor_id`` and ``case_id`` are ``SET NULL``, because
  they are references *for display* — losing either must not cost the reader the
  notification.
* **``uq_notifications_recipient_event`` is the invariant half of duplicate
  prevention**: one dispatched event produces at most one notification per
  person, whatever redelivers it. It cannot suppress a genuine repeat, because
  an event's identity is assigned once by the dispatcher and never reused.
  ``event_id`` is nullable — a system announcement has no event — and both
  PostgreSQL and SQLite treat NULLs as distinct in a unique index, so
  announcements are unconstrained by it rather than limited to one.
* **``ix_notifications_recipient_dedupe_key`` is the heuristic half**: "has this
  person already been told this, recently?", asked as a windowed query rather
  than enforced as a constraint, because a case genuinely updated twice in a week
  is two notifications and the same case updated twice in ten seconds is one.
* **Four recipient-leading indexes, and each is a query.**
  ``(recipient_id, created_at)`` is the panel and the history page;
  ``(recipient_id, read_at, created_at)`` is the unread badge, which is this
  feature's most frequent read by a wide margin;
  ``(recipient_id, category, created_at)`` is the category filter;
  ``(recipient_id, dedupe_key, created_at)`` is the duplicate check.
  ``(created_at, category)`` is the monitoring window, and the only one that does
  not start from a person.
* **No index on ``archived_at``**, deliberately, even though every read filters on
  it — the same trade ``documents.deleted_at``, ``conversations.deleted_at``, and
  ``reports.deleted_at`` make: it is a two-valued column on a table already
  narrowed to one recipient, so the composite index has done the work before it
  is evaluated.
* **Preferences are one row per ``(user, key)``, not a column per preference.**
  An eighth preference is a new row with no migration; a second delivery channel
  is one nullable boolean beside ``in_app``, and every row already exists to
  receive it. That is what the spec's *"preferences should prepare the platform
  for future delivery channels"* asks for, made structural.

Revision ID: b3d81e5fa27c
Revises: a91c4f27de63
Create Date: 2026-08-08 10:15:41.203118

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "b3d81e5fa27c"
down_revision: str | None = "a91c4f27de63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The closed vocabularies, in the order their Python enums declare them.
NOTIFICATION_TYPES = ("information", "success", "warning", "error")
NOTIFICATION_PRIORITIES = ("low", "normal", "high", "critical")

#: Held at module level so the downgrade can drop the types it created. Note that
#: neither is created explicitly in :func:`upgrade`: ``create_table`` emits
#: ``CREATE TYPE`` ahead of ``CREATE TABLE`` for every enum column, so creating
#: them first as well raises "type already exists" on the very first upgrade.
notification_type_enum = sa.Enum(*NOTIFICATION_TYPES, name="notification_type")
notification_priority_enum = sa.Enum(*NOTIFICATION_PRIORITIES, name="notification_priority")

#: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else — the variant
#: ``timeline_events.metadata`` established, and the reason is the same: JSONB
#: stores parsed and is indexable, and the SQLite test database has no JSONB.
JSON_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # The enum types are created by `create_table` itself — see the note above.
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=True),
        sa.Column("rule_key", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column(
            "notification_type",
            notification_type_enum,
            server_default="information",
            nullable=False,
        ),
        sa.Column(
            "priority", notification_priority_enum, server_default="normal", nullable=False
        ),
        sa.Column("context", JSON_TYPE, server_default="{}", nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("target_type", sa.String(length=30), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.id"],
            name=op.f("fk_notifications_recipient_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_notifications_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            name=op.f("fk_notifications_case_id_cases"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "recipient_id", "event_id", name="uq_notifications_recipient_event"
        ),
    )

    op.create_index(
        op.f("ix_notifications_recipient_id"), "notifications", ["recipient_id"], unique=False
    )
    op.create_index(op.f("ix_notifications_case_id"), "notifications", ["case_id"], unique=False)
    op.create_index(
        op.f("ix_notifications_category"), "notifications", ["category"], unique=False
    )
    op.create_index(
        "ix_notifications_recipient_created_at",
        "notifications",
        ["recipient_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_recipient_read_at",
        "notifications",
        ["recipient_id", "read_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_recipient_category_created_at",
        "notifications",
        ["recipient_id", "category", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_recipient_dedupe_key",
        "notifications",
        ["recipient_id", "dedupe_key", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_created_at_category",
        "notifications",
        ["created_at", "category"],
        unique=False,
    )

    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("preference_key", sa.String(length=50), nullable=False),
        sa.Column("in_app", sa.Boolean(), server_default="true", nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id", "preference_key", name="uq_notification_preferences_user_key"
        ),
    )

    op.create_index(
        op.f("ix_notification_preferences_user_id"),
        "notification_preferences",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_notification_preferences_user_id"), table_name="notification_preferences"
    )
    op.drop_table("notification_preferences")

    op.drop_index("ix_notifications_created_at_category", table_name="notifications")
    op.drop_index("ix_notifications_recipient_dedupe_key", table_name="notifications")
    op.drop_index("ix_notifications_recipient_category_created_at", table_name="notifications")
    op.drop_index("ix_notifications_recipient_read_at", table_name="notifications")
    op.drop_index("ix_notifications_recipient_created_at", table_name="notifications")
    op.drop_index(op.f("ix_notifications_category"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_case_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_recipient_id"), table_name="notifications")
    op.drop_table("notifications")

    # Dropped explicitly: `drop_table` does not remove the enum types it caused
    # to be created, so a re-upgrade would fail with "type already exists". The
    # `checkfirst` guard keeps the downgrade idempotent on a database where they
    # were already removed by hand.
    bind = op.get_bind()
    notification_priority_enum.drop(bind, checkfirst=True)
    notification_type_enum.drop(bind, checkfirst=True)
