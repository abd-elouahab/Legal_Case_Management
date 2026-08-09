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
* **Nothing here knows what happens to the events it publishes.** Activation, a
  role change, a deactivation, and a password reset are announced to the
  dispatcher as domain events, on the affected account's own topic. This service
  holds :class:`~services.events.EventPublisher`, which has one method and no way
  to ask who is listening — so the fact that a notification is created from some
  of them is not visible from this file, and adding a second consumer (email,
  WhatsApp, an audit sink) requires no change here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from core import security
from core.events import DomainEventType, user_topic
from core.exceptions import DuplicateEmailError, SelfModificationError, UserNotFoundError
from models.user import User, UserStatus
from repositories.user import UserRepository
from schemas.user import UserCreate, UserListQuery, UserUpdate
from services.events import EventPublisher, NullEventPublisher

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

    def __init__(self, users: UserRepository, *, events: EventPublisher | None = None) -> None:
        self._users = users
        #: Defaults to a publisher that announces nothing, so a script or a unit
        #: test that is not about events constructs this service unchanged — the
        #: same contract :class:`~services.case.CaseService` and
        #: :class:`~services.document.DocumentService` keep. The application
        #: wires the real dispatcher in :mod:`api.deps`.
        self._events = events or NullEventPublisher()

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

        # Captured before the write, because both are what the events below
        # compare against — and after it, `user` *is* the new value.
        previous_role = user.role
        previous_status = user.status

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

        # Published **after the commit** and only for what actually changed. Two
        # events rather than a generic `user.updated`, for the reason
        # `services/case.py` publishes `case.archived` rather than a status
        # change: a consumer that had to infer "this person's role moved" from a
        # field list would get it wrong the first time a field is added.
        if saved.role is not previous_role:
            self._announce(
                saved,
                DomainEventType.USER_ROLE_CHANGED,
                actor=actor,
                role=saved.role.value,
                previous_role=previous_role.value,
            )
        if saved.status is not previous_status:
            self._announce_status(saved, previous_status, actor=actor)

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
        previous_status = user.status
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

        # Only when something actually changed. Deactivation is idempotent, and a
        # repeated request must not announce a second time — the same rule
        # :meth:`~services.case.CaseService.archive_case` follows.
        if not already_inactive:
            self._announce(
                saved,
                DomainEventType.USER_DEACTIVATED,
                actor=actor,
                status=saved.status.value,
                previous_status=previous_status.value,
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

        # The payload carries **nothing about the password** — not the temporary
        # one, not its length, not whether it was generated. An event travels to
        # consumers this module does not know about, and the credential is the
        # one value in this service that must not leave the response it was
        # returned in.
        self._announce(
            user, DomainEventType.USER_PASSWORD_RESET, actor=actor, sessions_revoked=True
        )
        return PasswordReset(user=user, temporary_password=temporary_password)

    # -------------------------------------------------------------- events #

    def _announce_status(self, user: User, previous: UserStatus, *, actor: User) -> None:
        """Announce an account being enabled or disabled, as the event that fits.

        Two event types rather than one carrying a status, for the reason
        :meth:`~services.case.CaseService._publish_status_change` gives: a
        consumer that has to infer "this account was re-enabled" from a pair of
        status values is a consumer that will get it wrong the first time a
        status is added.
        """
        became_active = user.status is UserStatus.ACTIVE
        self._announce(
            user,
            DomainEventType.USER_ACTIVATED if became_active else DomainEventType.USER_DEACTIVATED,
            actor=actor,
            status=user.status.value,
            previous_status=previous.value,
        )

    def _announce(
        self, user: User, event_type: DomainEventType, *, actor: User, **payload: Any
    ) -> None:
        """Publish one event about one account, on that account's own topic.

        The topic is the **affected user's**, not the actor's, which is what
        makes the authorization rule identity equality: an account event is
        delivered to that person's own connections and to nobody else's, decided
        without a database lookup (see :mod:`services.realtime_access`).

        The payload carries the **status and role values** and nothing else — no
        email, no name, no phone number, and no credential. Those are personal
        data that the authorized read a client makes next will supply, which is
        the same line ``15-real-time-synchronization.md`` draws for a case's title
        and a document's filename.

        Never raises: the account change is already committed, and
        :meth:`~services.events.EventDispatcher.publish` swallows its own
        failures — so an unreachable consumer cannot turn a successful
        deactivation into a 500 that invites a duplicating retry.
        """
        self._events.publish(
            event_type=event_type,
            topic=user_topic(user.id),
            actor_id=actor.id,
            payload={"user_id": user.id, "role": user.role.value, **payload},
        )

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
