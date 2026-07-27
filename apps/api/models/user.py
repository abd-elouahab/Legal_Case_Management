"""User ORM model.

Represents an authenticated platform identity. This model deliberately carries
*identity* only — the role column records which kind of user this is (as
required by the storage model in ``architecture.md``), but no permission or
access-control logic lives here. What a role may do is decided by
:mod:`core.roles`, and enforced by :mod:`services.authorization`.

Users are created by administrators (a future User Management feature); there is
no self-registration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class UserRole(StrEnum):
    """The three platform roles defined in ``architecture.md``.

    The canonical role definition: it is persisted on the user record, and
    :mod:`core.roles` maps each member onto the permissions it grants. Nothing
    outside these two modules should spell a role name as a string.
    """

    ADMINISTRATOR = "administrator"
    LAWYER = "lawyer"
    COURT_REPRESENTATIVE = "court"


class User(Base):
    """A platform user account."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Email is the login identifier: unique and indexed. Stored lowercase
    # (normalized by the schema layer) so lookups are case-insensitive.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)

    full_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # bcrypt hash — never the plain password.
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            # Persist the enum *values* ("administrator") rather than the Python
            # member names ("ADMINISTRATOR").
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    # Disabled accounts are rejected at login and on every authenticated request.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Session generation. Every token embeds the generation current when it was
    # issued; a token whose generation is behind this value is rejected. Bumping it
    # therefore invalidates every existing session for this user in a single write,
    # with no need to enumerate them.
    #
    # A counter rather than a timestamp on purpose: the JWT `iat` claim has
    # whole-second precision, so a time-based cut-off cannot reliably distinguish
    # tokens issued in the same second as the change from the replacements issued
    # immediately after it. An integer comparison has no such ambiguity.
    #
    # Stored in PostgreSQL, not Redis, so the invalidation is durable — flushing
    # the cache must never resurrect a revoked session.
    session_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never include the password hash in a repr.
        return f"<User id={self.id!s} email={self.email!r} role={self.role.value!r}>"
