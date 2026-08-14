"""Authentication business logic.

Owns the authentication workflow described in the Authentication spec: verifying
credentials, issuing and rotating JWT pairs, resolving a token back to a user,
revoking sessions on logout, and changing passwords.

Scope: **identity only**. This service answers "who is this user?" — never "what
may they do?". Role-based authorization is a separate feature.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

import structlog

from core import security
from core.config import settings
from core.exceptions import (
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidPasswordError,
    InvalidTokenError,
    TokenExpiredError,
    TooManyLoginAttemptsError,
)
from core.security import IssuedToken, TokenType
from models.user import User
from repositories.user import UserRepository
from services.login_throttle import LoginThrottle
from services.session_registry import (
    NullSessionRegistry,
    SessionRecord,
    SessionStore,
)
from services.token_revocation import TokenRevocationStore

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenPair:
    """An access/refresh token pair issued for one session."""

    access: IssuedToken
    refresh: IssuedToken

    @property
    def access_expires_in(self) -> int:
        """Access token lifetime in whole seconds (for ``expires_in``)."""
        return int(settings.access_token_ttl.total_seconds())


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """A throwaway hash used to equalize timing for unknown emails.

    Without this, a login for a non-existent account would return noticeably
    faster than one for an existing account (no bcrypt work performed), letting
    an attacker enumerate valid emails by timing alone.
    """
    return security.hash_password("timing-equalizer-not-a-real-password")


class AuthService:
    """Coordinates credential verification and token lifecycle."""

    def __init__(
        self,
        users: UserRepository,
        revocations: TokenRevocationStore,
        throttle: LoginThrottle,
        sessions: SessionStore | None = None,
    ) -> None:
        self._users = users
        self._revocations = revocations
        self._throttle = throttle
        # Optional, and a **null** registry rather than `None` at the call sites —
        # the shape `NullEventPublisher` and `NullNotificationMetrics` established.
        # A session record is bookkeeping for the Settings page, so a caller that
        # does not track sessions (`scripts/create_user.py`, a unit test about
        # tokens) must not have to know this exists, and must not need Redis to
        # sign somebody in. See `services/session_registry.py` for why even the
        # real store fails soft where the revocation denylist fails closed.
        self._sessions: SessionStore = (
            sessions if sessions is not None else NullSessionRegistry()
        )

    # ----------------------------------------------------------------- login #

    def authenticate(self, email: str, password: str) -> User:
        """Verify credentials and return the matching active user.

        Does no throttling of its own — callers that expose credentials to the
        network must go through :meth:`login`.

        Raises:
            InvalidCredentialsError: unknown email or wrong password. The same
                error covers both so accounts cannot be enumerated.
            InactiveAccountError: credentials are valid but the account is disabled.
        """
        user = self._users.get_by_email(email)

        if user is None:
            # Burn comparable CPU time before failing (see _dummy_password_hash).
            security.verify_password(password, _dummy_password_hash())
            logger.warning("login_failed", reason="unknown_email", email=email)
            raise InvalidCredentialsError

        if not security.verify_password(password, user.hashed_password):
            logger.warning("login_failed", reason="bad_password", user_id=str(user.id))
            raise InvalidCredentialsError

        if not user.is_active:
            logger.warning("login_failed", reason="account_disabled", user_id=str(user.id))
            raise InactiveAccountError

        return user

    def login(
        self,
        email: str,
        password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[User, TokenPair]:
        """Authenticate a user and start a new session, with brute-force throttling.

        The lockout is checked *before* the password is verified, so a correct
        password does not unlock an account mid-lockout and no bcrypt work is
        performed for a blocked caller.

        A **session identifier** is minted here and carried by both tokens, and by
        every pair that later replaces them. It grants nothing — see
        :attr:`~core.security.TokenPayload.session_id` — and exists so the
        Settings page can list *sign-ins* rather than credentials. ``ip_address``
        and ``user_agent`` are recorded beside it for the same reason and are
        never used to decide anything.

        Raises:
            TooManyLoginAttemptsError: too many recent consecutive failures for
                this account or client IP.
            InvalidCredentialsError: unknown email or wrong password.
            InactiveAccountError: the account is disabled.
        """
        status = self._throttle.check(email=email, ip_address=ip_address)
        if status.blocked:
            logger.warning("login_blocked", reason="too_many_attempts", email=email)
            raise TooManyLoginAttemptsError(status.retry_after_seconds)

        try:
            user = self.authenticate(email, password)
        except InvalidCredentialsError:
            # Only credential guesses count toward the lockout. A disabled account
            # is not a guess, and counting it would replace the actionable
            # "account disabled" message with an opaque 429.
            failure = self._throttle.register_failure(email=email, ip_address=ip_address)
            if failure.blocked:
                # Report the lockout on the attempt that triggered it, rather than
                # letting the caller discover it one attempt later.
                raise TooManyLoginAttemptsError(failure.retry_after_seconds) from None
            raise

        # A success clears the counters, so the threshold tracks *consecutive*
        # failures and an ordinary mistyped password is never punished later.
        self._throttle.reset(email=email, ip_address=ip_address)

        session_id = security.new_session_id()
        tokens = self._issue_pair(user, session_id=session_id)
        self._users.set_last_login(user, datetime.now(UTC))
        self._sessions.record(
            user.id,
            session_id,
            expires_at=tokens.refresh.expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        logger.info("login_succeeded", user_id=str(user.id), role=user.role.value)
        return user, tokens

    # --------------------------------------------------------------- refresh #

    def refresh(
        self,
        refresh_token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[User, TokenPair]:
        """Exchange a valid refresh token for a new token pair.

        The presented refresh token is **rotated**: it is revoked as part of this
        call, so each refresh token is single-use. Replaying a consumed token
        fails, which surfaces token theft instead of silently allowing it.

        The **session identifier is carried across the rotation**, which is the
        whole reason it exists as a claim separate from ``jti``: a browser
        refreshes every fifteen minutes, and a session list keyed by the rotating
        credential would show one laptop as dozens of devices by the end of a
        working day. A token minted before the claim existed has none, so one is
        minted here rather than the session being lost — the session then dates
        from this refresh, which is the honest answer when the sign-in was never
        recorded.
        """
        payload = self._decode(refresh_token, TokenType.REFRESH)
        user = self._load_active_user(payload.subject, session_generation=payload.session_generation)

        session_id = payload.session_id or security.new_session_id()

        # Revoke before issuing so a crash mid-call cannot leave both tokens live.
        self._revocations.revoke(payload.jti, payload.expires_at)
        tokens = self._issue_pair(user, session_id=session_id)
        self._sessions.record(
            user.id,
            session_id,
            expires_at=tokens.refresh.expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        logger.info("token_refreshed", user_id=str(user.id))
        return user, tokens

    # ---------------------------------------------------------------- logout #

    def logout(
        self,
        user: User,
        access_jti: str,
        access_expires_at: datetime,
        refresh_token: str | None,
        *,
        session_id: str | None = None,
    ) -> None:
        """End a session by revoking its access token and, if given, its refresh token.

        A malformed or already-expired refresh token is ignored: logout must
        succeed so the client can clear local state either way.

        ``session_id`` is the session record to forget, so a signed-out device
        stops appearing on the Settings page. Forgetting it is bookkeeping — the
        revocation above is what ends the session — so a registry that cannot be
        reached costs a stale row and nothing else.
        """
        self._revocations.revoke(access_jti, access_expires_at)

        if refresh_token:
            try:
                refresh_payload = security.decode_token(refresh_token, expected_type=TokenType.REFRESH)
            except security.TokenError:
                logger.info("logout_refresh_token_unusable", user_id=str(user.id))
            else:
                self._revocations.revoke(refresh_payload.jti, refresh_payload.expires_at)
                session_id = session_id or refresh_payload.session_id

        if session_id:
            self._sessions.remove(user.id, session_id)

        logger.info("logout_succeeded", user_id=str(user.id))

    # ------------------------------------------------------------- sessions #

    def active_sessions(self, user: User) -> list[SessionRecord]:
        """Every sign-in that can still be resumed for this account.

        ``20-settings.md``'s *"View Active Sessions"*. Read from the registry
        rather than from the token store, because a token is a credential and a
        session is a sign-in — see :mod:`services.session_registry`.

        A deployment whose registry is unavailable, or that runs without one,
        reports **no sessions** rather than failing. That is a stated limitation
        of a view and never a weakening of revocation: what makes a session usable
        is the signature, the denylist, and ``users.session_generation``, none of
        which is in this list.
        """
        return self._sessions.list_sessions(user.id)

    def revoke_other_sessions(
        self,
        user: User,
        *,
        current_access: security.TokenPayload,
        current_refresh_token: str | None,
    ) -> TokenPair:
        """End every session for this account except the one making the request.

        ``20-settings.md``'s *"Logout Other Sessions"*, and it is deliberately the
        **same mechanism** :meth:`change_password` uses rather than a second one:
        bump ``users.session_generation``, which invalidates every token minted
        under the previous generation in a single durable write, then mint a
        replacement pair for this device under the new one. There is no session
        enumeration involved, so a device the registry never heard of is signed
        out exactly like one it did.

        The caller's outgoing tokens are additionally denylisted, matching what
        logout and password change do with the tokens actually being retired.

        The **session identifier is preserved**, so the device performing this
        keeps its place in the list rather than appearing to be a new sign-in.

        Returns:
            The replacement token pair for the current session. Clients must swap
            in the returned access token, exactly as they do after a password
            change.
        """
        previous_generation = user.session_generation
        self._users.bump_session_generation(user)

        self._revocations.revoke(current_access.jti, current_access.expires_at)
        refresh_session_id = self._retire_refresh_token(current_refresh_token)

        session_id = current_access.session_id or refresh_session_id or security.new_session_id()
        tokens = self._issue_pair(user, session_id=session_id)
        self._sessions.clear(user.id, keep=session_id)
        self._sessions.record(user.id, session_id, expires_at=tokens.refresh.expires_at)

        logger.info(
            "other_sessions_revoked",
            user_id=str(user.id),
            session_generation=user.session_generation,
            previous_generation=previous_generation,
        )
        return tokens

    # ------------------------------------------------------- token → identity #

    def resolve_access_token(self, token: str) -> tuple[User, security.TokenPayload]:
        """Return the active user a valid, non-revoked access token belongs to.

        The decoded payload is returned alongside the user so callers (e.g.
        logout) can act on this exact token without decoding it a second time.
        """
        payload = self._decode(token, TokenType.ACCESS)
        return (
            self._load_active_user(payload.subject, session_generation=payload.session_generation),
            payload,
        )

    # -------------------------------------------------------- change password #

    def change_password(
        self,
        user: User,
        current_password: str,
        new_password: str,
        *,
        current_access: security.TokenPayload,
        current_refresh_token: str | None,
    ) -> TokenPair:
        """Replace a user's password and end every existing session.

        Changing a password must not leave a session alive on a device the user is
        trying to lock out (a shared or stolen machine, for example). Every
        previously-issued token is therefore invalidated, and the caller is handed
        a **fresh token pair** so the device performing the change stays signed in.
        All other devices must authenticate again.

        Revocation works by incrementing the user's ``session_generation``: every
        token carries the generation it was minted under, so a single write
        invalidates all of them at once — no session enumeration required. The
        replacement pair is then minted under the *new* generation, which is why it
        survives while everything older does not.

        The caller's outgoing access and refresh tokens are additionally denylisted.
        The generation bump alone already rejects them; this makes the intent
        explicit and keeps logout and password-change behaving identically for the
        tokens actually being retired.

        ``must_change_password`` is cleared as part of the same write — see
        :meth:`~repositories.user.UserRepository.set_password`, whose default is
        exactly that. ``20-settings.md``'s Password Change Policy asks for it, and
        it has held since Authentication shipped: a password the user chose is by
        definition not the temporary one an administrator handed them.

        Returns:
            The replacement token pair for the current session.

        Raises:
            InvalidPasswordError: the supplied current password does not match.
        """
        if not security.verify_password(current_password, user.hashed_password):
            logger.warning("password_change_failed", reason="bad_current_password", user_id=str(user.id))
            raise InvalidPasswordError

        previous_generation = user.session_generation
        self._users.set_password(user, security.hash_password(new_password))

        # Retire the tokens this request arrived with.
        self._revocations.revoke(current_access.jti, current_access.expires_at)
        refresh_session_id = self._retire_refresh_token(current_refresh_token)

        # The device making the change keeps its identity, so its row on the
        # Settings page stays put rather than looking like a fresh sign-in — which
        # would be indistinguishable from the intruder this control exists to
        # remove.
        session_id = current_access.session_id or refresh_session_id or security.new_session_id()
        tokens = self._issue_pair(user, session_id=session_id)
        self._sessions.clear(user.id, keep=session_id)
        self._sessions.record(user.id, session_id, expires_at=tokens.refresh.expires_at)

        logger.info(
            "password_changed",
            user_id=str(user.id),
            sessions_revoked=True,
            session_generation=user.session_generation,
            previous_generation=previous_generation,
        )
        return tokens

    # --------------------------------------------------------------- helpers #

    def _issue_pair(self, user: User, *, session_id: str | None = None) -> TokenPair:
        """Mint a token pair stamped with the user's current session generation."""
        subject = str(user.id)
        generation = user.session_generation
        return TokenPair(
            access=security.create_access_token(subject, generation, session_id),
            refresh=security.create_refresh_token(subject, generation, session_id),
        )

    def _retire_refresh_token(self, refresh_token: str | None) -> str | None:
        """Denylist an outgoing refresh token, returning the session it belonged to.

        Shared by :meth:`change_password` and :meth:`revoke_other_sessions`, which
        do the same thing to the same token for the same reason. A malformed or
        expired token is ignored — the generation bump covers it regardless, and
        refusing the operation because the *outgoing* credential was already
        unusable would block exactly the person trying to secure their account.
        """
        if not refresh_token:
            return None
        try:
            payload = security.decode_token(refresh_token, expected_type=TokenType.REFRESH)
        except security.TokenError:
            return None

        self._revocations.revoke(payload.jti, payload.expires_at)
        return payload.session_id

    def _decode(self, token: str, expected_type: TokenType) -> security.TokenPayload:
        """Decode a token and reject it if it has been revoked."""
        try:
            payload = security.decode_token(token, expected_type=expected_type)
        except security.TokenExpiredError as exc:
            raise TokenExpiredError from exc
        except security.TokenError as exc:
            raise InvalidTokenError from exc

        if self._revocations.is_revoked(payload.jti):
            logger.warning("token_rejected", reason="revoked", token_type=expected_type.value)
            raise InvalidTokenError

        return payload

    def _load_active_user(self, subject: str, *, session_generation: int) -> User:
        """Load the token's subject, rejecting deleted, disabled, or revoked sessions."""
        try:
            user_id = uuid.UUID(subject)
        except ValueError as exc:
            raise InvalidTokenError from exc

        user = self._users.get_by_id(user_id)
        if user is None:
            logger.warning("token_rejected", reason="unknown_subject")
            raise InvalidTokenError

        if not user.is_active:
            logger.warning("token_rejected", reason="account_disabled", user_id=str(user.id))
            raise InactiveAccountError

        if session_generation < user.session_generation:
            # The user's generation has moved on (e.g. a password change), so this
            # session was revoked in bulk along with every other one.
            logger.warning("token_rejected", reason="session_revoked", user_id=str(user.id))
            raise InvalidTokenError

        return user
