"""Document indexing business logic.

Owns the workflow ``10-document-indexing.md`` defines: scheduling an index when
extraction completes, running it in the background, splitting the text into
chunks, embedding them, persisting the vectors and their metadata in Qdrant,
tracking the run's lifecycle, and letting an authorized user rebuild it.

Scope boundaries, kept deliberately sharp:

* **Chunking is not done here.** :mod:`services.chunking` owns it, behind a
  protocol, so a different splitter is one class.
* **Embedding is not done here.** :mod:`services.embedding` owns it, behind a
  protocol, so a different model — or a different runtime for the same model — is
  one class.
* **Vector persistence is not done here.** :mod:`services.vector_store` owns it,
  behind a protocol, so an alternative vector database is one class.
* **Scheduling is not done here.** :mod:`services.job_queue` owns it, behind a
  protocol, so Celery or Trigger.dev can replace the in-process pool.
* **Capability authorization is not decided here.** Whether the caller may use
  the indexing endpoints is settled by the dependencies in
  :mod:`api.authorization` before a request reaches this service.
* **Per-resource authorization is applied here**, through
  :class:`~services.indexing_access.IndexingAccessPolicy`, because it needs the
  document and its case — a dependency cannot know whether a lawyer is assigned
  to a case it has not loaded.
* **Nothing about search, retrieval, ranking, RAG, a prompt, or an LLM appears
  anywhere in this module.** The spec puts all of them out of scope and says
  indexing *"must remain independent from retrieval"*; the boundary is
  structural, because the only vector-database operations this service can reach
  are write, delete, and count.

What it does own are the rules nothing else can express: that an index belongs to
a *document version*, that only one worker may build it, that a status may only
move along the legal transitions, that a failure never touches the extracted text
or the document, that re-indexing is idempotent, and that an index inherits the
document's permissions.

It also **publishes to the timeline** — started, completed, failed, and retried
are the indexing half of a document's history. As with the case, document, and
OCR services, publication happens after the change is committed, describes rather
than decides, and can never fail the operation that caused it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from core.config import settings
from core.exceptions import (
    DocumentIndexNotFoundError,
    DocumentNotFoundError,
    DocumentVersionNotFoundError,
    IndexingAlreadyRunningError,
    IndexingDisabledError,
    IndexingNotReadyError,
    InvalidIndexTransitionError,
)
from core.indexing import (
    ChunkLocation,
    IndexFailureCode,
    can_reindex,
    can_transition,
    dominant_language,
    failure_message,
    normalize_error_message,
    success_rate,
)
from models.document import Document, DocumentVersion
from models.indexing import DocumentIndex, IndexStatus
from models.ocr import OcrResult, OcrStatus
from models.timeline import TimelineEventType
from models.user import User
from repositories.document import DocumentRepository
from repositories.indexing import IndexingRepository, IndexStatusCounts
from repositories.ocr import OcrRepository
from schemas.indexing import IndexListQuery
from services.chunking import Chunker, ChunkingError, TextChunk, get_chunker
from services.embedding import Embedder, EmbeddingError, get_embedder
from services.indexing_access import IndexingAccessPolicy
from services.job_queue import JobQueue, NullJobQueue
from services.timeline import NullTimelineRecorder, TimelineRecorder
from services.vector_store import (
    CollectionInfo,
    VectorPoint,
    VectorStore,
    VectorStoreError,
    build_payload,
    get_vector_store,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IndexJob:
    """One unit of background indexing work.

    Carries **identifiers only**, never an ORM instance. A job crosses a thread
    boundary and outlives the request that created it, and a detached SQLAlchemy
    object on the far side of either is a source of stale reads and cross-session
    errors. The worker re-reads the row it is about to change, which is also what
    makes the claim honest.
    """

    index_id: uuid.UUID
    document_id: uuid.UUID
    document_version: int


@dataclass(frozen=True, slots=True)
class IndexPageResult:
    """One page of indexing runs together with the total matching the filters."""

    results: list[DocumentIndex]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class IndexMetrics:
    """Platform-wide indexing health, as the monitoring endpoint reports it."""

    counts: IndexStatusCounts
    failures_by_code: dict[str, int]
    window_days: int | None
    embedding_model: str
    embedding_dimensions: int
    embedding_available: bool
    chunker: str
    chunk_size: int
    chunk_overlap: int
    collection: CollectionInfo
    vector_store_available: bool
    enabled: bool

    @property
    def success_rate(self) -> float:
        """Share of finished runs that produced an index."""
        return success_rate(self.counts.indexed, self.counts.failed)

    @property
    def failure_rate(self) -> float:
        """Share of finished runs that failed.

        Derived as the complement of the success rate rather than computed
        separately, so the two always sum to 100 for a non-empty window — two
        independent roundings would not.
        """
        if self.counts.finished <= 0:
            return 0.0
        return round(100.0 - self.success_rate, 2)


class IndexScheduler(Protocol):
    """The narrow half of indexing that OCR Processing depends on.

    The OCR service needs to say "text has been extracted, index it". It has no
    business reading an index, authorizing one, or knowing what a vector is.
    Depending on this protocol rather than on :class:`IndexingService` states
    that in the type system, and lets a unit test for extraction substitute a
    scheduler with no embedder, no queue, and no Qdrant — exactly the shape
    :class:`~services.ocr.OcrScheduler` has for Document Management.
    """

    def schedule_for_ocr_result(
        self, result: OcrResult, *, actor: User | None = None
    ) -> DocumentIndex | None:
        """Queue indexing for a completed extraction, if applicable."""
        ...


class NullIndexScheduler:
    """A scheduler that queues nothing.

    The default for a service constructed without indexing — a script, a
    migration helper, or a unit test that is not about search. Same role, and
    same reasoning, as :class:`~services.ocr.NullOcrScheduler`: publishing code
    stays a plain call with no ``if self._indexing`` guard at every site.
    """

    def schedule_for_ocr_result(
        self, result: OcrResult, *, actor: User | None = None
    ) -> DocumentIndex | None:
        """Discard the request and return ``None``."""
        return None


class IndexingService:
    """Coordinates the document-indexing lifecycle."""

    def __init__(
        self,
        indexes: IndexingRepository,
        documents: DocumentRepository,
        ocr: OcrRepository,
        chunker: Chunker | None = None,
        embedder: Embedder | None = None,
        vectors: VectorStore | None = None,
        queue: JobQueue[IndexJob] | None = None,
        access: IndexingAccessPolicy | None = None,
        *,
        timeline: TimelineRecorder | None = None,
    ) -> None:
        self._indexes = indexes
        self._documents = documents
        self._ocr = ocr
        self._chunker = chunker or get_chunker()
        self._embedder = embedder or get_embedder()
        self._vectors = vectors or get_vector_store()
        # See `OcrService.__init__` for why the defaults record and queue
        # nothing; the application wires the real collaborators in
        # `api.deps.get_indexing_service`.
        self._queue: JobQueue[IndexJob] = queue or NullJobQueue(name="indexing")
        self._access = access or IndexingAccessPolicy()
        self._timeline: TimelineRecorder = timeline or NullTimelineRecorder()

    # ---------------------------------------------------------- scheduling #

    def schedule_for_ocr_result(
        self, result: OcrResult, *, actor: User | None = None
    ) -> DocumentIndex | None:
        """Queue indexing for a completed extraction.

        Called by :class:`~services.ocr.OcrService` after a run reaches
        ``completed`` — which is exactly where ``10-document-indexing.md``'s flow
        diagram begins. **It never raises and never blocks**: the text is already
        persisted and the extraction has already succeeded, so a queueing problem
        must not turn a completed extraction into a failure.

        Returns ``None`` — with a reason in the log — when there is nothing to
        schedule:

        * indexing is disabled for this deployment;
        * the run did not complete, or produced no text. No row is created,
          because an index that could never be anything but "there was nothing to
          index" is not a record worth keeping, and the read endpoint says so
          explicitly instead.

        **Idempotent**, as the spec requires: a version that already has an index
        returns it untouched rather than creating a second. The unique constraint
        on ``(document_id, document_version)`` is what makes that true under a
        race rather than merely likely.
        """
        try:
            return self._schedule(result, actor=actor, trigger="ocr_completed")
        except Exception:
            logger.exception(
                "indexing_scheduling_failed",
                ocr_result_id=str(result.id),
                document_id=str(result.document_id),
                document_version=result.document_version,
            )
            return None

    def _schedule(
        self, result: OcrResult, *, actor: User | None, trigger: str
    ) -> DocumentIndex | None:
        """Create or reuse the index for a document version, then queue it."""
        if not settings.INDEXING_ENABLED:
            logger.info(
                "indexing_not_scheduled",
                reason="disabled",
                document_id=str(result.document_id),
                document_version=result.document_version,
            )
            return None

        if result.status is not OcrStatus.COMPLETED:
            logger.info(
                "indexing_not_scheduled",
                reason="ocr_not_completed",
                document_id=str(result.document_id),
                document_version=result.document_version,
                ocr_status=result.status.value,
            )
            return None

        if not self._has_text(result):
            logger.info(
                "indexing_not_scheduled",
                reason="no_text",
                document_id=str(result.document_id),
                document_version=result.document_version,
            )
            return None

        existing = self._indexes.get_for_version(result.document_id, result.document_version)
        if existing is not None:
            logger.info(
                "indexing_not_scheduled",
                reason="already_recorded",
                document_id=str(result.document_id),
                document_version=result.document_version,
                index_id=str(existing.id),
                index_status=existing.status.value,
            )
            return existing

        index = self._indexes.create(
            DocumentIndex(
                id=uuid.uuid4(),
                document_id=result.document_id,
                document_version=result.document_version,
                case_id=result.document.case_id,
                status=IndexStatus.PENDING,
                requested_by=actor.id if actor is not None else None,
            )
        )

        logger.info("indexing_requested", trigger=trigger, **self._event(index, actor=actor))
        self._enqueue(index)
        return index

    def requeue_pending(self, limit: int = 100) -> int:
        """Re-queue runs left ``pending`` by a previous process.

        A job lives in an in-process queue (see :mod:`services.job_queue`), so a
        restart loses the schedule but not the *record* of the work — the row is
        still ``pending``, and nothing else would ever pick it up. Running this at
        startup is what closes that gap, and it is safe to run repeatedly: a run
        that has since been claimed is no longer pending, and the claim itself is
        atomic, so a double-queued job processes exactly once.

        Returns:
            How many jobs were re-queued.
        """
        if not settings.INDEXING_ENABLED:
            return 0

        pending = self._indexes.pending_indexes(limit=limit)
        for index in pending:
            self._enqueue(index)

        if pending:
            logger.info("indexing_pending_requeued", count=len(pending))
        return len(pending)

    def _enqueue(self, index: DocumentIndex) -> None:
        """Hand a committed run to the background queue."""
        self._queue.enqueue(
            IndexJob(
                index_id=index.id,
                document_id=index.document_id,
                document_version=index.document_version,
            )
        )

    # ------------------------------------------------------------- reading #

    def get_index(
        self, document_id: uuid.UUID, *, actor: User, version: int | None = None
    ) -> DocumentIndex:
        """Return the index for a document, current version by default.

        Raises:
            DocumentNotFoundError: no live document has this identifier.
            IndexAccessDeniedError: the caller is not party to its case.
            DocumentIndexNotFoundError: the document has no index for that
                version.
        """
        document = self._require_document(document_id)
        self._access.require_document_access(actor, document)
        return self._require_index(document, version)

    def list_document_indexes(
        self, document_id: uuid.UUID, *, actor: User
    ) -> list[DocumentIndex]:
        """Every index recorded for a document, oldest version first.

        A document accumulates one index per version, because a replacement is
        new text that needs its own vectors while the previous version's index
        stays valid for as long as that version is readable.
        """
        document = self._require_document(document_id)
        self._access.require_document_access(actor, document)
        return self._indexes.list_for_document(document.id)

    def list_indexes(self, query: IndexListQuery, *, actor: User) -> IndexPageResult:
        """Return one page of indexing runs the caller is entitled to see.

        Filtering, sorting, pagination, **and the case scope** are all applied in
        the database, so the cost of a page does not grow with the number of
        indexed documents and the totals describe only what the caller may
        access.
        """
        results, total = self._indexes.list_indexes(
            query, visible_to=self._access.visibility_scope(actor)
        )
        return IndexPageResult(
            results=results, total=total, page=query.page, page_size=query.page_size
        )

    def metrics(self, *, window_days: int | None = None) -> IndexMetrics:
        """Aggregate indexing health for the monitoring endpoint.

        No per-resource scope is applied, deliberately: this is an operational
        view of the *pipeline*, gated on the administrative ``indexing:monitor``
        permission, and it reports counts, timings, and configuration only —
        never a document, a case, or a fragment of text. Scoping it per caller
        would make the platform's success rate mean something different for each
        of them, which is not a health metric.

        The collection description is read from the vector database rather than
        derived from the rows, because they answer different questions: the rows
        say what this platform believes it indexed, and Qdrant says what is
        actually stored. A divergence between the two is precisely what an
        operator opens this page to find.
        """
        since = (
            datetime.now(UTC) - timedelta(days=window_days) if window_days is not None else None
        )
        collection = self._vectors.collection_info()

        return IndexMetrics(
            counts=self._indexes.metrics(since=since),
            failures_by_code=self._indexes.failure_breakdown(since=since),
            window_days=window_days,
            embedding_model=self._embedder.model,
            embedding_dimensions=self._embedder.dimensions,
            embedding_available=self._embedder.is_available(),
            chunker=self._chunker.name,
            chunk_size=self._chunker.chunk_size,
            chunk_overlap=self._chunker.chunk_overlap,
            collection=collection,
            vector_store_available=self._vectors.is_available(),
            enabled=settings.INDEXING_ENABLED,
        )

    # ------------------------------------------------------------ re-index #

    def reindex(
        self, document_id: uuid.UUID, *, actor: User, version: int | None = None
    ) -> DocumentIndex:
        """Queue a fresh index for a document whose text is already extracted.

        Takes a document, not a file and not a body of text: nothing is uploaded,
        nothing stored is touched, and the extracted text is read back from
        ``ocr_pages`` exactly as the first run read it.

        The existing run is **re-used**, not replaced: same row, status back to
        ``pending``, timing and failure fields cleared, ``attempt_count``
        preserved. That is the idempotency the spec requires — re-indexing the
        same document version can never produce a second, contradictory record of
        it, and the vectors it writes overwrite rather than duplicate (see
        :func:`~core.indexing.chunk_point_id`).

        A version with no index at all is bootstrapped here rather than refused:
        a document extracted while indexing was disabled, or before this feature
        existed, is exactly the case a re-index should be able to fix.

        Raises:
            DocumentNotFoundError: no live document has this identifier.
            IndexAccessDeniedError: the caller is not party to its case.
            DocumentVersionNotFoundError: the document has no such version.
            IndexingDisabledError: indexing is switched off for this deployment.
            IndexingNotReadyError: that version has no completed extraction.
            IndexingAlreadyRunningError: a run is already queued or indexing.
        """
        document = self._require_document(document_id)
        self._access.require_document_access(actor, document)

        entry = self._resolve_version(document, version)

        if not settings.INDEXING_ENABLED:
            logger.info(
                "indexing_reindex_rejected",
                reason="disabled",
                document_id=str(document.id),
                document_version=entry.version,
                actor_id=str(actor.id),
            )
            raise IndexingDisabledError

        # The precondition the spec's flow diagram states: indexing begins at
        # "OCR Completed". Checked here rather than only in the worker so the
        # caller learns *now* that there is nothing to index, instead of watching
        # a run they requested fail a minute later for a reason nothing about
        # their request could have changed.
        ocr_result = self._ocr.get_for_version(document.id, entry.version)
        if ocr_result is None or ocr_result.status is not OcrStatus.COMPLETED:
            logger.info(
                "indexing_reindex_rejected",
                reason="ocr_not_completed",
                document_id=str(document.id),
                document_version=entry.version,
                ocr_status=ocr_result.status.value if ocr_result is not None else None,
                actor_id=str(actor.id),
            )
            raise IndexingNotReadyError(
                ocr_result.status.value if ocr_result is not None else None
            )

        existing = self._indexes.get_for_version(document.id, entry.version)

        if existing is None:
            # Nothing to rebuild — schedule the first run instead. Returned
            # through the same path, so a client sees one shape whether it asked
            # to rebuild a failure or to index a document never indexed at all.
            created = self._indexes.create(
                DocumentIndex(
                    id=uuid.uuid4(),
                    document_id=document.id,
                    document_version=entry.version,
                    case_id=document.case_id,
                    status=IndexStatus.PENDING,
                    requested_by=actor.id,
                )
            )
            logger.info(
                "indexing_requested", trigger="reindex", **self._event(created, actor=actor)
            )
            self._publish_retry(document, created, actor=actor, entry=entry)
            self._enqueue(created)
            return created

        if not can_reindex(existing.status):
            # `_event` already carries `index_status`, which is the whole reason
            # this rejection happened.
            logger.info(
                "indexing_reindex_rejected",
                reason="already_running",
                **self._event(existing, actor=actor),
            )
            raise IndexingAlreadyRunningError(existing.status.value)

        previous_status = existing.status
        self._transition(existing, IndexStatus.PENDING)
        # Cleared rather than kept: they describe the *previous* attempt, and
        # leaving them would make a queued run read as though it had already
        # failed. `attempt_count` is deliberately not reset — how many times a
        # document has been attempted is exactly what a re-index should preserve.
        existing.started_at = None
        existing.finished_at = None
        existing.duration_ms = None
        existing.error_code = None
        existing.error_message = None
        existing.requested_by = actor.id
        saved = self._indexes.save(existing)

        logger.info(
            "indexing_retried",
            previous_status=previous_status.value,
            attempt=saved.attempt_count,
            **self._event(saved, actor=actor),
        )
        self._publish_retry(document, saved, actor=actor, entry=entry)
        self._enqueue(saved)
        return saved

    # ----------------------------------------------------------- processing #

    def process(self, job: IndexJob) -> DocumentIndex | None:
        """Run one queued index to completion.

        The worker's entry point, and the only method here that is not driven by
        an HTTP request. It runs on a background thread with its own database
        session, so it re-reads everything it needs rather than trusting anything
        the request that queued it had loaded.

        The sequence is the spec's processing flow, in order:

        1. **Claim the run** — a conditional ``UPDATE`` from ``pending`` to
           ``indexing``. A run someone else already claimed returns here, which
           is what keeps two workers from indexing the same document version.
        2. Announce the start, on the timeline and in the log.
        3. **Load the OCR text**, page by page, in reading order.
        4. **Split into chunks**, preserving page order and page numbers.
        5. **Generate embeddings**, one per chunk.
        6. **Store in Qdrant** — previous vectors for this version deleted first,
           new ones upserted under derived identifiers.
        7. **Update the index status**, recording what was produced.

        **Never raises.** Every failure becomes a ``failed`` run carrying a
        machine-readable cause, because that is what the spec asks for: the
        extracted text stays intact, the original document stays available, and
        the run stays retryable.
        """
        index = self._indexes.get_by_id(job.index_id)
        if index is None:
            logger.warning("indexing_job_orphaned", index_id=str(job.index_id))
            return None

        started_at = datetime.now(UTC)
        if not self._indexes.claim(index.id, started_at=started_at):
            # Someone else got there first, or the run has already finished.
            # Not an error: this is the guard doing its job.
            logger.info(
                "indexing_job_skipped",
                reason="not_claimable",
                index_id=str(index.id),
                index_status=index.status.value,
            )
            return None

        index = self._indexes.get_by_id(index.id)
        if index is None:  # pragma: no cover - the row was just updated
            return None

        document = index.document
        actor = index.requester

        logger.info(
            "indexing_started", attempt=index.attempt_count, **self._event(index, actor=actor)
        )
        self._publish(
            document,
            index,
            TimelineEventType.INDEXING_STARTED,
            actor=actor,
            description=(
                f'Search indexing started for "{document.original_filename}" '
                f"(version {index.document_version})."
            ),
        )

        monotonic_start = time.monotonic()

        try:
            pages = self._load_text(index)
            chunks = self._chunk(pages, monotonic_start=monotonic_start)
            vectors = self._embed(chunks, monotonic_start=monotonic_start)
            written = self._persist(index, chunks, vectors)
        except _IndexRunFailed as failure:
            return self._fail(
                index,
                document,
                code=failure.code,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )
        except (ChunkingError, EmbeddingError, VectorStoreError) as exc:
            return self._fail(
                index,
                document,
                code=exc.code,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )
        except Exception:
            # A fault nothing above anticipated. Logged with a traceback, then
            # recorded as an ordinary failed run: an unexpected bug must not
            # leave a document stuck at `indexing` forever, which is the one
            # state nothing can recover from without operator intervention.
            logger.exception(
                "indexing_unexpected_failure",
                index_id=str(index.id),
                document_id=str(index.document_id),
            )
            return self._fail(
                index,
                document,
                code=IndexFailureCode.UNKNOWN,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )

        return self._complete(
            index,
            document,
            chunks,
            written=written,
            pages=pages,
            actor=actor,
            elapsed_ms=self._elapsed_ms(monotonic_start),
        )

    # ---------------------------------------------------------- the stages #

    def _load_text(self, index: DocumentIndex) -> list[str]:
        """Read the extracted text for this document version, in page order.

        The spec's "Load OCR Text" step, and the only place indexing touches the
        OCR feature's data. It reads what OCR persisted and writes nothing back —
        *"failures must […] preserve OCR data"* is structural here rather than a
        matter of care, because this service holds no write path to either OCR
        table.

        Raises:
            _IndexRunFailed: the version has no completed extraction, or its
                text is empty.
        """
        result = self._ocr.get_for_version(index.document_id, index.document_version)

        if result is None or result.status is not OcrStatus.COMPLETED:
            logger.warning(
                "indexing_text_unavailable",
                index_id=str(index.id),
                document_id=str(index.document_id),
                document_version=index.document_version,
                ocr_status=result.status.value if result is not None else None,
            )
            raise _IndexRunFailed(IndexFailureCode.INVALID_OCR_OUTPUT)

        pages = [page.text for page in result.pages]
        if not any(page.strip() for page in pages):
            # Completed, but empty: every page was blank. A legitimate extraction
            # outcome and an impossible index — there is nothing to embed, and
            # recording it as a success would claim the document is searchable.
            logger.warning(
                "indexing_text_empty",
                index_id=str(index.id),
                document_id=str(index.document_id),
                document_version=index.document_version,
            )
            raise _IndexRunFailed(IndexFailureCode.INVALID_OCR_OUTPUT)

        return pages

    def _chunk(self, pages: list[str], *, monotonic_start: float) -> list[TextChunk]:
        """Split the pages into passages, bounded by ``INDEX_MAX_CHUNKS``.

        The cap is the spec's "large documents" requirement met honestly: a
        900-page bundle produces thousands of passages, and embedding all of them
        would exceed any deadline. Truncating produces a *partial* index and says
        so in the log, which is strictly better than a timeout that produces
        none — exactly the reasoning behind ``OCR_MAX_PAGES``.

        Raises:
            _IndexRunFailed: nothing indexable came out, or the deadline passed.
            ChunkingError: the splitter failed.
        """
        chunks = self._chunker.split(pages)

        if not chunks:
            # Text that is all page numbers, rules, and stamps. Distinct from
            # "no text at all", which `_load_text` already rejected, but with the
            # same outcome: there is nothing worth a vector.
            raise _IndexRunFailed(IndexFailureCode.CHUNKING_FAILURE)

        if len(chunks) > settings.INDEX_MAX_CHUNKS:
            logger.warning(
                "indexing_chunks_truncated",
                produced=len(chunks),
                limit=settings.INDEX_MAX_CHUNKS,
            )
            chunks = chunks[: settings.INDEX_MAX_CHUNKS]

        self._check_deadline(monotonic_start)
        return chunks

    def _embed(self, chunks: list[TextChunk], *, monotonic_start: float) -> list[list[float]]:
        """Generate one embedding per chunk, in the same order.

        Order is the contract with :meth:`_persist`: the vectors are paired back
        with their chunks by position, so a reordering would attach every
        passage's vector to a different passage. The embedder promises it, and a
        test asserts it.

        Raises:
            _IndexRunFailed: the deadline passed, or the counts disagree.
            EmbeddingError: the model failed.
        """
        batch = self._embedder.embed([chunk.text for chunk in chunks])

        if len(batch.vectors) != len(chunks):  # pragma: no cover - embedder contract
            logger.error(
                "indexing_embedding_count_mismatch",
                chunks=len(chunks),
                vectors=len(batch.vectors),
            )
            raise _IndexRunFailed(IndexFailureCode.EMBEDDING_FAILURE)

        self._check_deadline(monotonic_start)
        return batch.vectors

    def _persist(
        self, index: DocumentIndex, chunks: list[TextChunk], vectors: list[list[float]]
    ) -> int:
        """Write the vectors and their metadata, replacing this version's previous ones.

        **Delete before write, and both scoped to this document *version*.** The
        delete is what makes a re-index that produces fewer chunks complete
        rather than leaving the tail of the previous run behind; the derived
        point ids are what make writing the same chunk twice an overwrite. The
        two together are the spec's "avoid duplicate vectors / replace outdated
        vectors / the operation must be idempotent", and neither alone suffices —
        see :func:`~core.indexing.chunk_point_id`.

        Scoping both to the version rather than to the document is what lets a
        replacement's index be built without destroying the previous version's,
        which is still the right answer for anyone reading that version.

        Raises:
            VectorStoreError: the collection could not be prepared, or a write
                failed.
        """
        self._vectors.ensure_collection(dimensions=self._embedder.dimensions)
        self._vectors.delete_document_version(index.document_id, index.document_version)

        indexed_at = datetime.now(UTC)
        points = [
            VectorPoint(
                id=ChunkLocation(
                    document_id=index.document_id,
                    document_version=index.document_version,
                    case_id=index.case_id,
                    page_number=chunk.page_number,
                    chunk_number=chunk.chunk_number,
                ).point_id,
                vector=vector,
                payload=build_payload(
                    document_id=index.document_id,
                    document_version=index.document_version,
                    case_id=index.case_id,
                    page_number=chunk.page_number,
                    chunk_number=chunk.chunk_number,
                    text=chunk.text,
                    language=chunk.language,
                    embedding_model=self._embedder.model,
                    indexed_at=indexed_at,
                ),
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

        return self._vectors.upsert(points)

    # ------------------------------------------------------------- helpers #

    def _complete(
        self,
        index: DocumentIndex,
        document: Document,
        chunks: list[TextChunk],
        *,
        written: int,
        pages: list[str],
        actor: User | None,
        elapsed_ms: int,
    ) -> DocumentIndex:
        """Record what the run produced and mark it indexed."""
        self._transition(index, IndexStatus.INDEXED)

        index.chunk_count = written
        index.page_count = len({chunk.page_number for chunk in chunks})
        index.character_count = sum(len(page) for page in pages)
        index.embedding_model = self._embedder.model
        index.embedding_dimensions = self._embedder.dimensions
        index.vector_collection = self._vectors.collection
        index.chunk_size = self._chunker.chunk_size
        index.chunk_overlap = self._chunker.chunk_overlap
        index.detected_language = dominant_language(chunk.language for chunk in chunks)
        index.finished_at = datetime.now(UTC)
        index.duration_ms = elapsed_ms
        index.error_code = None
        index.error_message = None

        saved = self._save(index)

        logger.info(
            "indexing_completed",
            chunk_count=saved.chunk_count,
            page_count=saved.page_count,
            # The *size* of the text, never the text: `code-standards.md` and the
            # spec both forbid logging document contents, and a character count
            # is what an operator actually needs to spot an empty index.
            character_count=saved.character_count,
            embedding_model=saved.embedding_model,
            duration_ms=saved.duration_ms,
            **self._event(saved, actor=actor),
        )
        self._publish(
            document,
            saved,
            TimelineEventType.INDEXING_COMPLETED,
            actor=actor,
            description=(
                f'"{document.original_filename}" is now searchable — '
                f"{saved.chunk_count} passage{'s' if saved.chunk_count != 1 else ''} indexed."
            ),
            extra={
                "chunk_count": saved.chunk_count,
                "page_count": saved.page_count,
                "character_count": saved.character_count,
                "embedding_model": saved.embedding_model,
                "duration_ms": saved.duration_ms,
            },
        )
        return saved

    def _fail(
        self,
        index: DocumentIndex,
        document: Document,
        *,
        code: IndexFailureCode,
        actor: User | None,
        elapsed_ms: int,
    ) -> DocumentIndex:
        """Record a failed run, leaving the extracted text and the document untouched.

        The guarantees the spec names hold structurally rather than by care: this
        method writes to ``document_indexes`` only, so it *cannot* delete an
        uploaded file, corrupt a document's metadata, or lose a page of OCR text
        — it has no write path to any of them.

        Vectors already written by this attempt are deliberately **left in
        place**. They are correct passages of the current version under derived
        identifiers, so a retry overwrites them; deleting them would mean a
        failure at chunk 900 of 1000 throws away 899 good vectors, and a partial
        index is strictly more useful than none while the failure is
        investigated.
        """
        self._transition(index, IndexStatus.FAILED)
        index.finished_at = datetime.now(UTC)
        index.duration_ms = elapsed_ms
        index.error_code = code.value
        index.error_message = normalize_error_message(failure_message(code))

        saved = self._save(index)

        logger.warning(
            "indexing_failed",
            error_code=code.value,
            attempt=saved.attempt_count,
            duration_ms=saved.duration_ms,
            **self._event(saved, actor=actor),
        )
        self._publish(
            document,
            saved,
            TimelineEventType.INDEXING_FAILED,
            actor=actor,
            description=(
                f'Search indexing failed for "{document.original_filename}" — '
                f"{saved.error_message}"
            ),
            extra={"error_code": code.value, "attempt": saved.attempt_count},
        )
        return saved

    def _save(self, index: DocumentIndex) -> DocumentIndex:
        """Commit a run's changes, rolling back and re-raising on failure."""
        try:
            return self._indexes.save(index)
        except Exception:
            self._indexes.rollback()
            logger.error(
                "indexing_write_failed",
                index_id=str(index.id),
                document_id=str(index.document_id),
            )
            raise

    @staticmethod
    def _transition(index: DocumentIndex, target: IndexStatus) -> None:
        """Move a run to a new state, refusing an illegal move.

        Checked even though every caller here is already correct, because the
        transition table is the definition of the lifecycle and a future caller —
        a bulk re-indexing job after a model change, an admin tool — must not be
        able to write a status directly. A run that reached ``indexed`` without
        passing through ``indexing`` would make every duration and every start
        time a lie.

        Raises:
            InvalidIndexTransitionError: the move is not in the transition table.
        """
        if not can_transition(index.status, target):
            raise InvalidIndexTransitionError(index.status.value, target.value)
        index.status = target

    @staticmethod
    def _elapsed_ms(monotonic_start: float) -> int:
        """Milliseconds since the run started.

        From :func:`time.monotonic` rather than from the two stored timestamps: a
        wall clock can be adjusted mid-run, and a negative duration in a
        monitoring average is worse than a slightly imprecise one.
        """
        return max(0, int((time.monotonic() - monotonic_start) * 1000))

    @staticmethod
    def _check_deadline(monotonic_start: float) -> None:
        """Stop a run that has outlived ``INDEXING_TIMEOUT_SECONDS``.

        Checked **between stages** rather than enforced inside them, deliberately.
        Neither the splitter nor the embedder accepts a deadline, and interrupting
        a model mid-forward-pass is not something a thread can do safely — so the
        honest guarantee is "no new stage begins after the deadline", which
        bounds the run at one stage's overrun instead of pretending to a
        precision the libraries do not offer. The alternative, no deadline at
        all, is a worker thread that never returns.

        Raises:
            _IndexRunFailed: the deadline has passed.
        """
        if time.monotonic() - monotonic_start > settings.INDEXING_TIMEOUT_SECONDS:
            raise _IndexRunFailed(IndexFailureCode.TIMEOUT)

    @staticmethod
    def _has_text(result: OcrResult) -> bool:
        """Whether an extraction produced anything worth indexing."""
        return any(page.text.strip() for page in result.pages)

    def _require_document(self, document_id: uuid.UUID) -> Document:
        """Load a live document, or fail with a 404.

        Deliberately excludes soft-deleted documents, exactly as every other read
        path does: a withdrawn document's index is withdrawn with it, because the
        index inherits the document's permissions and the document is no longer
        readable.
        """
        document = self._documents.get_by_id(document_id)
        if document is None:
            logger.info("indexing_document_lookup_failed", document_id=str(document_id))
            raise DocumentNotFoundError
        return document

    def _require_index(self, document: Document, version: int | None) -> DocumentIndex:
        """The index for a document version, or a 404 that explains itself."""
        entry = self._resolve_version(document, version)
        index = self._indexes.get_for_version(document.id, entry.version)

        if index is None:
            ocr_result = self._ocr.get_for_version(document.id, entry.version)
            logger.info(
                "indexing_lookup_failed",
                document_id=str(document.id),
                document_version=entry.version,
                # Why there is no index is almost always "its text has not been
                # extracted yet", and an operator reading this line should not
                # have to cross-reference a second table to find that out.
                ocr_status=ocr_result.status.value if ocr_result is not None else None,
            )
            raise DocumentIndexNotFoundError
        return index

    @staticmethod
    def _resolve_version(document: Document, version: int | None) -> DocumentVersion:
        """Return the requested version, or the current one when unspecified.

        Raises:
            DocumentVersionNotFoundError: the document has no such version.
        """
        wanted = document.version if version is None else version
        entry = next((item for item in document.versions if item.version == wanted), None)

        if entry is None:
            logger.info(
                "indexing_version_lookup_failed",
                document_id=str(document.id),
                requested_version=wanted,
            )
            raise DocumentVersionNotFoundError
        return entry

    def _publish_retry(
        self,
        document: Document,
        index: DocumentIndex,
        *,
        actor: User,
        entry: DocumentVersion,
    ) -> None:
        """Announce that a user asked for the index to be rebuilt."""
        self._publish(
            document,
            index,
            TimelineEventType.INDEXING_RETRIED,
            actor=actor,
            description=(
                f"{actor.full_name} requested search indexing for "
                f'"{entry.original_filename}" (version {entry.version}).'
            ),
            extra={"attempt": index.attempt_count + 1},
        )

    def _publish(
        self,
        document: Document,
        index: DocumentIndex,
        event_type: TimelineEventType,
        *,
        actor: User | None,
        description: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Announce an indexing event on the document's case timeline.

        Every indexing event carries the same identifying metadata, assembled
        here so four call sites cannot each remember a different subset of it;
        ``extra`` adds whatever is specific to one of them.

        **The filename appears here and nowhere in the application log**, and no
        indexed passage appears in either. That is the separation OCR
        established and this feature keeps: the timeline is served only to users
        already entitled to the document's case, while a log line goes to an
        operator who has no such entitlement — and a chunk's text *is* the
        document's contents, which nothing outside the authorized read path may
        reproduce.
        """
        self._timeline.record(
            case_id=document.case_id,
            event_type=event_type,
            actor=actor,
            description=description,
            metadata={
                "document_id": document.id,
                "filename": document.original_filename,
                "document_version": index.document_version,
                "index_id": index.id,
                "index_status": index.status.value,
                **(extra or {}),
            },
        )

    @staticmethod
    def _event(index: DocumentIndex, *, actor: User | None) -> dict[str, Any]:
        """The fields every indexing log entry carries.

        Identifiers, the status, and the shape of the work — **never a filename,
        never a description, and never a character of indexed text**. The first
        two can name a client or quote a matter; the third *is* the document's
        contents, and both the spec and `code-standards.md` forbid logging it.
        """
        return {
            "index_id": str(index.id),
            "document_id": str(index.document_id),
            "document_version": index.document_version,
            "case_id": str(index.case_id),
            "index_status": index.status.value,
            "actor_id": str(actor.id) if actor is not None else None,
        }


class _IndexRunFailed(Exception):
    """Internal signal that a run failed for a reason outside the three seams.

    Private to this module: missing OCR text, a deadline, or a stage that
    produced nothing is a failure of the *run*, not of the chunker, the embedder,
    or the vector store, and it needs to travel to the same recording path
    without pretending to be one of theirs.
    """

    def __init__(self, code: IndexFailureCode) -> None:
        self.code = code
        super().__init__(code.value)
