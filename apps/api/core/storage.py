"""MinIO object-storage client and a connectivity check.

Establishes a shared MinIO client and verifies it can reach the server. Buckets
are not created and no files are uploaded here.
"""

from __future__ import annotations

import urllib3
from minio import Minio

from core.config import settings

# Bound connect/read time and disable retries so readiness probes fail fast when
# MinIO is unreachable instead of blocking on urllib3's default retry backoff.
_http_client = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=settings.MINIO_CONNECT_TIMEOUT, read=settings.MINIO_CONNECT_TIMEOUT),
    retries=urllib3.Retry(total=0),
)

minio_client: Minio = Minio(
    endpoint=settings.MINIO_ENDPOINT,
    access_key=settings.MINIO_ACCESS_KEY,
    secret_key=settings.MINIO_SECRET_KEY,
    secure=settings.MINIO_SECURE,
    region=settings.MINIO_REGION,
    http_client=_http_client,
)


def check_minio_connection() -> None:
    """Verify connectivity to MinIO by listing buckets. Raises on failure."""
    # ``list_buckets`` performs an authenticated round-trip without mutating state.
    minio_client.list_buckets()
