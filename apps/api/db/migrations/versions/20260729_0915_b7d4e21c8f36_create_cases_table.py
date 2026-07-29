"""create cases table

Introduces the platform's central business entity. Every later module
(documents, timeline, hearings, reports, notifications) will reference this
table, so the shape here is deliberately the complete Case entity from
``06-case-management.md`` rather than a minimum that would need widening.

Notes on the choices this migration encodes:

* ``case_number`` carries a unique index. It is the identifier quoted in
  correspondence, and uniqueness is enforced by the database rather than only by
  the service — two concurrent creations can otherwise both pass an application
  check and both commit.
* ``status`` and ``priority`` are PostgreSQL enums. ``create_table`` creates the
  types, but dropping a table does *not* drop them — so the downgrade drops both
  explicitly, which is what makes upgrade → downgrade → upgrade work.
* ``status``, ``priority``, and both assignment columns are indexed: they are the
  filters the case list runs on every request, and the assignment columns
  additionally carry the per-resource authorization scope, so they are read on
  *every* list query a lawyer or court representative makes.
* All four foreign keys into ``users`` use ``ON DELETE SET NULL``. Users are
  soft-deleted, so this only guards against manual cleanup — but losing a case
  because an account row was removed would be catastrophic, and a case with an
  unknown assignee is recoverable while a deleted case is not.

Revision ID: b7d4e21c8f36
Revises: c41d7b8e5a92
Create Date: 2026-07-29 09:15:22.481003

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d4e21c8f36"
down_revision: str | None = "c41d7b8e5a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


case_status_enum = sa.Enum(
    "draft",
    "open",
    "in_progress",
    "waiting_for_hearing",
    "closed",
    "archived",
    name="case_status",
)

case_priority_enum = sa.Enum("low", "medium", "high", "urgent", name="case_priority")


def upgrade() -> None:
    # The enum types are created by `create_table` itself, which emits CREATE
    # TYPE ahead of CREATE TABLE for every enum column. The downgrade has to drop
    # them explicitly, because dropping a table does not drop its types.
    op.create_table(
        "cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_number", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("status", case_status_enum, server_default="draft", nullable=False),
        sa.Column("priority", case_priority_enum, server_default="medium", nullable=False),
        sa.Column("court_name", sa.String(length=255), nullable=True),
        sa.Column("filing_date", sa.Date(), nullable=True),
        sa.Column("next_hearing_date", sa.Date(), nullable=True),
        sa.Column("assigned_lawyer_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_court_representative_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
        sa.ForeignKeyConstraint(
            ["assigned_lawyer_id"],
            ["users.id"],
            name=op.f("fk_cases_assigned_lawyer_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_court_representative_id"],
            ["users.id"],
            name=op.f("fk_cases_assigned_court_representative_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_cases_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_cases_updated_by_users"),
            ondelete="SET NULL",
        ),
    )

    op.create_index(op.f("ix_cases_case_number"), "cases", ["case_number"], unique=True)
    op.create_index(op.f("ix_cases_status"), "cases", ["status"], unique=False)
    op.create_index(op.f("ix_cases_priority"), "cases", ["priority"], unique=False)
    op.create_index(
        op.f("ix_cases_assigned_lawyer_id"), "cases", ["assigned_lawyer_id"], unique=False
    )
    op.create_index(
        op.f("ix_cases_assigned_court_representative_id"),
        "cases",
        ["assigned_court_representative_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(op.f("ix_cases_assigned_court_representative_id"), table_name="cases")
    op.drop_index(op.f("ix_cases_assigned_lawyer_id"), table_name="cases")
    op.drop_index(op.f("ix_cases_priority"), table_name="cases")
    op.drop_index(op.f("ix_cases_status"), table_name="cases")
    op.drop_index(op.f("ix_cases_case_number"), table_name="cases")
    op.drop_table("cases")

    # Dropping the table does not drop the types, so a re-upgrade would fail on
    # "type already exists" without this.
    case_priority_enum.drop(bind, checkfirst=True)
    case_status_enum.drop(bind, checkfirst=True)
