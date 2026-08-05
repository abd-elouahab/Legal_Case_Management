"""create ocr_results and ocr_pages tables

Adds the OCR Processing storage layer from ``09-ocr-processing.md``: the record
of each extraction run, and the machine-readable text it produced.

Notes on the choices this migration encodes:

* **``ocr_status`` is a PostgreSQL enum**, like ``case_status``,
  ``case_priority``, ``document_category``, and ``user_role`` — and unlike
  ``timeline_events.event_type``. The distinction is the one the timeline
  migration drew: a closed, small vocabulary that the platform itself defines
  gets an enum, whereas an open registry future modules extend does not. The four
  OCR states are the whole lifecycle and will not grow by accretion, so the
  database is the right place to enforce them. The downgrade therefore has to
  drop the type as well as the tables, or a re-upgrade fails with
  "type already exists" — the same trap the case and document migrations
  documented.
* **``uq_ocr_results_document_id_document_version`` is the idempotency
  guarantee.** ``09-ocr-processing.md`` requires that retrying OCR for the same
  document version must not create duplicate records. The service checks first,
  but check-then-insert has a race in the middle; the constraint is what closes
  it. It is also what makes
  :meth:`~repositories.ocr.OcrRepository.claim` — a conditional ``UPDATE`` on a
  single row — a sufficient concurrency control.
* **``uq_ocr_pages_ocr_result_id_page_number``** does the same job one level
  down: a retry replaces a run's pages wholesale, and without this a partially
  written attempt could leave two rows both claiming to be page 3, turning
  "preserve page order" into "preserve some order".
* **Both foreign keys are ``ON DELETE CASCADE``.** ``document_id`` for the same
  reason as ``documents.case_id``: the column is NOT NULL so it cannot be nulled,
  and documents are soft-deleted rather than removed, so this only guards against
  manual cleanup. ``ocr_result_id`` because a page without its run is not a
  partial record, it is unreachable data. ``requested_by`` is ``SET NULL``,
  matching every other user reference on the platform — losing who asked for a
  run must never cost the run itself.
* **``text`` is unbounded ``Text``, not a capped ``String``.** A dense page of
  Arabic or French legal prose is several thousand characters, and truncating it
  would corrupt the very artefact a later indexing feature will consume. The
  ceiling that does exist (``MAX_PAGE_TEXT_CHARS``) is applied in
  :mod:`core.ocr`, where it can be a guard against pathological engine output
  rather than a storage limit.
* **``ix_ocr_results_status_created_at``** is the index the monitoring endpoint
  and the startup requeue both read: one aggregates by status over a window, the
  other looks for the oldest pending work. Two single-column indexes could not
  serve either as well.

Revision ID: d5b91c37ea48
Revises: a3c8f5e70b14
Create Date: 2026-08-05 10:15:41.902118

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5b91c37ea48"
down_revision: str | None = "a3c8f5e70b14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The lifecycle states, in the order ``models.ocr.OcrStatus`` declares them.
OCR_STATUSES = ("pending", "processing", "completed", "failed")

#: Held at module level so the downgrade can drop the type it created. Note that
#: it is **not** created explicitly in :func:`upgrade`: ``create_table`` emits
#: ``CREATE TYPE`` ahead of ``CREATE TABLE`` for every enum column, so creating it
#: first as well raises "type ocr_status already exists" on the very first
#: upgrade. Exactly the shape the case and document migrations use.
ocr_status_enum = sa.Enum(*OCR_STATUSES, name="ocr_status")


def upgrade() -> None:
    # The enum type is created by `create_table` itself — see the note above.
    op.create_table(
        "ocr_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("status", ocr_status_enum, server_default="pending", nullable=False),
        sa.Column("engine", sa.String(length=50), nullable=True),
        sa.Column("engine_version", sa.String(length=50), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("detected_language", sa.String(length=50), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ocr_results")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_ocr_results_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_ocr_results_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "document_id",
            "document_version",
            name="uq_ocr_results_document_id_document_version",
        ),
    )

    op.create_index(op.f("ix_ocr_results_document_id"), "ocr_results", ["document_id"], unique=False)
    op.create_index(op.f("ix_ocr_results_status"), "ocr_results", ["status"], unique=False)
    op.create_index(
        "ix_ocr_results_status_created_at", "ocr_results", ["status", "created_at"], unique=False
    )

    op.create_table(
        "ocr_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ocr_result_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ocr_pages")),
        sa.ForeignKeyConstraint(
            ["ocr_result_id"],
            ["ocr_results.id"],
            name=op.f("fk_ocr_pages_ocr_result_id_ocr_results"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "ocr_result_id", "page_number", name="uq_ocr_pages_ocr_result_id_page_number"
        ),
    )

    op.create_index(
        op.f("ix_ocr_pages_ocr_result_id"), "ocr_pages", ["ocr_result_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ocr_pages_ocr_result_id"), table_name="ocr_pages")
    op.drop_table("ocr_pages")

    op.drop_index("ix_ocr_results_status_created_at", table_name="ocr_results")
    op.drop_index(op.f("ix_ocr_results_status"), table_name="ocr_results")
    op.drop_index(op.f("ix_ocr_results_document_id"), table_name="ocr_results")
    op.drop_table("ocr_results")

    # Dropped explicitly: PostgreSQL keeps the type after its last column is
    # gone, and leaving it behind makes a re-upgrade fail with "type already
    # exists" — a true inverse has to remove it.
    ocr_status_enum.drop(op.get_bind(), checkfirst=True)
