"""Failed-login throttling (brute-force protection), backed by Redis.

Counts consecutive failed sign-in attempts and refuses further attempts once the
threshold is reached. Two independent counters are kept:

* **per account** (email) — stops an attacker guessing one user's password, even
  from many source addresses;
* **per client IP** — stops one host spraying a common password across many
  accounts, which a per-account counter alone would never notice.

Either counter reaching the threshold blocks the attempt. Redis is the natural
home for this (``architecture.md`` assigns it rate limiting): every key carries a
TTL, so counters expire on their own and the keyspace cannot grow without bound.

**Fails closed.** If Redis is unreachable the attempt is rejected rather than
waved through, matching :mod:`services.token_revocation`. Redis is already a hard
dependency for authenticated requests, so failing open here would only remove the
brute-force protection without buying real availability.

Note that the email counter is incremented for unknown addresses too. That is
deliberate: the lockout behaves identically whether or not the account exists, so
it cannot be used to enumerate accounts.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from redis.exceptions import RedisError

from core.cache import redis_client
from core.config import settings
from core.exceptions import ServiceUnavailableError

logger = structlog.get_logger(__name__)

#: Key namespaces. `attempts:*` counts failures; `lock:*` marks an active lockout.
_ATTEMPTS_PREFIX = "auth:login_attempts:"
_LOCK_PREFIX = "auth:login_lock:"


@dataclass(frozen=True, slots=True)
class ThrottleStatus:
    """Whether a login attempt may proceed."""

    blocked: bool
    #: Seconds until the caller may try again (0 when not blocked).
    retry_after_seconds: int = 0


def _attempts_key(scope: str, identifier: str) -> str:
    return f"{_ATTEMPTS_PREFIX}{scope}:{identifier}"


def _lock_key(scope: str, identifier: str) -> str:
    return f"{_LOCK_PREFIX}{scope}:{identifier}"


class LoginThrottle:
    """Tracks failed sign-in attempts per account and per client IP."""

    def __init__(self) -> None:
        self._max_attempts = settings.MAX_FAILED_LOGIN_ATTEMPTS
        self._window_seconds = int(settings.login_failure_window.total_seconds())
        self._lockout_seconds = int(settings.login_lockout_duration.total_seconds())

    # ------------------------------------------------------------------ checks #

    def check(self, *, email: str, ip_address: str | None) -> ThrottleStatus:
        """Return whether this attempt is currently locked out.

        Called *before* credentials are verified, so a locked account cannot be
        signed into even with the correct password, and no bcrypt work is done on
        behalf of an attacker.
        """
        scopes = self._scopes(email, ip_address)
        try:
            # The longest remaining lockout wins, so the response never
            # under-reports how long the caller must wait.
            longest = 0
            for scope, identifier in scopes:
                ttl = redis_client.ttl(_lock_key(scope, identifier))
                if ttl and ttl > 0:
                    longest = max(longest, int(ttl))
        except RedisError as exc:
            logger.error("login_throttle_read_failed", error=str(exc))
            raise ServiceUnavailableError("Unable to process the request. Please try again.") from exc

        if longest > 0:
            return ThrottleStatus(blocked=True, retry_after_seconds=longest)
        return ThrottleStatus(blocked=False)

    # ----------------------------------------------------------- state changes #

    def register_failure(self, *, email: str, ip_address: str | None) -> ThrottleStatus:
        """Record a failed attempt and lock the scope if it hit the threshold.

        Returns the resulting status so the caller can report a 429 immediately on
        the attempt that crosses the threshold, rather than only on the next one.
        """
        try:
            longest_lock = 0
            for scope, identifier in self._scopes(email, ip_address):
                attempts = self._increment(scope, identifier)
                if attempts >= self._max_attempts:
                    # `set(..., ex=)` rather than the deprecated `setex`.
                    redis_client.set(_lock_key(scope, identifier), "1", ex=self._lockout_seconds)
                    # The window counter has done its job; clear it so the next
                    # attempt after the lockout starts from zero.
                    redis_client.delete(_attempts_key(scope, identifier))
                    longest_lock = max(longest_lock, self._lockout_seconds)
                    logger.warning(
                        "login_locked_out",
                        scope=scope,
                        attempts=attempts,
                        lockout_seconds=self._lockout_seconds,
                    )
        except RedisError as exc:
            logger.error("login_throttle_write_failed", error=str(exc))
            raise ServiceUnavailableError("Unable to process the request. Please try again.") from exc

        if longest_lock > 0:
            return ThrottleStatus(blocked=True, retry_after_seconds=longest_lock)
        return ThrottleStatus(blocked=False)

    def reset(self, *, email: str, ip_address: str | None) -> None:
        """Clear counters after a successful sign-in.

        This is what makes the threshold apply to *consecutive* failures: one
        success wipes the slate for the account (and for the address it came
        from), so an ordinary user who mistypes a few times is never locked out.
        """
        try:
            keys = [
                key
                for scope, identifier in self._scopes(email, ip_address)
                for key in (_attempts_key(scope, identifier), _lock_key(scope, identifier))
            ]
            if keys:
                redis_client.delete(*keys)
        except RedisError as exc:
            # A failure here cannot let anyone in, so log and continue rather than
            # failing a login that has already been authenticated.
            logger.error("login_throttle_reset_failed", error=str(exc))

    # ---------------------------------------------------------------- internals #

    def _increment(self, scope: str, identifier: str) -> int:
        """Increment a scope's counter, setting the window TTL on first failure."""
        key = _attempts_key(scope, identifier)
        attempts = int(redis_client.incr(key))
        if attempts == 1:
            # Fixed window starting at the first failure in this run.
            redis_client.expire(key, self._window_seconds)
        return attempts

    @staticmethod
    def _scopes(email: str, ip_address: str | None) -> list[tuple[str, str]]:
        """The (scope, identifier) pairs tracked for this attempt."""
        scopes = [("email", email.strip().lower())]
        if ip_address:
            scopes.append(("ip", ip_address))
        return scopes
