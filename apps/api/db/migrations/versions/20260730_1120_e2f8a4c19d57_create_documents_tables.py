"""create documents and document_versions tables

Adds the Document Management storage layer from ``07-document-management.md``.
Metadata lives here; the binaries live in MinIO, addressed by
``storage_bucket`` + ``storage_key``.

Notes on the choices this migration encodes:

* Two tables, because a document has two lifetimes. ``documents`` is the current
  state of a logical document — the file a user downloads today plus the
  metadata that survives a replacement. ``document_versions`` is one immutable
  row per uploaded file, including the current one, which is what makes
  "preserve previous versions" a property of the schema.
* ``document_versions`` carries a **unique** ``(document_id, version)``
  constraint. The service reads the highest version and writes the next, which
  is a read-then-write; without the constraint two simultaneous replacements
  could both commit the same number and one file would vanish from the history.
* ``category`` is a PostgreSQL enum. ``create_table`` creates the type, but
  dropping a table does *not* drop it — so the downgrade drops it explicitly,
  which is what makes upgrade → downgrade → upgrade work.
* ``documents.case_id`` is ``ON DELETE CASCADE`` rather than ``SET NULL``: a
  document must belong to a case, so the column is NOT NULL and cannot be
  nulled. Cases are archived rather than deleted, so this only guards against
  manual cleanup — but a document row pointing at a case that no longer exists
  would be unreachable by every query in the module.
* Both ``uploaded_by`` columns are ``ON DELETE SET NULL``, matching the rest of
  the platform: users are soft-deleted, and losing a document because an account
  row was removed would be far worse than losing the attribution.
* ``file_size`` is ``BigInteger``. A 4-byte column caps out at 2 GB, which a
  scanned case bundle can legitimately approach.
* The indexed columns are the ones the document list filters and scopes on every
  request: ``case_id``, ``category``, ``file_extension``, ``uploaded_by``,
  ``version``, and ``deleted_at`` (which every non-opt-in query tests for NULL).

Revision ID: e2f8a4c19d57
Revises: b7d4e21c8f36
Create Date: 2026-07-30 11:20:14.902117

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f8a4c19d57"
down_revision: str | None = "b7d4e21c8f36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


document_category_enum = sa.Enum(
    "contract",
    "evidence",
    "court_decision",
    "pleading",
    "correspondence",
    "invoice",
    "identity_document",
    "other",
    name="document_category",
)


def upgrade() -> None:
    # The enum type is created by `create_table` itself, which emits CREATE TYPE
    # ahead of CREATE TABLE for every enum column. The downgrade has to drop it
    # explicitly, because dropping a table does not drop its types.
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_filename", sa.String(length=255), nullable=False),
        sa.Column("file_extension", sa.String(length=20), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_bucket", sa.String(length=63), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("category", document_category_enum, server_default="other", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            name=op.f("fk_documents_case_id_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_documents_uploaded_by_users"),
            ondelete="SET NULL",
        ),
    )

    op.create_index(op.f("ix_documents_case_id"), "documents", ["case_id"], unique=False)
    op.create_index(op.f("ix_documents_category"), "documents", ["category"], unique=False)
    op.create_index(
        op.f("ix_documents_file_extension"), "documents", ["file_extension"], unique=False
    )
    op.create_index(op.f("ix_documents_version"), "documents", ["version"], unique=False)
    op.create_index(op.f("ix_documents_uploaded_by"), "documents", ["uploaded_by"], unique=False)
    op.create_index(op.f("ix_documents_deleted_at"), "documents", ["deleted_at"], unique=False)

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_filename", sa.String(length=255), nullable=False),
        sa.Column("file_extension", sa.String(length=20), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_bucket", sa.String(length=63), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_versions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_document_versions_uploaded_by_users"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "document_id", "version", name="uq_document_versions_document_id_version"
        ),
    )

    op.create_index(
        op.f("ix_document_versions_document_id"), "document_versions", ["document_id"], unique=False
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(op.f("ix_document_versions_document_id"), table_name="document_versions")
    op.drop_table("document_versions")

    op.drop_index(op.f("ix_documents_deleted_at"), table_name="documents")
    op.drop_index(op.f("ix_documents_uploaded_by"), table_name="documents")
    op.drop_index(op.f("ix_documents_version"), table_name="documents")
    op.drop_index(op.f("ix_documents_file_extension"), table_name="documents")
    op.drop_index(op.f("ix_documents_category"), table_name="documents")
    op.drop_index(op.f("ix_documents_case_id"), table_name="documents")
    op.drop_table("documents")

    # Dropping the table does not drop the type, so a re-upgrade would fail on
    # "type already exists" without this.
    document_category_enum.drop(bind, checkfirst=True)
