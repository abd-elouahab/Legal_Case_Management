"""Active sessions, backed by Redis.

``20-settings.md``'s Account & Security section asks for three things: change
password, **view active sessions**, and log other sessions out. Two of the three
already worked — :meth:`~services.auth.AuthService.change_password` clears
``must_change_password`` and bumps ``users.session_generation``, which invalidates
every token for that account in one write. The platform could therefore *revoke*
every session and could not *name* one, because JWTs are stateless and there was
nowhere a sign-in was recorded.

This module is that record, and what it is **not** matters more than what it is:

* **It is a view, never a boundary.** Whether a session is still usable is decided
  exactly as before — the signature, the expiry, the ``jti`` denylist, and
  ``sgen`` against the user's current generation, three of which are durable and
  the fourth of which is in PostgreSQL. Nothing here is consulted when a request
  is authorized. A row that lingered after a revocation would be a stale line on a
  settings page; it could never be a session that still worked.
* **It therefore fails soft**, which is the opposite of
  :class:`~services.token_revocation.TokenRevocationStore` and deliberately so.
  That store fails *closed* because treating an unknown revocation state as
  "valid" would let a logged-out token keep working. Here, an unreachable Redis
  means the platform cannot *list* somebody's sessions — so it returns none and
  says nothing false, rather than refusing a sign-in because bookkeeping was
  unavailable. Recording a session must never be able to fail a login.
* **It stores no credential.** A record carries a session identifier — opaque,
  random, and granting nothing — the moment it began, when it was last seen, the
  client's IP, and a **truncated** user-agent. No token, no ``jti``, no email, and
  no name: the page that renders this is already showing it to the one person it
  is about, and everything beyond "which device is this?" would be a detail
  somebody who stole a session could read as easily as its owner.

**Keyed by the ``sid`` claim, not by ``jti``.** A ``jti`` rotates on every
refresh — every fifteen minutes, in this deployment — so a registry keyed by one
would show a lawyer twenty "sessions" for one laptop by lunchtime. ``sid`` is
minted once at sign-in and carried through every rotation, which is what makes
*"Chrome on Windows, since Tuesday"* expressible.

Entries expire on their own, with a TTL equal to the refresh token's remaining
lifetime: a session that has not been refreshed within the refresh window cannot
be resumed, so a record of it is a record of nothing. Nothing sweeps this store,
for the reason the denylist has no sweeper either.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import structlog
from redis.exceptions import RedisError

from core.cache import redis_client

logger = structlog.get_logger(__name__)

#: Key namespace. One hash per user, one field per session, so listing somebody's
#: sessions is a single ``HGETALL`` rather than a key scan — a scan across a
#: production keyspace to render a settings page is the kind of query that is
#: fine until the day it is not.
_KEY_PREFIX: Final[str] = "auth:sessions:"

#: Longest user-agent kept. Enough to recognise a browser and an operating
#: system, short enough that a crafted header cannot make one account's hash
#: arbitrarily large — the same bounding every payload on this platform gets.
_MAX_USER_AGENT: Final[int] = 200

#: Most sessions listed for one account. A person with more than this has a
#: problem the list will not solve, and an unbounded read is an unbounded
#: response.
MAX_SESSIONS_LISTED: Final[int] = 50


def _key(user_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}{user_id}"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One sign-in, as the Settings page shows it."""

    #: The ``sid`` claim. Opaque and random — see :func:`~core.security.new_session_id`.
    session_id: str
    #: When the sign-in happened. Not when the token was last rotated: the point
    #: of a stable identifier is that this does not move.
    created_at: datetime
    #: When a request on this session was last seen, which for a browser means the
    #: last refresh rather than the last click.
    last_seen_at: datetime
    #: When the session can no longer be resumed, absent a refresh.
    expires_at: datetime
    #: The client address, as the application resolved it. ``None`` when the
    #: deployment is behind a proxy it has not been configured to trust — an
    #: absent address is better than a load balancer's.
    ip_address: str | None
    #: A truncated user-agent, for recognising the device. Never parsed.
    user_agent: str | None


class SessionStore(Protocol):
    """What :class:`~services.auth.AuthService` requires of a session record.

    A protocol rather than a concrete dependency, in the shape ``EmailProvider``,
    ``OcrEngine``, and ``EventPublisher`` established — and here it earns its keep
    immediately: :class:`NullSessionRegistry` is what lets a script provision the
    first administrator, and a unit test about tokens sign somebody in, without
    Redis being reachable at all.
    """

    def record(
        self,
        user_id: uuid.UUID,
        session_id: str,
        *,
        expires_at: datetime,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Create or refresh one session's record."""
        ...

    def list_sessions(self, user_id: uuid.UUID) -> list[SessionRecord]:
        """Every live session for this account."""
        ...

    def remove(self, user_id: uuid.UUID, session_id: str) -> None:
        """Forget one session."""
        ...

    def clear(self, user_id: uuid.UUID, *, keep: str | None = None) -> None:
        """Forget every session for this account, optionally keeping one."""
        ...


class SessionRegistry:
    """Records which sign-ins are live, for the account they belong to.

    Every method swallows :class:`~redis.exceptions.RedisError`; see the module
    docstring for why this store fails soft where the revocation denylist fails
    closed.
    """

    def record(
        self,
        user_id: uuid.UUID,
        session_id: str,
        *,
        expires_at: datetime,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Create or refresh one session's record.

        Called on sign-in and again on every token rotation. A rotation **keeps
        the original** ``created_at`` and moves ``last_seen_at``, which is what
        makes "since Tuesday" mean since Tuesday rather than since the last
        fifteen-minute refresh.

        The client details are only written when supplied, so a refresh made by a
        background tab does not erase the user-agent the sign-in recorded.
        """
        now = datetime.now(UTC)
        ttl_seconds = int((expires_at - now).total_seconds())
        if ttl_seconds <= 0:
            return

        try:
            key = _key(user_id)
            existing = self._read_field(key, session_id)

            payload = {
                "created_at": (existing or {}).get("created_at") or now.isoformat(),
                "last_seen_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "ip_address": ip_address or (existing or {}).get("ip_address"),
                "user_agent": (
                    user_agent[:_MAX_USER_AGENT]
                    if user_agent
                    else (existing or {}).get("user_agent")
                ),
            }

            redis_client.hset(key, session_id, json.dumps(payload))
            # The hash's TTL is the longest-lived session in it. Individual fields
            # cannot expire on their own in Redis, so a session past its own
            # `expires_at` is filtered on read — which is correct anyway, since a
            # record has to be readable to be filterable.
            redis_client.expire(key, ttl_seconds, gt=True)
        except RedisError as exc:
            # A login must not fail because bookkeeping was unavailable.
            logger.warning("session_registry_write_failed", error=str(exc))

    def list_sessions(self, user_id: uuid.UUID) -> list[SessionRecord]:
        """Every live session for this account, newest sign-in first.

        Returns an **empty list** when the store is unreachable rather than
        raising: the caller is a settings page, and a page that will not load is a
        worse answer than one that says it has nothing to show. The revocation
        controls beside it keep working regardless, because they act on the
        PostgreSQL generation counter rather than on this.

        Expired records are filtered here (see :meth:`record` for why they can
        exist) and **removed**, so the store does not accumulate rows nothing will
        ever show.
        """
        try:
            raw = redis_client.hgetall(_key(user_id))
        except RedisError as exc:
            logger.warning("session_registry_read_failed", error=str(exc))
            return []

        now = datetime.now(UTC)
        sessions: list[SessionRecord] = []
        stale: list[str] = []

        for field, value in raw.items():
            session_id = field.decode() if isinstance(field, bytes) else str(field)
            record = _decode(session_id, value)
            if record is None or record.expires_at <= now:
                stale.append(session_id)
                continue
            sessions.append(record)

        if stale:
            self._forget(user_id, stale)

        sessions.sort(key=lambda record: record.created_at, reverse=True)
        return sessions[:MAX_SESSIONS_LISTED]

    def remove(self, user_id: uuid.UUID, session_id: str) -> None:
        """Forget one session — a sign-out."""
        self._forget(user_id, [session_id])

    def clear(self, user_id: uuid.UUID, *, keep: str | None = None) -> None:
        """Forget every session for this account, optionally keeping one.

        The bookkeeping half of "log out everywhere else". The half that actually
        revokes anything is the ``session_generation`` bump in PostgreSQL, which
        happens whether or not this succeeds — so a Redis outage costs a stale
        list, never a session that should have ended and did not.
        """
        try:
            if keep is None:
                redis_client.delete(_key(user_id))
                return

            key = _key(user_id)
            fields = [
                field.decode() if isinstance(field, bytes) else str(field)
                for field in redis_client.hkeys(key)
            ]
            doomed = [field for field in fields if field != keep]
            if doomed:
                redis_client.hdel(key, *doomed)
        except RedisError as exc:
            logger.warning("session_registry_clear_failed", error=str(exc))

    # --------------------------------------------------------------- helpers #

    def _read_field(self, key: str, session_id: str) -> dict[str, Any] | None:
        """Read one stored record, or ``None`` if there is none or it is unusable."""
        raw = redis_client.hget(key, session_id)
        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, dict) else None

    def _forget(self, user_id: uuid.UUID, session_ids: list[str]) -> None:
        try:
            redis_client.hdel(_key(user_id), *session_ids)
        except RedisError as exc:
            logger.warning("session_registry_delete_failed", error=str(exc))


def _decode(session_id: str, value: Any) -> SessionRecord | None:
    """Turn a stored field back into a record, tolerating anything malformed.

    ``None`` rather than an exception for a value this version cannot read, for
    the reason every open registry on this platform is read tolerantly: a record
    written by a later release must not make an earlier one unable to show
    somebody their sessions. The caller deletes what it cannot read.
    """
    try:
        payload = json.loads(value)
        return SessionRecord(
            session_id=session_id,
            created_at=datetime.fromisoformat(payload["created_at"]),
            last_seen_at=datetime.fromisoformat(payload["last_seen_at"]),
            expires_at=datetime.fromisoformat(payload["expires_at"]),
            ip_address=payload.get("ip_address"),
            user_agent=payload.get("user_agent"),
        )
    except (TypeError, ValueError, KeyError):
        return None


class NullSessionRegistry:
    """A registry that records nothing and lists nothing.

    The default for an :class:`~services.auth.AuthService` built without one — a
    provisioning script, or a test about token lifetimes. Same role and reasoning
    as :class:`~services.events.NullEventPublisher`: the feature that consumes
    this is optional, so the *absence* of it must be expressible without a branch
    at every call site.

    An empty session list from this is honest rather than misleading: a deployment
    wired this way is not tracking sessions, so there is nothing to show. It never
    weakens revocation, because revocation was never this store's job.
    """

    def record(
        self,
        user_id: uuid.UUID,
        session_id: str,
        *,
        expires_at: datetime,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Discard the observation."""

    def list_sessions(self, user_id: uuid.UUID) -> list[SessionRecord]:
        """Report no sessions."""
        return []

    def remove(self, user_id: uuid.UUID, session_id: str) -> None:
        """Do nothing."""

    def clear(self, user_id: uuid.UUID, *, keep: str | None = None) -> None:
        """Do nothing."""


__all__ = [
    "MAX_SESSIONS_LISTED",
    "NullSessionRegistry",
    "SessionRecord",
    "SessionRegistry",
    "SessionStore",
]
