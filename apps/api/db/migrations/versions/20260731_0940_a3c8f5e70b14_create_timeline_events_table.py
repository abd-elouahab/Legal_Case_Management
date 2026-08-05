"""create timeline_events table

Adds the Timeline & Audit Trail storage layer from ``08-timeline.md``: one
append-only row per significant thing that happened to a case.

Notes on the choices this migration encodes:

* **No enum types.** ``event_type`` and ``actor_role`` are plain ``VARCHAR``,
  unlike ``case_status``, ``document_category``, and ``user_role``. Two different
  reasons, both about this table specifically. ``event_type``: the spec requires
  that *"future modules should be able to publish events without modifying the
  Timeline implementation"*, and an ``ALTER TYPE`` per new event type is exactly
  the modification it rules out. ``actor_role``: the column is a **snapshot**, and
  a role retired from ``UserRole`` in five years must still read back from a row
  written today — which a column constrained to the current enum could not do.
  The consequence is that this migration creates no type and its downgrade drops
  none.
* **``metadata`` is ``JSONB`` on PostgreSQL** (plain ``JSON`` on other dialects,
  which is what the SQLite test database uses). JSONB stores parsed and is
  indexable, so a later "find every event mentioning this document" query is
  possible without a schema change. ``NOT NULL DEFAULT '{}'`` so an absent value
  and an empty object are the same thing and no reader has to handle both.
* **No ``updated_at`` and no ``deleted_at``.** The table is append-only: nothing
  in the platform updates or deletes a timeline event, so either column would be
  one that can never change. Their absence is what makes that structural rather
  than a convention.
* ``case_id`` is ``ON DELETE CASCADE`` for the same reason as
  ``documents.case_id``: the column is NOT NULL so it cannot be nulled, and cases
  are archived rather than deleted, so this only guards against manual cleanup.
* ``actor_id`` is ``ON DELETE SET NULL``, matching the rest of the platform —
  and losing the *identifier* costs nothing here, because ``actor_name`` and
  ``actor_role`` are snapshots that survive the account.
* The composite ``(case_id, created_at)`` index is the one the case timeline
  actually uses: every read filters by case and orders by time. The single-column
  indexes serve the filters (``event_type``, ``actor_id``) and the single-event
  lookup.

Revision ID: a3c8f5e70b14
Revises: e2f8a4c19d57
Create Date: 2026-07-31 09:40:22.118453

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a3c8f5e70b14"
down_revision: str | None = "e2f8a4c19d57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Mirrors ``models.timeline._METADATA_TYPE`` so the migration and the model
#: cannot disagree about the column's storage.
metadata_type = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "timeline_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("actor_role", sa.String(length=50), nullable=True),
        sa.Column("metadata", metadata_type, server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_timeline_events")),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            name=op.f("fk_timeline_events_case_id_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_timeline_events_actor_id_users"),
            ondelete="SET NULL",
        ),
    )

    op.create_index(op.f("ix_timeline_events_case_id"), "timeline_events", ["case_id"], unique=False)
    op.create_index(
        op.f("ix_timeline_events_event_type"), "timeline_events", ["event_type"], unique=False
    )
    op.create_index(
        op.f("ix_timeline_events_actor_id"), "timeline_events", ["actor_id"], unique=False
    )
    op.create_index(
        op.f("ix_timeline_events_created_at"), "timeline_events", ["created_at"], unique=False
    )
    op.create_index(
        "ix_timeline_events_case_id_created_at",
        "timeline_events",
        ["case_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_timeline_events_case_id_created_at", table_name="timeline_events")
    op.drop_index(op.f("ix_timeline_events_created_at"), table_name="timeline_events")
    op.drop_index(op.f("ix_timeline_events_actor_id"), table_name="timeline_events")
    op.drop_index(op.f("ix_timeline_events_event_type"), table_name="timeline_events")
    op.drop_index(op.f("ix_timeline_events_case_id"), table_name="timeline_events")
    op.drop_table("timeline_events")
    # No enum types to drop: this table deliberately uses none — see the module
    # docstring.
