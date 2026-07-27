"""create users table

Creates the ``users`` table backing authentication: login identity (unique
email), bcrypt password hash, role, and the active/disabled flag.

Revision ID: 9a0f33933f6d
Revises:
Create Date: 2026-07-25 13:56:33.595068

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9a0f33933f6d"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Role enum shared by upgrade/downgrade. ``create_type=False`` keeps
#: ``create_table`` from emitting the CREATE TYPE twice; it is created
#: explicitly below so the downgrade can drop it symmetrically.
user_role_enum = sa.Enum("administrator", "lawyer", "court", name="user_role")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("hashed_password", sa.String(length=128), nullable=False),
        sa.Column("role", user_role_enum, nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    # Unique index doubles as the case-insensitive login lookup index (emails are
    # normalized to lowercase before they are stored).
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
    # Dropping the table does not drop the PostgreSQL enum type, so remove it
    # explicitly to keep the downgrade a true inverse of the upgrade.
    user_role_enum.drop(op.get_bind(), checkfirst=True)
