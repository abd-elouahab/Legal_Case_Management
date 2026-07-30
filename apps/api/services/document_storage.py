"""MinIO object storage for document binaries.

The only module in the platform that moves file *content*. Everything above it
deals in metadata rows and object keys, which is what keeps
`code-standards.md`'s storage rule — business data in PostgreSQL, files in
MinIO — enforceable rather than aspirational.

Four operations, exactly the ones ``07-document-management.md`` lists:

* **Upload object** — writes one version's bytes under a key that has never been
  used before.
* **Download object** — opens the body as a stream, so a large file is never
  buffered in the API process.
* **Retrieve metadata** — the object's size and type as object storage sees it,
  used to verify a stored file is still there and intact.
* **Delete object (logical only)** — deliberately does **not** remove anything.
  See :meth:`DocumentStorageService.delete_object`.

Every MinIO failure is translated into
:class:`~core.exceptions.DocumentStorageError`, a 503 with a generic body: the
underlying S3 error names buckets, keys, and endpoints, none of which a client
should receive. The specifics travel to the log instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, NoReturn

import structlog
import urllib3
from minio import Minio
from minio.error import MinioException, S3Error

from core.config import settings
from core.exceptions import DocumentStorageError
from core.storage import minio_client

logger = structlog.get_logger(__name__)

#: Bytes read from object storage per chunk while streaming a download.
#:
#: 64 KiB is the usual sweet spot: large enough that a 25 MB document is a few
#: hundred iterations rather than thousands, small enough that the API process
#: never holds a meaningful fraction of a file in memory.
DOWNLOAD_CHUNK_SIZE = 64 * 1024

#: Buckets already confirmed to exist in this process. Object storage is shared
#: and long-lived, so the check is worth doing once rather than on every upload —
#: it is an extra network round-trip on a request that is already the slowest in
#: the API.
_ensured_buckets: set[str] = set()


@dataclass(frozen=True, slots=True)
class StoredObjectInfo:
    """What object storage knows about a stored file."""

    key: str
    size: int
    content_type: str
    last_modified: datetime | None
    etag: str | None


class ObjectStream:
    """An open object body, yielded in chunks and closed exactly once.

    Returned already-open by :meth:`DocumentStorageService.open_object` rather
    than opened lazily inside the generator, and that ordering matters: a
    streaming HTTP response has already sent ``200 OK`` by the time its body is
    iterated, so a storage failure discovered then can no longer be reported as
    a 503. Opening first means an unreachable MinIO produces a clean error
    response instead of a truncated download.
    """

    def __init__(self, response: urllib3.BaseHTTPResponse, *, key: str) -> None:
        self._response = response
        self._key = key

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._response.stream(DOWNLOAD_CHUNK_SIZE)
        finally:
            # urllib3 requires both: `close` ends the response, `release_conn`
            # returns the socket to the pool. Skipping the second leaks a
            # connection per download until the pool is exhausted.
            self._response.close()
            self._response.release_conn()

    def close(self) -> None:
        """Release the underlying connection without reading the body."""
        self._response.close()
        self._response.release_conn()


class DocumentStorageService:
    """Reads and writes document binaries in MinIO."""

    def __init__(self, client: Minio | None = None, bucket: str | None = None) -> None:
        self._client = client or minio_client
        self._bucket = bucket or settings.MINIO_DOCUMENTS_BUCKET

    @property
    def bucket(self) -> str:
        """The bucket documents are stored in."""
        return self._bucket

    # ------------------------------------------------------------- writing #

    def ensure_bucket(self) -> None:
        """Create the documents bucket if it does not exist yet.

        Idempotent, and safe to race: another process winning the creation
        returns ``BucketAlreadyOwnedByYou``, which is success as far as this
        caller is concerned.
        """
        if self._bucket in _ensured_buckets:
            return

        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                logger.info("document_bucket_created", bucket=self._bucket)
        except S3Error as exc:
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                self._fail("ensure_bucket", exc)
        except (MinioException, urllib3.exceptions.HTTPError, OSError) as exc:
            self._fail("ensure_bucket", exc)

        _ensured_buckets.add(self._bucket)

    def upload_object(
        self, *, key: str, stream: BinaryIO, size: int, content_type: str
    ) -> StoredObjectInfo:
        """Store one version's bytes under ``key``.

        The key is generated per version (see
        :func:`~core.documents.build_storage_key`), so this never overwrites a
        previous version — the guarantee is structural rather than a rule this
        method has to check.

        Raises:
            DocumentStorageError: MinIO was unreachable or refused the write.
        """
        self.ensure_bucket()

        try:
            self._client.put_object(
                self._bucket,
                key,
                data=stream,
                length=size,
                content_type=content_type,
            )
        except (S3Error, MinioException, urllib3.exceptions.HTTPError, OSError) as exc:
            self._fail("upload_object", exc, key=key)

        logger.info(
            "document_object_uploaded",
            bucket=self._bucket,
            # The key contains only identifiers — case, document, version — and
            # a generated filename, never the original name, which can identify
            # a client or a matter.
            key=key,
            size=size,
            content_type=content_type,
        )
        return StoredObjectInfo(
            key=key, size=size, content_type=content_type, last_modified=None, etag=None
        )

    # ------------------------------------------------------------- reading #

    def open_object(self, key: str) -> ObjectStream:
        """Open a stored object's body for streaming.

        Raises:
            DocumentStorageError: MinIO was unreachable, or the object is not
                there. A missing object is a *storage* fault rather than a 404:
                the metadata row says the document exists, so the file having
                vanished is an operational problem, not a client one.
        """
        try:
            response = self._client.get_object(self._bucket, key)
        except (S3Error, MinioException, urllib3.exceptions.HTTPError, OSError) as exc:
            self._fail("open_object", exc, key=key)

        return ObjectStream(response, key=key)

    def object_metadata(self, key: str) -> StoredObjectInfo:
        """Return what object storage knows about a stored file.

        Raises:
            DocumentStorageError: MinIO was unreachable, or the object is not
                there.
        """
        try:
            stat = self._client.stat_object(self._bucket, key)
        except (S3Error, MinioException, urllib3.exceptions.HTTPError, OSError) as exc:
            self._fail("object_metadata", exc, key=key)

        return StoredObjectInfo(
            key=key,
            size=stat.size or 0,
            content_type=stat.content_type or "application/octet-stream",
            last_modified=stat.last_modified,
            etag=stat.etag,
        )

    # ------------------------------------------------------------ deleting #

    def delete_object(self, key: str, *, reason: str) -> None:
        """Record a **logical** deletion. The object is not removed.

        This is not an oversight and not a stub: `code-standards.md` says never
        permanently delete a legal document without authorization, the spec says
        "do not immediately remove the file from MinIO", and `architecture.md`
        invariant 6 makes an uploaded document immutable. Deleting a document is
        therefore a metadata operation — ``documents.deleted_at`` — and the bytes
        stay put so the deletion is recoverable.

        What this method does instead is leave an audit record that the object is
        now unreferenced, which is what a future cleanup job needs in order to
        reclaim the storage safely.
        """
        logger.info(
            "document_object_logically_deleted",
            bucket=self._bucket,
            key=key,
            reason=reason,
            retained=True,
        )

    # ------------------------------------------------------------- helpers #

    def _fail(self, operation: str, exc: Exception, *, key: str | None = None) -> NoReturn:
        """Log the storage fault with its specifics, then raise a generic 503.

        Annotated ``NoReturn`` so the type checker understands that a call site
        writing ``self._fail(...)`` on its own line ends the code path — without
        it, every caller would have to add an unreachable ``raise``.
        """
        logger.error(
            "document_storage_failed",
            operation=operation,
            bucket=self._bucket,
            key=key,
            error=type(exc).__name__,
        )
        raise DocumentStorageError(
            detail=f"MinIO {operation} failed for bucket {self._bucket!r} key {key!r}: {exc}"
        )
