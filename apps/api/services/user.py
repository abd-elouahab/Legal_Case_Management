"""User management business logic.

Owns the administrative workflow the User Management spec defines: creating,
listing, updating, deactivating, and resetting the password of an account.

Scope boundaries, kept deliberately sharp:

* **Authorization is not decided here.** Whether the caller may manage users is
  settled by the dependencies in :mod:`api.authorization` before the request ever
  reaches this service. What it *does* enforce are business rules that no
  permission can express — email uniqueness, and an administrator not disabling
  their own account.
* **Authentication is not touched here.** Verifying credentials and issuing
  tokens remain :class:`~services.auth.AuthService`'s job. The one overlap is
  deliberate: a password reset must revoke the target's sessions, which is done
  through the same ``session_generation`` mechanism a password change uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from core import security
from core.exceptions import DuplicateEmailError, SelfModificationError, UserNotFoundError
from models.user import User, UserStatus
from repositories.user import UserRepository
from schemas.user import UserCreate, UserListQuery, UserUpdate

logger = structlog.get_logger(__name__)

#: Fields whose modification is refused on the caller's *own* account, because
#: doing so removes their ability to undo it.
_SELF_PROTECTED_FIELDS = frozenset({"role", "status"})


@dataclass(frozen=True, slots=True)
class UserPageResult:
    """One page of users together with the total matching the filters."""

    users: list[User]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class PasswordReset:
    """Outcome of an administrator-initiated password reset."""

    user: User
    #: The generated password, returned to the administrator exactly once. Only
    #: its hash is stored, and it is never logged.
    temporary_password: str


class UserService:
    """Coordinates the administrative user lifecycle."""

    def __init__(self, users: UserRepository) -> None:
        self._users = users

    # ------------------------------------------------------------- reading #

    def list_users(self, query: UserListQuery) -> UserPageResult:
        """Return one page of the user directory.

        Search, filtering, sorting, and pagination are all applied in the
        database rather than in Python, so the cost of a page does not grow with
        the size of the directory.
        """
        users, total = self._users.list_users(query)
        return UserPageResult(users=users, total=total, page=query.page, page_size=query.page_size)

    def get_user(self, user_id: uuid.UUID) -> User:
        """Return one user.

        Raises:
            UserNotFoundError: no account has this identifier.
        """
        user = self._users.get_by_id(user_id)
        if user is None:
            logger.info("user_lookup_failed", user_id=str(user_id))
            raise UserNotFoundError
        return user

    # ------------------------------------------------------------ creating #

    def create_user(self, payload: UserCreate, *, actor: User) -> User:
        """Provision a new account.

        Raises:
            DuplicateEmailError: the email is already in use.
        """
        if self._users.email_taken(payload.email):
            logger.info("user_create_rejected", reason="duplicate_email")
            raise DuplicateEmailError

        user = User(
            id=uuid.uuid4(),
            email=payload.email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            hashed_password=security.hash_password(payload.password),
            phone=payload.phone,
            profile_image=payload.profile_image,
            role=payload.role,
            status=payload.status,
            must_change_password=payload.must_change_password,
            # Audit fields are populated here, never accepted from the request:
            # a client must not be able to claim someone else made the change.
            created_by=actor.id,
            updated_by=actor.id,
        )
        created = self._users.create(user)

        logger.info(
            "user_created",
            user_id=str(created.id),
            role=created.role.value,
            status=created.status.value,
            actor_id=str(actor.id),
        )
        return created

    # ------------------------------------------------------------ updating #

    def update_user(self, user_id: uuid.UUID, payload: UserUpdate, *, actor: User) -> User:
        """Apply a partial update to an account.

        Only the fields the client actually sent are written, so a PATCH that
        omits a field leaves it alone while ``"phone": null`` clears it.

        Raises:
            UserNotFoundError: no account has this identifier.
            DuplicateEmailError: the new email belongs to another account.
            SelfModificationError: the caller tried to change their own role or
                status.
        """
        user = self.get_user(user_id)
        changes = payload.provided_fields()

        self._reject_self_lockout(user, changes, actor=actor)

        email = changes.get("email")
        if isinstance(email, str) and self._users.email_taken(email, excluding=user.id):
            logger.info("user_update_rejected", reason="duplicate_email", user_id=str(user.id))
            raise DuplicateEmailError

        for field, value in changes.items():
            setattr(user, field, value)
        user.updated_by = actor.id

        saved = self._users.save(user)

        logger.info(
            "user_updated",
            user_id=str(saved.id),
            # The field *names* say what an administrator touched without putting
            # the values — an email, a phone number — into the log.
            fields=sorted(changes),
            actor_id=str(actor.id),
        )
        return saved

    def deactivate_user(self, user_id: uuid.UUID, *, actor: User) -> User:
        """Soft-delete an account by marking it inactive.

        The row is never removed: audit logs, case assignments, and document
        ownership all reference users, and deleting the record would orphan that
        history. An inactive account cannot authenticate, and every existing
        session for it is revoked immediately rather than surviving until its
        token expires.

        Idempotent — deactivating an already-inactive account succeeds and leaves
        it inactive, so a repeated request is not an error.

        Raises:
            UserNotFoundError: no account has this identifier.
            SelfModificationError: the caller tried to deactivate themselves.
        """
        user = self.get_user(user_id)

        if user.id == actor.id:
            logger.info("user_deactivate_rejected", reason="self", user_id=str(user.id))
            raise SelfModificationError

        already_inactive = user.status is UserStatus.INACTIVE
        user.status = UserStatus.INACTIVE
        user.updated_by = actor.id
        if not already_inactive:
            # Disabling an account must end its sessions now; otherwise a signed-in
            # user keeps working until their access token expires.
            user.session_generation += 1

        saved = self._users.save(user)

        logger.info(
            "user_deactivated",
            user_id=str(saved.id),
            actor_id=str(actor.id),
            sessions_revoked=not already_inactive,
        )
        return saved

    # ------------------------------------------------------------ passwords #

    def reset_password(self, user_id: uuid.UUID, *, actor: User) -> PasswordReset:
        """Issue a temporary password for an account and force a change.

        The generated password is hashed before storage and returned to the
        administrator once, in the response, so they can pass it on. It is never
        logged. Every session the user has is revoked, because a reset is what an
        administrator does when an account may be compromised — leaving the
        existing sessions alive would defeat the point.

        Raises:
            UserNotFoundError: no account has this identifier.
        """
        user = self.get_user(user_id)
        temporary_password = security.generate_temporary_password()

        self._users.set_password(
            user,
            security.hash_password(temporary_password),
            revoke_sessions=True,
            must_change_password=True,
            updated_by=actor.id,
        )

        logger.info(
            "user_password_reset",
            user_id=str(user.id),
            actor_id=str(actor.id),
            sessions_revoked=True,
        )
        return PasswordReset(user=user, temporary_password=temporary_password)

    # -------------------------------------------------------------- helpers #

    @staticmethod
    def _reject_self_lockout(user: User, changes: dict[str, Any], *, actor: User) -> None:
        """Refuse a self-applied role or status change.

        An administrator who demotes or disables themselves cannot undo it, and
        if they are the last administrator nobody else can either — recovery
        would mean running ``scripts/create_user.py`` on the server. Changing
        one's own name, phone, or avatar stays permitted.
        """
        if user.id != actor.id:
            return

        attempted = _SELF_PROTECTED_FIELDS & changes.keys()
        # Re-submitting the current value changes nothing, so it is not a lockout
        # risk and a form that round-trips every field still works.
        if any(changes[field] != getattr(user, field) for field in attempted):
            logger.info("user_update_rejected", reason="self_modification", user_id=str(user.id))
            raise SelfModificationError
