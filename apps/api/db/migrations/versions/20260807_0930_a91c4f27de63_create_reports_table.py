"""create reports table

Adds the AI report generation storage layer from ``14-ai-report-agent.md``: one
row per generated report, carrying its lifecycle, its content, its citations, and
the provenance an evaluation needs. ``architecture.md`` has listed *Reports*
under PostgreSQL since the storage model was written; this migration is that
listing.

Notes on the choices this migration encodes:

* **One table, not two.** A ``report_sections`` table was the obvious
  alternative and was rejected for the reason :mod:`models.indexing` gives for
  its own single table: a section is never read, filtered, paged, or referenced
  on its own. It is read with the report, exported with the report, and cleared
  when the report is regenerated. A second table would buy a join on every read
  and a second place for the section order to live, in exchange for a
  normalisation nothing queries against.
* **``sections`` and ``citations`` are JSON**, with the same
  ``JSONB``-on-PostgreSQL variant ``timeline_events.metadata`` and
  ``conversation_messages.citations`` use. A citation is the *RAG pipeline's*
  output; a normalized table here would be a second definition of a citation that
  this feature would then own and have to keep in step. Nothing queries inside
  either column.
* **``requested_by`` is ``ON DELETE CASCADE``**, matching
  ``conversations.owner_id`` and unlike every other user reference on the
  platform. The rest are audit trails that must outlive the account; a report is
  the account's private work product, and an owner-less row would be data no
  ``requested_by = :caller`` query can ever return while still containing a
  generated interpretation of a client's file.
* **``case_id`` is ``CASCADE`` and ``conversation_id`` is ``SET NULL``.** A
  report without its case is unreachable *and* unauthorizable — its access is
  decided by the case — while a conversation link is provenance, and losing the
  conversation must not cost the report.
* **Two PostgreSQL enums** — ``report_type`` and ``report_status`` — like
  ``ocr_status``, ``index_status``, and ``conversation_status``, and unlike
  ``timeline_events.event_type``: closed, small vocabularies the platform itself
  defines, not open registries future modules extend. A sixth report type is
  therefore a migration, and deliberately so: a type that arrived without one
  would have no template, no titles, and nothing able to render it. The downgrade
  drops the types as well as the tables, or a re-upgrade fails with "type already
  exists" — the trap every enum migration here documents.
* **Three composite indexes, and each is a query.**
  ``(requested_by, status, created_at)`` is the history page, whole — every read
  of this table starts from the requester. ``(case_id, created_at)`` is the case
  workspace's report list. ``(status, created_at)`` is the startup requeue and
  the monitoring window.
* **No index on ``deleted_at``**, deliberately, even though every read filters on
  it. It is a two-valued column on a table already narrowed to one requester, so
  the composite index above has done the work before it is evaluated — the same
  trade ``documents.deleted_at`` and ``conversations.deleted_at`` make.
* **``sections_total`` is nullable and ``sections_completed`` is not.** Before a
  run is planned the platform genuinely does not know how many sections there
  will be, and ``0`` would read as "this report is empty"; how many have been
  written, on the other hand, is always exactly known and starts at zero.

Revision ID: a91c4f27de63
Revises: f2a76c40d91b
Create Date: 2026-08-07 09:30:12.884201

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a91c4f27de63"
down_revision: str | None = "f2a76c40d91b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The vocabularies, in the order their Python enums declare them.
REPORT_TYPES = (
    "case_summary",
    "hearing_preparation",
    "evidence_summary",
    "chronological_timeline",
    "executive_summary",
)
REPORT_STATUSES = ("pending", "processing", "completed", "failed")

#: Held at module level so the downgrade can drop the types it created. Note that
#: neither is created explicitly in :func:`upgrade`: ``create_table`` emits
#: ``CREATE TYPE`` ahead of ``CREATE TABLE`` for every enum column, so creating
#: them first as well raises "type already exists" on the very first upgrade.
report_type_enum = sa.Enum(*REPORT_TYPES, name="report_type")
report_status_enum = sa.Enum(*REPORT_STATUSES, name="report_status")

#: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else — the variant
#: ``timeline_events.metadata`` established, and the reason is the same: JSONB
#: stores parsed and is indexable, and the SQLite test database has no JSONB.
JSON_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # The enum types are created by `create_table` itself — see the note above.
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("report_type", report_type_enum, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("status", report_status_enum, server_default="pending", nullable=False),
        sa.Column("template_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("sections_total", sa.Integer(), nullable=True),
        sa.Column("sections_completed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sections", JSON_TYPE, server_default="[]", nullable=False),
        sa.Column("citations", JSON_TYPE, server_default="[]", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retrieved_count", sa.Integer(), nullable=True),
        sa.Column("context_count", sa.Integer(), nullable=True),
        sa.Column("grounded_sections", sa.Integer(), nullable=True),
        sa.Column("character_count", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_name", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("export_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            name=op.f("fk_reports_case_id_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_reports_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_reports_requested_by_users"),
            ondelete="CASCADE",
        ),
    )

    op.create_index(op.f("ix_reports_case_id"), "reports", ["case_id"], unique=False)
    op.create_index(op.f("ix_reports_report_type"), "reports", ["report_type"], unique=False)
    op.create_index(op.f("ix_reports_status"), "reports", ["status"], unique=False)
    op.create_index(op.f("ix_reports_requested_by"), "reports", ["requested_by"], unique=False)
    op.create_index(
        "ix_reports_requested_by_status_created_at",
        "reports",
        ["requested_by", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_reports_case_id_created_at", "reports", ["case_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_reports_status_created_at", "reports", ["status", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_reports_status_created_at", table_name="reports")
    op.drop_index("ix_reports_case_id_created_at", table_name="reports")
    op.drop_index("ix_reports_requested_by_status_created_at", table_name="reports")
    op.drop_index(op.f("ix_reports_requested_by"), table_name="reports")
    op.drop_index(op.f("ix_reports_status"), table_name="reports")
    op.drop_index(op.f("ix_reports_report_type"), table_name="reports")
    op.drop_index(op.f("ix_reports_case_id"), table_name="reports")
    op.drop_table("reports")

    # Dropped explicitly: `drop_table` does not remove the enum types it caused
    # to be created, so a re-upgrade would fail with "type already exists". The
    # `checkfirst` guard keeps the downgrade idempotent on a database where they
    # were already removed by hand.
    bind = op.get_bind()
    report_status_enum.drop(bind, checkfirst=True)
    report_type_enum.drop(bind, checkfirst=True)
