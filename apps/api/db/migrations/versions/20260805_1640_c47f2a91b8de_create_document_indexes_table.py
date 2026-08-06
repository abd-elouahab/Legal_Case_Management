"""create document_indexes table

Adds the Document Indexing storage layer from ``10-document-indexing.md``: the
record of each indexing run, what it produced, and with which model.

Notes on the choices this migration encodes:

* **One table, not two.** OCR needed ``ocr_results`` *and* ``ocr_pages`` because
  the text it produces is business data with nowhere else to live. Indexing needs
  one: the chunks and their embeddings live in **Qdrant**, which is where
  `architecture.md` and `code-standards.md` both say document embeddings belong.
  Duplicating them into PostgreSQL would create two stores that can disagree
  about what is indexed, and this table would then be a cache with no
  invalidation. What is kept here is the *run* — the part a lawyer polls, an
  operator monitors, and a rebuild re-uses.
* **``index_status`` is a PostgreSQL enum**, like ``ocr_status``,
  ``case_status``, ``case_priority``, ``document_category``, and ``user_role`` —
  and unlike ``timeline_events.event_type``. The distinction is the one the
  timeline migration drew: a closed, small vocabulary the platform itself defines
  gets an enum, whereas an open registry future modules extend does not. The four
  indexing states are the whole lifecycle and will not grow by accretion. The
  downgrade therefore has to drop the type as well as the table, or a re-upgrade
  fails with "type already exists" — the trap the case, document, and OCR
  migrations each documented, and which the OCR migration hit for real because
  the SQLite test database has no ``CREATE TYPE`` to fail on.
* **``uq_document_indexes_document_id_document_version`` is the idempotency
  guarantee.** ``10-document-indexing.md`` requires that re-indexing be
  idempotent and produce no duplicate records. The service checks first, but
  check-then-insert has a race in the middle; the constraint is what closes it.
  It is also what makes
  :meth:`~repositories.indexing.IndexingRepository.claim` — a conditional
  ``UPDATE`` on a single row — a sufficient concurrency control.
* **``case_id`` is stored, not joined.** Every vector written to Qdrant carries
  its case in the payload so a future search can filter by it, and that value has
  to be readable by a background worker without a join through ``documents``.
  Copying it makes the vector payload and the index row provably agree; a
  document never changes case, so the copy cannot go stale. It is also what lets
  the list endpoint's case scope be one subquery rather than two.
* **All three foreign keys mirror the OCR table's.** ``document_id`` and
  ``case_id`` are ``ON DELETE CASCADE`` — both columns are NOT NULL so neither
  can be nulled, and both parents are soft-deleted rather than removed, so this
  only guards against manual cleanup. ``requested_by`` is ``SET NULL``, matching
  every other user reference on the platform: losing who asked for a rebuild must
  never cost the record of the rebuild.
* **``ix_document_indexes_status_created_at``** is the index the monitoring
  endpoint and the startup requeue both read: one aggregates by status over a
  window, the other looks for the oldest pending work.
* **No index on ``embedding_model``**, deliberately, even though the list
  endpoint filters by it. That filter exists for one operation — finding the
  documents still built with a superseded model — which is run rarely and
  combines with a status filter that is indexed. An index carrying a low-
  cardinality string on every write is the wrong trade for it.

Revision ID: c47f2a91b8de
Revises: d5b91c37ea48
Create Date: 2026-08-05 16:40:12.774510

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c47f2a91b8de"
down_revision: str | None = "d5b91c37ea48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The lifecycle states, in the order ``models.indexing.IndexStatus`` declares
#: them.
INDEX_STATUSES = ("pending", "indexing", "indexed", "failed")

#: Held at module level so the downgrade can drop the type it created. Note that
#: it is **not** created explicitly in :func:`upgrade`: ``create_table`` emits
#: ``CREATE TYPE`` ahead of ``CREATE TABLE`` for every enum column, so creating it
#: first as well raises "type index_status already exists" on the very first
#: upgrade. Exactly the shape the case, document, and OCR migrations use.
index_status_enum = sa.Enum(*INDEX_STATUSES, name="index_status")


def upgrade() -> None:
    # The enum type is created by `create_table` itself — see the note above.
    op.create_table(
        "document_indexes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("status", index_status_enum, server_default="pending", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("character_count", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("vector_collection", sa.String(length=100), nullable=True),
        sa.Column("chunk_size", sa.Integer(), nullable=True),
        sa.Column("chunk_overlap", sa.Integer(), nullable=True),
        sa.Column("detected_language", sa.String(length=20), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_indexes")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_indexes_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            name=op.f("fk_document_indexes_case_id_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_document_indexes_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "document_id",
            "document_version",
            name="uq_document_indexes_document_id_document_version",
        ),
    )

    op.create_index(
        op.f("ix_document_indexes_document_id"), "document_indexes", ["document_id"], unique=False
    )
    op.create_index(
        op.f("ix_document_indexes_case_id"), "document_indexes", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_document_indexes_status"), "document_indexes", ["status"], unique=False
    )
    op.create_index(
        "ix_document_indexes_status_created_at",
        "document_indexes",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_indexes_status_created_at", table_name="document_indexes")
    op.drop_index(op.f("ix_document_indexes_status"), table_name="document_indexes")
    op.drop_index(op.f("ix_document_indexes_case_id"), table_name="document_indexes")
    op.drop_index(op.f("ix_document_indexes_document_id"), table_name="document_indexes")
    op.drop_table("document_indexes")

    # Dropped explicitly: PostgreSQL keeps the type after its last column is
    # gone, and leaving it behind makes a re-upgrade fail with "type already
    # exists" — a true inverse has to remove it. This is the fault the OCR
    # migration shipped with and the live run caught; the SQLite test database
    # has no CREATE TYPE at all, so no test can catch it.
    index_status_enum.drop(op.get_bind(), checkfirst=True)
