"""Settings ORM models.

``20-settings.md`` requires that settings **persist** — *"settings should survive
logout, login, browser refresh, and device changes"* — which is what rules out
the obvious cheap answer (``localStorage``) and gives this module two tables.

**Two tables, and the separation is the spec's own requirement made
structural.** :class:`UserSetting` is one person's answer about their own
experience; :class:`PlatformSetting` is an administrator's answer about the
deployment. *"Administrator settings should remain isolated from regular user
settings"*, so they are not one table with a nullable ``user_id`` — a shape that
would make "whose setting is this?" a runtime question, would let one careless
``WHERE`` clause serve a platform value as somebody's preference, and would need
a partial unique index to keep the two vocabularies apart. Two tables answer it
in the schema.

**One row per ``(user, key)``, not a column per setting**, which is the shape
``notification_preferences`` proved twice — once for email, once for WhatsApp —
and the concrete meaning of the spec's *"support future sections without
redesign"*:

* a **tenth setting** is a member of :class:`~core.settings.UserSettingKey` plus a
  descriptor, with **no migration**;
* a **new section** is a group of those, with no migration either;
* an account that has never opened the Settings page has **no rows at all** and
  follows :func:`~core.settings.default_user_value`, so a platform-wide change of
  defaults reaches every untouched account without a backfill.

**The value is JSON, and that is what a registry costs.** A settings table with
one typed column per setting cannot be extended without a migration, which is the
whole thing being avoided; a ``VARCHAR`` holding ``"true"`` would make every
reader parse, and a reader that parsed differently from the writer is a
corrupted preference. JSON carries the boolean as a boolean and the widget list
as a list, and :func:`~core.settings.validate_setting` is what stops anything
else from being written — the type discipline lives in the registry rather than
in the column, deliberately and at a stated cost.

**No setting here is a secret.** ``20-settings.md`` requires that *"sensitive
values remain protected"*, and the strongest form of that is not to hold any: the
password lives in ``users.hashed_password``, credentials live in the environment
(:mod:`core.config`), and nothing in either registry is a token, a key, or an
address. A setting whose value needed encrypting would be a setting that belongs
in the deployment's configuration instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from db.base import Base

#: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else — the same variant
#: :mod:`models.notification`, :mod:`models.timeline`, and :mod:`models.report`
#: use, and for the same two reasons: JSONB stores parsed and is indexable, and
#: the SQLite test database has no JSONB at all.
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class UserSetting(Base):
    """One person's answer to one question about their own experience."""

    __tablename__ = "user_settings"

    __table_args__ = (
        # One opinion per person per setting. The unique constraint *is* the
        # upsert target — see `SettingsRepository.set_user_settings` — so two
        # settings pages open at once cannot leave somebody with two
        # contradictory rows for the same key. That is the spec's "concurrent
        # updates" requirement answered by the schema rather than by a lock.
        UniqueConstraint("user_id", "setting_key", name="uq_user_settings_user_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: Whose setting this is.
    #:
    #: ``CASCADE`` rather than the ``SET NULL`` most user references on this
    #: platform use, matching ``notification_preferences.user_id`` and for the
    #: same reason: every read here is ``user_id = :caller``, so an owner-less row
    #: would be unreachable data that nothing can ever serve or clean up.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Which setting this is, from :class:`~core.settings.UserSettingKey`.
    #:
    #: ``VARCHAR`` because the registry is **open** by design — the same trade
    #: ``timeline_events.event_type`` and ``notification_preferences.
    #: preference_key`` make, and the reason a tenth setting needs no
    #: ``ALTER TYPE``. Read tolerantly by
    #: :func:`~core.settings.user_setting_from_value`, so a key written by a later
    #: version of the platform cannot make an earlier one unable to load
    #: somebody's settings.
    setting_key: Mapped[str] = mapped_column(String(50), nullable=False)

    #: What they chose. Validated against the key's descriptor before it is
    #: written, never after it is read — see the module docstring.
    value: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # The key and the owner. **Never the value**: a repr ends up in whatever
        # log line interpolates the object, and `20-settings.md`'s Logging section
        # asks for *what changed*, not for what somebody chose.
        return f"<UserSetting user_id={self.user_id!s} key={self.setting_key!r}>"


class PlatformSetting(Base):
    """An administrator's answer to one question about the deployment.

    **No ``user_id``, and that absence is the isolation.** A row here belongs to
    the platform; there is no query that can return one as somebody's preference,
    because there is no column to scope it by. Every read requires
    ``settings:view`` and every write requires ``settings:manage``, which no role
    but administrator holds.

    :attr:`updated_by` is the audit trail ``code-standards.md`` requires of a
    sensitive operation, and it is kept **on the row** rather than only in the log
    because "who turned maintenance mode on?" is a question asked days later, from
    a database, by somebody who does not have the application's logs.
    """

    __tablename__ = "platform_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: Which setting this is, from :class:`~core.settings.PlatformSettingKey`.
    #: Unique on its own — there is exactly one platform, so there is exactly one
    #: answer per key. ``VARCHAR`` for the reason :attr:`UserSetting.setting_key`
    #: is one.
    setting_key: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )

    value: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)

    #: Who last changed it. ``SET NULL`` rather than ``CASCADE``: an account is
    #: soft-deleted rather than removed, so this only guards manual cleanup — and
    #: losing the administrator must never cost the platform its configuration.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PlatformSetting key={self.setting_key!r}>"


__all__ = ["PlatformSetting", "UserSetting"]
