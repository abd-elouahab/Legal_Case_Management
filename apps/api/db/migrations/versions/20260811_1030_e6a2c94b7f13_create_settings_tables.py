"""create settings tables

The Settings module's storage (spec ``20-settings.md``), and one column on
``users``.

**Two tables rather than one**, and the separation is the spec's *"administrator
settings should remain isolated from regular user settings"* answered in the
schema instead of in a ``WHERE`` clause. ``user_settings`` has a ``user_id`` and
``platform_settings`` has none, so there is no query that can serve a platform
value as somebody's preference and no partial unique index needed to keep the two
vocabularies apart.

**One row per ``(user, key)`` rather than a column per setting**, which is the
shape ``notification_preferences`` established and has now been extended twice
without a migration. It is what makes the spec's *"support future sections
without redesign"* concrete: a tenth setting is a registry entry in
``core/settings.py`` and nothing here changes. The cost is that the value is
JSON rather than a typed column, which is stated rather than hidden — the type
discipline lives in :func:`~core.settings.validate_setting`, applied to every
write before anything is persisted.

**No index beyond the two unique constraints, deliberately.** Every read of
``user_settings`` is *"this person's settings"*, which the
``(user_id, setting_key)`` constraint already serves, and every read of
``platform_settings`` is the whole table — nine rows at most. An index chosen for
a query nobody makes is a write cost with no reader.

``users.job_title`` is nullable with no default and no backfill: it is optional
free text nothing authorizes against, so every existing account correctly starts
without one.

Revision ID: e6a2c94b7f13
Revises: d1f5b83a6c47
Create Date: 2026-08-11 10:30:24.118207

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e6a2c94b7f13"
down_revision: str | Sequence[str] | None = "d1f5b83a6c47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ``JSONB`` on PostgreSQL, plain ``JSON`` elsewhere — the same variant every
#: other JSON column on this platform uses, so the SQLite test database can run
#: the same migration.
_JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # --- Profile: the one field the spec names that nothing had ------------- #
    op.add_column("users", sa.Column("job_title", sa.String(length=120), nullable=True))

    # --- One person's own preferences --------------------------------------- #
    op.create_table(
        "user_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("setting_key", sa.String(length=50), nullable=False),
        sa.Column("value", _JSON_TYPE, nullable=False),
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
        # CASCADE, matching `notification_preferences`: every read here is
        # `user_id = :caller`, so an owner-less row would be unreachable data
        # nothing can serve or clean up.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # The upsert target. Two settings pages open at once cannot leave one
        # account with two contradictory rows for the same key — the spec's
        # "concurrent updates" requirement, held by the schema.
        sa.UniqueConstraint("user_id", "setting_key", name="uq_user_settings_user_key"),
    )
    op.create_index(op.f("ix_user_settings_user_id"), "user_settings", ["user_id"])

    # --- The deployment's own configuration ---------------------------------- #
    op.create_table(
        "platform_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("setting_key", sa.String(length=50), nullable=False),
        sa.Column("value", _JSON_TYPE, nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
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
        # SET NULL rather than CASCADE: losing the administrator who last changed
        # a setting must never cost the platform its configuration.
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_platform_settings_setting_key"),
        "platform_settings",
        ["setting_key"],
        unique=True,
    )

    # No rows are seeded. "Nothing stored" is the platform's own default
    # everywhere in `core/settings.py`, so seeding the defaults would create rows
    # that say exactly what their absence already says — and would then need a
    # data migration every time a default changed.


def downgrade() -> None:
    op.drop_index(op.f("ix_platform_settings_setting_key"), table_name="platform_settings")
    op.drop_table("platform_settings")

    op.drop_index(op.f("ix_user_settings_user_id"), table_name="user_settings")
    op.drop_table("user_settings")

    op.drop_column("users", "job_title")
