"""add session generation to users

Adds the counter used to revoke every session for a user in a single write: each
token embeds the generation it was issued under, and a token whose generation is
behind the user's is rejected. A password change increments it.

Existing rows default to 0, which is also the generation read from tokens that
predate the claim — so applying this migration does not sign anyone out.

Revision ID: 6f3ebd7e2669
Revises: 9a0f33933f6d
Create Date: 2026-07-25 15:12:29.560905

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "6f3ebd7e2669"
down_revision: str | None = "9a0f33933f6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("session_generation", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("users", "session_generation")
