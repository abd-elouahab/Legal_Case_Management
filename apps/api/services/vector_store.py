"""The vector-database boundary.

``10-document-indexing.md`` requires vectors and their metadata to be persisted
in **Qdrant**, and requires the whole feature to stay usable if the database
changes: *"Allow future support for […] alternative vector databases"*. This
module is that boundary, and it is the only place in the platform that speaks
Qdrant's data model — :mod:`core.vector` owns the client and the connectivity
probe, and nothing above this file knows what a "point" or a "payload" is.

The shape mirrors :mod:`services.ocr_engine`, :mod:`services.chunking`, and
:mod:`services.embedding`:

* :class:`VectorStore` is the protocol :mod:`services.indexing` depends on;
* :class:`QdrantVectorStore` is the implementation.

**Two mechanisms together give the spec's re-indexing guarantee**, and they cover
different halves:

* every point's id is *derived* from (document, version, page, chunk) — see
  :func:`~core.indexing.chunk_point_id` — so writing the same chunk twice is an
  overwrite. That is "avoid duplicate vectors";
* :meth:`VectorStore.delete_document_version` removes a version's points before
  its replacements are written, so a re-index that produces **fewer** chunks does
  not leave the tail of the previous one behind. That is "replace outdated
  vectors".

Either alone would be insufficient, and together they make the operation
idempotent whichever order two runs interleave in.

**Nothing here searches.** The spec puts Semantic Search out of scope, and the
boundary is structural rather than a matter of discipline: this protocol has no
query method, so a retrieval feature cannot be smuggled in through it — it will
add its own read-side module against the same collection.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from core.config import settings
from core.indexing import IndexFailureCode

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class VectorStoreError(Exception):
    """The vector database could not be reached, or refused an operation.

    Deliberately **not** an :class:`~core.exceptions.AppException`: writes happen
    in a background worker with no request behind it, and the failure's
    destination is the run's ``error_code`` column, not a status line.
    """

    #: The cause, recorded on the index and used to group failures in the
    #: monitoring view.
    code: IndexFailureCode = IndexFailureCode.VECTOR_STORE_UNAVAILABLE

    def __init__(self, message: str, *, code: IndexFailureCode | None = None) -> None:
        self.code = code or self.code
        super().__init__(message)


# --------------------------------------------------------------------------- #
# What is stored
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VectorPoint:
    """One chunk as it is persisted: an identifier, a vector, and its metadata.

    The payload carries **exactly** the fields ``10-document-indexing.md`` lists
    under "Vector Storage" — document id, version, case id, page, chunk number,
    language, and timestamps — plus the chunk's text and the model that produced
    the vector.

    Two of those need justifying, because neither is in the spec's list:

    * **the text**. A future search has to return the passage it matched, and a
      result that carries only "document 3, page 7, chunk 4" would need a second
      round trip per hit to say anything a lawyer can read. The passage is
      already stored in ``ocr_pages``, so this is a copy — but it is a copy of a
      value that never changes, written by one writer, and deleted with its
      version.
    * **the embedding model**. `ai-architecture.md` states that changing models
      requires re-indexing everything; a point that does not say which model
      produced it cannot be told apart from one that does not need rebuilding.
    """

    #: Derived, never random — see :func:`~core.indexing.chunk_point_id`.
    id: uuid.UUID
    vector: list[float]
    payload: dict[str, Any]


def build_payload(
    *,
    document_id: uuid.UUID,
    document_version: int,
    case_id: uuid.UUID,
    page_number: int,
    chunk_number: int,
    text: str,
    language: str,
    embedding_model: str,
    indexed_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the metadata stored beside one vector.

    Built here rather than at the call site so that every point on the platform
    carries the same keys with the same types — a payload whose ``case_id`` is a
    ``UUID`` on one point and a ``str`` on another is a filter that silently
    matches nothing. UUIDs are stringified because that is what survives a JSON
    round trip through the wire format, and timestamps are ISO-8601 in UTC for
    the same reason.
    """
    return {
        "document_id": str(document_id),
        "document_version": document_version,
        "case_id": str(case_id),
        "page_number": page_number,
        "chunk_number": chunk_number,
        "text": text,
        "language": language,
        "embedding_model": embedding_model,
        "indexed_at": (indexed_at or datetime.now(UTC)).isoformat(),
    }


@dataclass(frozen=True, slots=True)
class CollectionInfo:
    """What a collection looks like, for the monitoring view."""

    name: str
    #: Vectors currently stored. ``None`` when the collection does not exist yet,
    #: which is distinct from zero: "nothing has been indexed" and "the
    #: collection is missing" call for different responses.
    vector_count: int | None
    dimensions: int | None
    exists: bool


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class VectorStore(Protocol):
    """What the indexing service requires of a vector database.

    Five members: prepare the collection, write points, delete a version's
    points, count them, and probe availability. **No query method**, deliberately
    — see the module docstring.
    """

    @property
    def collection(self) -> str:
        """Name of the collection vectors are written to."""
        ...

    def is_available(self) -> bool:
        """Whether the database can be reached right now."""
        ...

    def ensure_collection(self, *, dimensions: int) -> None:
        """Create the collection if it does not exist. Idempotent.

        Raises:
            VectorStoreError: the database is unreachable, or the existing
                collection has a different vector width.
        """
        ...

    def upsert(self, points: Sequence[VectorPoint]) -> int:
        """Write points, replacing any that share an identifier.

        Returns:
            How many points were written.

        Raises:
            VectorStoreError: the write failed.
        """
        ...

    def delete_document_version(self, document_id: uuid.UUID, version: int) -> None:
        """Remove every point belonging to one version of one document.

        Raises:
            VectorStoreError: the delete failed.
        """
        ...

    def count_document_version(self, document_id: uuid.UUID, version: int) -> int:
        """How many points one version of one document currently has."""
        ...

    def collection_info(self) -> CollectionInfo:
        """Describe the collection, for monitoring."""
        ...


# --------------------------------------------------------------------------- #
# Qdrant
# --------------------------------------------------------------------------- #


class QdrantVectorStore:
    """Qdrant, through the shared client in :mod:`core.vector`.

    The client is shared rather than constructed here for the same reason the
    embedder is: it holds connections, and one per indexing run would be a pool
    per document.

    Every Qdrant exception is translated into a :class:`VectorStoreError` at this
    boundary, so the service above records a cause without knowing what an
    ``UnexpectedResponse`` is — and so a driver message, which can quote a
    payload and therefore the document's text, never leaves this module.
    """

    def __init__(self, *, collection: str | None = None, client: Any | None = None) -> None:
        self._collection = collection or settings.QDRANT_COLLECTION
        # Injectable so a unit test can drive the translation logic with a stub
        # and no running Qdrant — the same reason `DocumentStorageService` and
        # the OCR engine are dependencies rather than imports at their call
        # sites.
        self._client = client
        self._ensured = False

    # ------------------------------------------------------------ identity #

    @property
    def collection(self) -> str:
        """Name of the collection vectors are written to."""
        return self._collection

    def is_available(self) -> bool:
        """Whether Qdrant answers.

        Probed rather than assumed, so an unreachable database is reported as an
        actionable failed run and a ``false`` on the monitoring endpoint.
        """
        try:
            self._qdrant().get_collections()
        except Exception:
            return False
        return True

    # ---------------------------------------------------------- collection #

    def ensure_collection(self, *, dimensions: int) -> None:
        """Create the collection at the given width if it is absent.

        Called once per run rather than at startup, deliberately: the API must
        come up whether or not Qdrant is reachable (``/ready`` reports that
        separately), and a collection created lazily on the first index is one
        fewer thing that can abort a deployment.

        **Cosine distance**, because the embedder returns unit-length vectors and
        cosine is what bge-m3 is trained for. An existing collection is left
        exactly as it is — including its width, which is *verified* rather than
        corrected: silently recreating a collection would delete every vector on
        the platform, which is never the right response to a configuration
        mistake.

        Raises:
            VectorStoreError: Qdrant is unreachable, or the collection exists
                with a different vector width.
        """
        if self._ensured:
            return

        client = self._qdrant()

        try:
            exists = bool(client.collection_exists(self._collection))
        except Exception as exc:
            raise self._unreachable("collection_exists", exc) from exc

        if exists:
            self._verify_dimensions(dimensions)
            self._ensured = True
            return

        from qdrant_client.models import Distance, VectorParams

        try:
            client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE),
            )
        except Exception as exc:
            # A concurrent run can create the collection between the check and
            # the call. That is success, not failure — verified rather than
            # assumed, so a genuine creation failure is still reported.
            if self._collection_now_exists(client):
                self._verify_dimensions(dimensions)
                self._ensured = True
                return
            raise self._unreachable("create_collection", exc) from exc

        logger.info(
            "vector_collection_created", collection=self._collection, dimensions=dimensions
        )
        self._ensured = True

    # --------------------------------------------------------------- write #

    def upsert(self, points: Sequence[VectorPoint]) -> int:
        """Write points in bounded batches, replacing any that share an id.

        Batched because the spec requires it under "Performance": a 100-page
        bundle is several hundred vectors, and one request carrying all of them
        is a payload large enough to time out on a slow link — while one request
        per vector pays a round trip each. ``QDRANT_UPSERT_BATCH_SIZE`` is the
        middle.

        ``wait=True`` on every batch: the run reports ``indexed`` when it
        finishes, and reporting that before Qdrant has durably accepted the
        points would make the status a promise rather than a fact.

        Raises:
            VectorStoreError: a batch could not be written. Earlier batches stay
                written — which is safe, because every point id is derived, so a
                retry rewrites exactly the same points rather than duplicating
                them.
        """
        if not points:
            return 0

        client = self._qdrant()
        from qdrant_client.models import PointStruct

        written = 0
        batch_size = max(1, settings.QDRANT_UPSERT_BATCH_SIZE)

        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            try:
                client.upsert(
                    collection_name=self._collection,
                    points=[
                        PointStruct(id=str(point.id), vector=point.vector, payload=point.payload)
                        for point in batch
                    ],
                    wait=True,
                )
            except Exception as exc:
                raise self._unreachable("upsert", exc) from exc
            written += len(batch)

        return written

    def delete_document_version(self, document_id: uuid.UUID, version: int) -> None:
        """Remove every point belonging to one version of one document.

        Filtered by payload rather than by a list of identifiers, deliberately:
        the identifiers of the *previous* run's chunks are not knowable from the
        current one (it may produce fewer), and reconstructing them would mean
        storing a chunk count and trusting it. A filter deletes what is actually
        there, which is the only formulation that is correct when the two runs
        disagree about how many chunks the document has.

        Raises:
            VectorStoreError: the delete failed.
        """
        client = self._qdrant()

        try:
            if not client.collection_exists(self._collection):
                # Nothing to remove. Not an error: a first index has no
                # predecessor, and the delete-then-write order means this is the
                # normal path for every new document.
                return

            from qdrant_client.models import FilterSelector

            client.delete(
                collection_name=self._collection,
                # `delete` takes a *selector*; `count` takes the bare filter.
                # Wrapping happens here rather than in `_version_filter` because
                # the two calls genuinely want different shapes, and a helper
                # that returned the wrapped form made `count` fail against a real
                # Qdrant while every stub-driven test passed.
                points_selector=FilterSelector(
                    filter=self._version_filter(document_id, version)
                ),
                wait=True,
            )
        except Exception as exc:
            raise self._unreachable("delete", exc) from exc

    # ---------------------------------------------------------------- read #

    def count_document_version(self, document_id: uuid.UUID, version: int) -> int:
        """How many points one version of one document currently has.

        A verification aid rather than a search: it answers "is what I just wrote
        actually there", which is what makes the re-indexing guarantee testable
        against a live database rather than only against the code.

        Returns ``0`` rather than raising when the collection does not exist —
        the honest answer to "how many vectors does this document have" when
        nothing has ever been indexed.
        """
        client = self._qdrant()

        try:
            if not client.collection_exists(self._collection):
                return 0
            outcome = client.count(
                collection_name=self._collection,
                count_filter=self._version_filter(document_id, version),
                exact=True,
            )
        except Exception as exc:
            raise self._unreachable("count", exc) from exc

        return int(getattr(outcome, "count", 0))

    def collection_info(self) -> CollectionInfo:
        """Describe the collection, for the monitoring endpoint.

        Never raises: monitoring must still render when the vector database is
        down — an unreachable Qdrant is precisely what an operator opens the page
        to find out.
        """
        try:
            client = self._qdrant()
            if not client.collection_exists(self._collection):
                return CollectionInfo(
                    name=self._collection, vector_count=None, dimensions=None, exists=False
                )

            described = client.get_collection(self._collection)
            return CollectionInfo(
                name=self._collection,
                vector_count=int(getattr(described, "points_count", 0) or 0),
                dimensions=self._configured_dimensions(described),
                exists=True,
            )
        except Exception:
            logger.warning("vector_collection_unreadable", collection=self._collection)
            return CollectionInfo(
                name=self._collection, vector_count=None, dimensions=None, exists=False
            )

    # ------------------------------------------------------------- helpers #

    def _qdrant(self) -> Any:
        """The shared Qdrant client, or the injected stub."""
        if self._client is not None:
            return self._client

        from core.vector import qdrant_client

        return qdrant_client

    @staticmethod
    def _version_filter(document_id: uuid.UUID, version: int) -> Any:
        """A filter matching exactly one version of one document.

        Both conditions, not just the document: a replacement's vectors and its
        predecessor's live side by side in one collection, and deleting by
        document alone would take out the version that is still current.

        Returns the **bare** ``Filter``. ``count`` takes it as-is;
        ``delete_document_version`` wraps it in a ``FilterSelector``, which is the
        only shape that call accepts.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        return Filter(
            must=[
                FieldCondition(key="document_id", match=MatchValue(value=str(document_id))),
                FieldCondition(key="document_version", match=MatchValue(value=version)),
            ]
        )

    def _collection_now_exists(self, client: Any) -> bool:
        """Whether the collection exists, swallowing a probe failure."""
        try:
            return bool(client.collection_exists(self._collection))
        except Exception:
            return False

    def _verify_dimensions(self, dimensions: int) -> None:
        """Refuse to write into a collection built for a different vector width.

        Raises:
            VectorStoreError: the widths disagree. Reported rather than repaired:
                the fix is a re-index of the whole platform under the new model,
                which is an operator's decision and not something an indexing run
                may make on its behalf.
        """
        try:
            described = self._qdrant().get_collection(self._collection)
        except Exception:
            # Unreadable rather than mismatched. Left to the write to report, so
            # a transient blip does not masquerade as a configuration error.
            return

        actual = self._configured_dimensions(described)
        if actual is not None and actual != dimensions:
            logger.error(
                "vector_collection_dimension_mismatch",
                collection=self._collection,
                configured=dimensions,
                actual=actual,
            )
            raise VectorStoreError(
                f"Collection {self._collection!r} stores {actual}-dimensional vectors, "
                f"but this deployment produces {dimensions}."
            )

    @staticmethod
    def _configured_dimensions(described: Any) -> int | None:
        """Read a collection's vector width out of its description.

        Defensive by necessity: the client's description object has changed shape
        across versions, and an unnamed single-vector configuration is nested
        differently from a named one. A width this cannot read is ``None``, which
        the callers treat as "not verifiable" rather than as a mismatch.
        """
        try:
            params = described.config.params.vectors
        except AttributeError:
            return None

        size = getattr(params, "size", None)
        if size is not None:
            return int(size)

        if isinstance(params, dict):
            for entry in params.values():
                entry_size = getattr(entry, "size", None)
                if entry_size is not None:
                    return int(entry_size)

        return None

    def _unreachable(self, operation: str, exc: Exception) -> VectorStoreError:
        """Translate a driver failure, keeping its message out of the log.

        The exception's own text can include the payload it was writing, and the
        payload holds the document's text. The *type* is logged instead, which is
        what an operator needs to tell a connection refusal from a rejected
        request.
        """
        logger.error(
            "vector_store_operation_failed",
            collection=self._collection,
            operation=operation,
            error_type=type(exc).__name__,
        )
        return VectorStoreError(f"The search index could not complete {operation!r}.")


def get_vector_store(collection: str | None = None) -> VectorStore:
    """Build the configured vector store.

    A plain constructor call today, because Qdrant is the only implementation and
    `architecture.md` names it. It exists as a function so that adding a second
    store is a registry here rather than an edit at every call site — the same
    shape as :func:`~services.ocr_engine.get_ocr_engine`.
    """
    return QdrantVectorStore(collection=collection)


__all__ = [
    "CollectionInfo",
    "QdrantVectorStore",
    "VectorPoint",
    "VectorStore",
    "VectorStoreError",
    "build_payload",
    "get_vector_store",
]
