"""expand users for user management

Grows the ``users`` table from the identity-only shape Authentication needed into
the complete User entity the User Management spec defines.

Three structural changes, each with a data-preserving backfill:

* ``full_name`` is split into ``first_name`` / ``last_name``. The platform
  searches and sorts on the parts independently, and the combined display name is
  derived on the model rather than stored, so the two cannot disagree. Existing
  rows are split on the first space; a single-word name keeps the whole value as
  the first name (never NULL, since both columns end up NOT NULL).
* ``is_active`` becomes ``status`` (active / inactive / suspended). A boolean
  cannot express "suspended", and having both would allow a row where the two
  disagree about whether sign-in is permitted. ``User.is_active`` survives as a
  derived property, so authentication is unaffected.
* ``phone``, ``profile_image``, ``must_change_password``, ``created_by``, and
  ``updated_by`` are added. The audit columns are self-referential and nullable:
  accounts provisioned by ``scripts/create_user.py`` have no acting user.

The downgrade is a true inverse, including dropping the new PostgreSQL enum type.

Revision ID: c41d7b8e5a92
Revises: 6f3ebd7e2669
Create Date: 2026-07-27 10:40:12.114508

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c41d7b8e5a92"
down_revision: str | None = "6f3ebd7e2669"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Account lifecycle enum, created and dropped explicitly so both directions are
#: symmetric (dropping a column does not drop the PostgreSQL type).
user_status_enum = sa.Enum("active", "inactive", "suspended", name="user_status")


def upgrade() -> None:
    bind = op.get_bind()

    # --- names ------------------------------------------------------------ #
    op.add_column("users", sa.Column("first_name", sa.String(length=100), nullable=True))
    op.add_column("users", sa.Column("last_name", sa.String(length=100), nullable=True))

    # split_part returns '' rather than NULL when there is no second word, so a
    # single-word name yields an empty last name instead of violating NOT NULL.
    op.execute(
        """
        UPDATE users
           SET first_name = split_part(full_name, ' ', 1),
               last_name  = COALESCE(
                   NULLIF(substr(full_name, strpos(full_name, ' ') + 1), full_name),
                   ''
               )
        """
    )
    op.alter_column("users", "first_name", nullable=False)
    op.alter_column("users", "last_name", nullable=False)
    op.drop_column("users", "full_name")

    # --- status ----------------------------------------------------------- #
    user_status_enum.create(bind, checkfirst=True)
    op.add_column(
        "users",
        sa.Column("status", user_status_enum, server_default="active", nullable=False),
    )
    op.execute("UPDATE users SET status = 'inactive' WHERE is_active IS FALSE")
    op.drop_column("users", "is_active")

    # --- profile, password policy, audit ---------------------------------- #
    op.add_column("users", sa.Column("phone", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("profile_image", sa.String(length=512), nullable=True))
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("users", sa.Column("created_by", sa.Uuid(), nullable=True))
    op.add_column("users", sa.Column("updated_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_users_created_by_users"),
        "users",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_users_updated_by_users"),
        "users",
        "users",
        ["updated_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_constraint(op.f("fk_users_updated_by_users"), "users", type_="foreignkey")
    op.drop_constraint(op.f("fk_users_created_by_users"), "users", type_="foreignkey")
    op.drop_column("users", "updated_by")
    op.drop_column("users", "created_by")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "profile_image")
    op.drop_column("users", "phone")

    # A suspended account is disabled, so it collapses to is_active = false.
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.execute("UPDATE users SET is_active = (status = 'active')")
    op.drop_column("users", "status")
    user_status_enum.drop(bind, checkfirst=True)

    op.add_column("users", sa.Column("full_name", sa.String(length=200), nullable=True))
    op.execute("UPDATE users SET full_name = btrim(first_name || ' ' || last_name)")
    op.alter_column("users", "full_name", nullable=False)
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")
