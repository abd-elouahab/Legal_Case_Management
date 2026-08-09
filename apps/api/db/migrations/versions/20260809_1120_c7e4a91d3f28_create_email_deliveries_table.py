"""create email deliveries table and the email preference channel

Adds the storage layer from ``17-email-delivery-channel.md``: one row per
notification the platform tried to deliver by email, and one column per person
per preference saying whether they want that channel at all.

Notes on the choices this migration encodes:

* **The preference change is one column, exactly as promised.**
  ``16-notifications.md`` shipped ``notification_preferences`` as one row per
  ``(user, preference_key)`` with an ``in_app`` boolean, and recorded that *"a
  second delivery channel is one boolean column beside it, and every row already
  exists to receive it"*. This is that column. There is **no new table, no new
  key, and no backfill**: an account that has never opened the settings page
  still has no row and still follows the platform defaults, so the default
  reaches every untouched account for free.
* **``NOT NULL DEFAULT true`` rather than nullable.** A nullable column would
  make "has not chosen" and "chose on" two stored states for one behaviour, and
  the platform already represents "has not chosen" perfectly well as *no row*.
  The existing rows — written by users who expressed an in-app opinion before
  this channel existed — take the server default, which is the same answer a user
  with no row gets.
* **No prose is stored on a delivery either.** There is no ``subject`` and no
  ``body`` column, for the reason ``notifications`` has no ``title`` and no
  ``message``: the wording is rendered per attempt from ``core/notifications.py``
  and the templates in ``apps/api/emails/``, and a column holding the contents of
  an email is exactly what the spec's Logging section forbids putting in a log —
  a table is not a better place for it.
* **``uq_email_deliveries_notification`` is the duplicate guard**, and it is an
  invariant rather than a heuristic: one notification is one email, whatever
  re-dispatches it. Retrying re-uses the row — the same shape ``ocr_results`` and
  ``document_indexes`` use — so scheduling the same work twice cannot produce two
  messages. Note the contrast with ``notifications``, which needs *two* duplicate
  mechanisms because it is deduplicating events; here the notification's own
  identity has already done that work.
* **``recipient_email`` is a snapshot, not a join.** ``timeline_events`` snapshots
  its actor for the same reason: a join renders the address the account has
  *today*, so a user who changed their email would silently rewrite the record of
  where messages were actually sent — and that is the first question of any
  delivery investigation.
* **One database enum and one ``VARCHAR``.** ``email_delivery_status`` is closed,
  four-valued, and platform-defined, so it is an enum like ``report_status``;
  ``error_code`` is an open registry that grows as providers are added, so it is a
  ``VARCHAR`` like ``timeline_events.event_type`` — an ``ALTER TYPE`` per new
  provider failure is precisely the redesign the spec asks to avoid. The
  downgrade drops the type as well as the table, or a re-upgrade fails with "type
  already exists" — the trap every enum migration here documents.
* **Three indexes, and each is a query.**
  ``(status, next_attempt_at)`` is the retry sweeper: everything queued and due.
  ``(recipient_id, created_at)`` is "why did this person not get the email?",
  which is what the spec's troubleshooting requirement is actually about.
  ``(created_at, status)`` is the monitoring window, and the only one that does
  not start from a person.
* **``ON DELETE CASCADE`` on both foreign keys.** A delivery describes an attempt
  to send one specific notification to one specific person; without either it is a
  record of nothing that anything could render, explain, or clean up — the same
  reasoning ``notifications.recipient_id`` and ``reports.requested_by`` record.

Revision ID: c7e4a91d3f28
Revises: b3d81e5fa27c
Create Date: 2026-08-09 11:20:07.884215

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e4a91d3f28"
down_revision: str | Sequence[str] | None = "b3d81e5fa27c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The closed vocabulary, in the order its Python enum declares it.
EMAIL_DELIVERY_STATUSES = ("pending", "sending", "sent", "failed")

#: Held at module level so the downgrade can drop the type it created. Note that
#: it is **not** created explicitly in :func:`upgrade`: ``create_table`` emits
#: ``CREATE TYPE`` ahead of ``CREATE TABLE`` for every enum column, so creating it
#: first as well raises "type already exists" on the very first upgrade — the
#: same note ``create_notification_tables`` and ``create_reports_table`` carry.
email_delivery_status_enum = sa.Enum(
    *EMAIL_DELIVERY_STATUSES, name="email_delivery_status"
)


def upgrade() -> None:
    # --- The email channel on the existing preference rows ------------------ #
    op.add_column(
        "notification_preferences",
        sa.Column("email", sa.Boolean(), server_default="true", nullable=False),
    )

    # --- Delivery records ---------------------------------------------------- #
    op.create_table(
        "email_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("rule_key", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("template", sa.String(length=100), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column(
            "status", email_delivery_status_enum, server_default="pending", nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_deliveries")),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_email_deliveries_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.id"],
            name=op.f("fk_email_deliveries_recipient_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("notification_id", name="uq_email_deliveries_notification"),
    )

    op.create_index(
        op.f("ix_email_deliveries_recipient_id"),
        "email_deliveries",
        ["recipient_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_deliveries_status"), "email_deliveries", ["status"], unique=False
    )
    op.create_index(
        "ix_email_deliveries_status_next_attempt_at",
        "email_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_email_deliveries_recipient_created_at",
        "email_deliveries",
        ["recipient_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_email_deliveries_created_at_status",
        "email_deliveries",
        ["created_at", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_email_deliveries_created_at_status", table_name="email_deliveries")
    op.drop_index("ix_email_deliveries_recipient_created_at", table_name="email_deliveries")
    op.drop_index("ix_email_deliveries_status_next_attempt_at", table_name="email_deliveries")
    op.drop_index(op.f("ix_email_deliveries_status"), table_name="email_deliveries")
    op.drop_index(op.f("ix_email_deliveries_recipient_id"), table_name="email_deliveries")
    op.drop_table("email_deliveries")

    op.drop_column("notification_preferences", "email")

    # Dropped explicitly: `drop_table` does not remove the enum type it caused to
    # be created, so a re-upgrade would fail with "type already exists". The
    # `checkfirst` guard keeps the downgrade idempotent on a database where it was
    # already removed by hand.
    bind = op.get_bind()
    email_delivery_status_enum.drop(bind, checkfirst=True)
