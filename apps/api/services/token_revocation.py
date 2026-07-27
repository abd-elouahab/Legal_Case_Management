"""Token revocation (denylist) backed by Redis.

JWTs are stateless, so a token stays cryptographically valid until it expires.
Logout and refresh-token rotation therefore need server-side state: the token's
``jti`` is recorded here until its natural expiry, and every verification checks
against that record.

Redis is the right store for this (``architecture.md`` assigns it session cache
and temporary data): entries carry a TTL equal to the token's remaining lifetime,
so the denylist cannot grow without bound.

Fail-closed: if Redis is unreachable the check raises, which the caller turns
into a rejected request. Treating an unknown revocation state as "valid" would
let a logged-out token keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from redis.exceptions import RedisError

from core.cache import redis_client
from core.exceptions import ServiceUnavailableError

logger = structlog.get_logger(__name__)

#: Key namespace for revoked token identifiers.
_KEY_PREFIX = "auth:revoked_jti:"


def _key(jti: str) -> str:
    return f"{_KEY_PREFIX}{jti}"


class TokenRevocationStore:
    """Records revoked token identifiers until they would have expired anyway."""

    def revoke(self, jti: str, expires_at: datetime) -> None:
        """Mark ``jti`` as revoked, expiring the entry when the token does.

        A token that is already past ``expires_at`` needs no entry — it is
        rejected by signature verification regardless.
        """
        ttl_seconds = int((expires_at - datetime.now(UTC)).total_seconds())
        if ttl_seconds <= 0:
            return

        try:
            # `set(..., ex=)` rather than the deprecated `setex`.
            redis_client.set(_key(jti), "1", ex=ttl_seconds)
        except RedisError as exc:
            logger.error("token_revocation_write_failed", error=str(exc))
            raise ServiceUnavailableError("Unable to complete the request. Please try again.") from exc

    def is_revoked(self, jti: str) -> bool:
        """Return whether ``jti`` has been revoked.

        Raises:
            ServiceUnavailableError: if the denylist cannot be read. The caller
                must reject the request rather than assume the token is valid.
        """
        try:
            return redis_client.exists(_key(jti)) > 0
        except RedisError as exc:
            logger.error("token_revocation_read_failed", error=str(exc))
            raise ServiceUnavailableError("Unable to verify the session. Please try again.") from exc
