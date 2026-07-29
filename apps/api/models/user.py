"""User ORM model.

Represents a platform identity. The role column records which kind of user this
is (as required by the storage model in ``architecture.md``), but no permission
or access-control logic lives here. What a role may do is decided by
:mod:`core.roles`, and enforced by :mod:`services.authorization`.

Users are created by administrators (see ``services/user.py`` and
``api/v1/users/router.py``); there is no self-registration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Uuid, func
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


class UserStatus(StrEnum):
    """Lifecycle state of an account.

    Persisted, and the single answer to "may this account be used?" — see
    :attr:`User.is_active`. Only :attr:`ACTIVE` can authenticate.

    :attr:`INACTIVE` is also the soft-delete state: deleting a user marks the
    account inactive rather than removing the row, so audit trails and historical
    references to the account survive.
    """

    #: Enabled; the account may sign in.
    ACTIVE = "active"
    #: Disabled, either administratively or by a soft delete.
    INACTIVE = "inactive"
    #: Blocked pending review. Cannot sign in; distinct from INACTIVE so an
    #: administrator can tell a decommissioned account from a suspended one.
    SUSPENDED = "suspended"


class User(Base):
    """A platform user account."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Email is the login identifier: unique and indexed. Stored lowercase
    # (normalized by the schema layer) so lookups are case-insensitive.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)

    # Names are stored separately because the platform searches and sorts on them
    # independently (see the User Management spec). The combined display name is
    # derived, never stored, so the two can never disagree.
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # bcrypt hash — never the plain password.
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)

    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Location of the user's avatar (an object-storage key or absolute URL).
    #: Images themselves live in MinIO, never in PostgreSQL.
    profile_image: Mapped[str | None] = mapped_column(String(512), nullable=True)

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

    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            name="user_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
    )

    #: Set when an administrator resets the password. The user keeps a working
    #: session but the client is expected to send them to a change-password screen
    #: before anything else; ``AuthService.change_password`` clears it.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

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

    # Audit trail (`code-standards.md`: every sensitive operation is recorded).
    # Nullable because the first administrator is provisioned by a script with no
    # acting user, and ON DELETE SET NULL because users are soft-deleted — a row
    # is never actually removed, so this only guards against manual cleanup.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # ------------------------------------------------------------- derived #

    @property
    def full_name(self) -> str:
        """Display name, composed from the stored name parts.

        Derived rather than stored so it cannot drift from ``first_name`` /
        ``last_name`` after an edit.
        """
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_active(self) -> bool:
        """Whether the account may authenticate.

        A single reading of :attr:`status`, so authentication has exactly one
        question to ask and adding a future status cannot accidentally grant
        sign-in. Read-only: change :attr:`status` to enable or disable an account.
        """
        return self.status is UserStatus.ACTIVE

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never include the password hash in a repr.
        return f"<User id={self.id!s} email={self.email!r} role={self.role.value!r}>"
