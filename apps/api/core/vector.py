"""Qdrant vector-database client and a connectivity check.

Establishes a shared Qdrant client and verifies connectivity. Collections are
not created and no embeddings are generated here.
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from core.config import settings

qdrant_client: QdrantClient = QdrantClient(
    host=settings.QDRANT_HOST,
    port=settings.QDRANT_PORT,
    api_key=settings.QDRANT_API_KEY,
    https=settings.QDRANT_HTTPS,
    timeout=settings.QDRANT_TIMEOUT,
    # Skip the eager client/server version probe on construction: it emits a
    # warning and blocks when Qdrant is unreachable. Connectivity is verified
    # explicitly via check_qdrant_connection().
    check_compatibility=False,
)


def check_qdrant_connection() -> None:
    """Verify connectivity to Qdrant by listing collections. Raises on failure."""
    qdrant_client.get_collections()


def close_qdrant() -> None:
    """Close the Qdrant client (called on application shutdown)."""
    qdrant_client.close()
