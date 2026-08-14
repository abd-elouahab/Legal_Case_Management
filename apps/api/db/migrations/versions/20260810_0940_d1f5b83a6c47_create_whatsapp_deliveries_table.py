"""create whatsapp deliveries table and the whatsapp preference channel

Adds the storage layer from ``18-whatsapp-delivery-channel.md``: one row per
notification the platform tried to deliver over WhatsApp, and one column per
person per preference saying whether they want that channel at all.

Notes on the choices this migration encodes:

* **The preference change is one column, exactly as promised — twice now.**
  ``16-notifications.md`` shipped ``notification_preferences`` as one row per
  ``(user, preference_key)`` with an ``in_app`` boolean, and recorded that *"a
  second delivery channel is one boolean column beside it, and every row already
  exists to receive it"*. ``17-email-delivery-channel.md`` cashed that prediction
  once; this cashes it a second time. There is **no new table, no new key, and no
  backfill**: an account that has never opened the settings page still has no row
  and still follows the platform defaults, so the default reaches every untouched
  account for free.
* **``NOT NULL DEFAULT true`` rather than nullable**, for the reason ``email`` is:
  a nullable column would make "has not chosen" and "chose on" two stored states
  for one behaviour, and the platform already represents "has not chosen"
  perfectly well as *no row*. Note that on this channel the default is narrowed
  twice by things outside the column — the rule set, and whether the account has a
  phone number at all — so "true" means "yes, if there is anything to send you and
  somewhere to send it".
* **No prose is stored on a delivery either.** There is no ``body`` and no
  ``parameters`` column, for the reason ``notifications`` has no ``title`` and
  ``email_deliveries`` has no ``subject``: the wording is rendered per attempt from
  ``core/notifications.py`` and the descriptors in ``apps/api/whatsapp/``, and a
  column holding the contents of a message is exactly what the spec's Logging
  section forbids putting in a log — a table is not a better place for it.
* **``recipient_phone`` is a snapshot, not a join**, and it is stored in E.164
  digits rather than as typed. ``timeline_events`` snapshots its actor for the same
  reason: a join renders the number the account has *today*, so a user who changed
  their phone would silently rewrite the record of where messages were actually
  sent — the first question of any delivery investigation. Storing the normalized
  form rather than the raw one is what makes two rows for the same person
  comparable, since ``users.phone`` is a free-text display field.
* **``provider_message_id`` is new relative to ``email_deliveries``**, and it earns
  its column: the ``wamid`` Meta returns is the only handle that correlates a row
  with anything on the provider's side — a support case, a Business Manager log,
  and the delivery-receipt webhook a later feature would consume. It is an opaque
  identifier rather than content, so keeping it breaks none of this feature's rules
  about what may be stored. SMTP has no equivalent, which is why the email table
  has no such column.
* **``uq_whatsapp_deliveries_notification`` is the duplicate guard**, and it is an
  invariant rather than a heuristic: one notification is one message, whatever
  re-dispatches it. Retrying re-uses the row — the same shape ``ocr_results``,
  ``document_indexes``, and ``email_deliveries`` use — so scheduling the same work
  twice cannot produce two messages. That matters more on this channel than on any
  other: two phone alerts about the same hearing leave a reader unable to tell
  which one is current.
* **One database enum and one ``VARCHAR``.** ``whatsapp_delivery_status`` is
  closed, four-valued, and platform-defined, so it is an enum like
  ``email_delivery_status``; ``error_code`` is an open registry that grows as
  providers are added, so it is a ``VARCHAR`` like ``timeline_events.event_type`` —
  an ``ALTER TYPE`` per new provider failure is precisely the redesign the spec
  asks to avoid. The downgrade drops the type as well as the table, or a re-upgrade
  fails with "type already exists" — the trap every enum migration here documents.
* **Three indexes, and each is a query.** ``(status, next_attempt_at)`` is the
  retry sweeper: everything queued and due. ``(recipient_id, created_at)`` is "why
  did this person not get the message?", which is what the spec's troubleshooting
  requirement is actually about. ``(created_at, status)`` is the monitoring window,
  and the only one that does not start from a person.
* **``ON DELETE CASCADE`` on both foreign keys.** A delivery describes an attempt
  to send one specific notification to one specific person; without either it is a
  record of nothing that anything could render, explain, or clean up.

Revision ID: d1f5b83a6c47
Revises: c7e4a91d3f28
Create Date: 2026-08-10 09:40:12.551903

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f5b83a6c47"
down_revision: str | Sequence[str] | None = "c7e4a91d3f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The closed vocabulary, in the order its Python enum declares it.
#:
#: ``delivered`` rather than ``sent``, matching the spec's "Delivery Status" list.
#: What it honestly means is "the provider accepted it and issued an identifier" —
#: see ``models/whatsapp.py`` for why the platform does not currently claim more.
WHATSAPP_DELIVERY_STATUSES = ("pending", "sending", "delivered", "failed")

#: Held at module level so the downgrade can drop the type it created. Note that
#: it is **not** created explicitly in :func:`upgrade`: ``create_table`` emits
#: ``CREATE TYPE`` ahead of ``CREATE TABLE`` for every enum column, so creating it
#: first as well raises "type already exists" on the very first upgrade — the same
#: note ``create_email_deliveries_table`` and ``create_reports_table`` carry.
whatsapp_delivery_status_enum = sa.Enum(
    *WHATSAPP_DELIVERY_STATUSES, name="whatsapp_delivery_status"
)


def upgrade() -> None:
    # --- The WhatsApp channel on the existing preference rows ---------------- #
    op.add_column(
        "notification_preferences",
        sa.Column("whatsapp", sa.Boolean(), server_default="true", nullable=False),
    )

    # --- Delivery records ---------------------------------------------------- #
    op.create_table(
        "whatsapp_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_phone", sa.String(length=32), nullable=False),
        sa.Column("rule_key", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("template", sa.String(length=100), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column(
            "status",
            whatsapp_delivery_status_enum,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_whatsapp_deliveries")),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_whatsapp_deliveries_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.id"],
            name=op.f("fk_whatsapp_deliveries_recipient_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "notification_id", name="uq_whatsapp_deliveries_notification"
        ),
    )

    op.create_index(
        op.f("ix_whatsapp_deliveries_recipient_id"),
        "whatsapp_deliveries",
        ["recipient_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_whatsapp_deliveries_status"),
        "whatsapp_deliveries",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_deliveries_status_next_attempt_at",
        "whatsapp_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_deliveries_recipient_created_at",
        "whatsapp_deliveries",
        ["recipient_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_deliveries_created_at_status",
        "whatsapp_deliveries",
        ["created_at", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_deliveries_created_at_status", table_name="whatsapp_deliveries"
    )
    op.drop_index(
        "ix_whatsapp_deliveries_recipient_created_at", table_name="whatsapp_deliveries"
    )
    op.drop_index(
        "ix_whatsapp_deliveries_status_next_attempt_at", table_name="whatsapp_deliveries"
    )
    op.drop_index(
        op.f("ix_whatsapp_deliveries_status"), table_name="whatsapp_deliveries"
    )
    op.drop_index(
        op.f("ix_whatsapp_deliveries_recipient_id"), table_name="whatsapp_deliveries"
    )
    op.drop_table("whatsapp_deliveries")

    op.drop_column("notification_preferences", "whatsapp")

    # Dropped explicitly: `drop_table` does not remove the enum type it caused to
    # be created, so a re-upgrade would fail with "type already exists". The
    # `checkfirst` guard keeps the downgrade idempotent on a database where it was
    # already removed by hand.
    bind = op.get_bind()
    whatsapp_delivery_status_enum.drop(bind, checkfirst=True)
