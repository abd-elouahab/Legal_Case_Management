"""Redis client with connection pooling and a health check.

Provides a shared, pooled Redis client. Caching and Pub/Sub are intentionally
not implemented here — this module only establishes and verifies connectivity.
"""

from __future__ import annotations

import redis
from redis import Redis
from redis.connection import ConnectionPool

from core.config import settings

_pool: ConnectionPool = redis.ConnectionPool.from_url(
    settings.REDIS_URL,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    decode_responses=True,
    socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT,
    socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
)

redis_client: Redis = redis.Redis(connection_pool=_pool)


def check_redis_connection() -> None:
    """Ping Redis to verify connectivity. Raises on failure."""
    redis_client.ping()


def close_redis() -> None:
    """Release all pooled Redis connections (called on application shutdown)."""
    redis_client.close()
    _pool.disconnect()
