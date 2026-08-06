"""OCR processing business logic.

Owns the workflow ``09-ocr-processing.md`` defines: scheduling extraction for an
uploaded document, running it in the background, persisting the text it produces,
tracking the run's lifecycle, and letting an authorized user retry it.

Scope boundaries, kept deliberately sharp:

* **Recognition is not done here.** :mod:`services.ocr_engine` owns it, behind a
  protocol, so the engine can be replaced without touching this module.
* **Scheduling is not done here.** :mod:`services.ocr_queue` owns it, behind a
  protocol, so Celery or Trigger.dev can replace the in-process pool without
  touching this module.
* **Capability authorization is not decided here.** Whether the caller may use
  the OCR endpoints is settled by the dependencies in :mod:`api.authorization`
  before a request reaches this service.
* **Per-resource authorization is applied here**, through
  :class:`~services.ocr_access.OcrAccessPolicy`, because it needs the document
  and its case — a dependency cannot know whether a lawyer is assigned to a case
  it has not loaded.
* **Nothing about embeddings, vectors, chunking, retrieval, or an LLM appears
  anywhere in this module.** The spec is explicit that this feature ends at
  persisted text, and the text it persists is what the indexing feature reads.
  ``10-document-indexing.md``'s flow begins at "OCR Completed", so this service
  *announces* that moment through the narrow
  :class:`~services.indexing.IndexScheduler` protocol — one call, after the
  commit, that cannot fail the extraction. It knows nothing about what indexing
  does with it, exactly as :class:`~services.document.DocumentService` knows
  nothing about extraction beyond :class:`OcrScheduler`.

What it does own are the rules nothing else can express: that a run belongs to a
*document version*, that only one worker may process it, that a status may only
move along the legal transitions, that a failure never touches the uploaded file
or its metadata, and that the extracted text inherits the document's permissions.

It also **publishes to the timeline** — started, completed, failed, and retried
are the OCR half of a document's history. As with the case and document services,
publication happens after the change is committed, describes rather than decides,
and can never fail the operation that caused it.
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
    DocumentNotFoundError,
    DocumentStorageError,
    DocumentVersionNotFoundError,
    InvalidOcrTransitionError,
    OcrAlreadyRunningError,
    OcrDisabledError,
    OcrResultNotFoundError,
    OcrUnsupportedFormatError,
)
from core.ocr import (
    OcrFailureCode,
    can_retry,
    can_transition,
    failure_message,
    is_supported,
    normalize_confidence,
    normalize_error_message,
    normalize_language,
    sorted_supported_extensions,
    success_rate,
)
from models.document import Document, DocumentVersion
from models.ocr import OcrPage, OcrResult, OcrStatus
from models.timeline import TimelineEventType
from models.user import User
from repositories.document import DocumentRepository
from repositories.ocr import OcrRepository, OcrStatusCounts
from schemas.ocr import OcrListQuery
from services.document_storage import DocumentStorageService
from services.indexing import IndexScheduler, NullIndexScheduler
from services.ocr_access import OcrAccessPolicy
from services.ocr_engine import Extraction, OcrEngine, OcrEngineError, get_ocr_engine
from services.ocr_queue import NullOcrJobQueue, OcrJob, OcrJobQueue
from services.timeline import NullTimelineRecorder, TimelineRecorder

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OcrPageResult:
    """One page of OCR runs together with the total matching the filters."""

    results: list[OcrResult]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class OcrMetrics:
    """Platform-wide OCR health, as the monitoring endpoint reports it."""

    counts: OcrStatusCounts
    failures_by_code: dict[str, int]
    window_days: int | None
    engine: str
    engine_available: bool
    enabled: bool

    @property
    def success_rate(self) -> float:
        """Share of finished runs that produced text."""
        return success_rate(self.counts.completed, self.counts.failed)

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


class OcrScheduler(Protocol):
    """The narrow half of OCR that Document Management depends on.

    The document service needs to say "a file was stored, extract its text". It
    has no business reading a run, authorizing one, or knowing what a queue is.
    Depending on this protocol rather than on :class:`OcrService` states that in
    the type system, and lets a unit test for document upload substitute a
    scheduler without an engine, a queue, or a database.
    """

    def schedule_for_document(
        self, document: Document, *, actor: User | None = None
    ) -> OcrResult | None:
        """Queue extraction for a document's current version, if applicable."""
        ...


class NullOcrScheduler:
    """A scheduler that queues nothing.

    The default for a service constructed without OCR — a script, a migration
    helper, or a unit test that is not about extraction. Same role, and same
    reasoning, as :class:`~services.timeline.NullTimelineRecorder`: publishing
    code stays a plain call with no ``if self._ocr`` guard at every site.
    """

    def schedule_for_document(
        self, document: Document, *, actor: User | None = None
    ) -> OcrResult | None:
        """Discard the request and return ``None``."""
        return None


class OcrService:
    """Coordinates the OCR lifecycle."""

    def __init__(
        self,
        results: OcrRepository,
        documents: DocumentRepository,
        storage: DocumentStorageService | None = None,
        engine: OcrEngine | None = None,
        queue: OcrJobQueue | None = None,
        access: OcrAccessPolicy | None = None,
        *,
        timeline: TimelineRecorder | None = None,
        indexing: IndexScheduler | None = None,
    ) -> None:
        self._results = results
        self._documents = documents
        self._storage = storage or DocumentStorageService()
        self._engine = engine or get_ocr_engine()
        # See `CaseService.__init__` for why the defaults record and queue
        # nothing; the application wires the real collaborators in
        # `api.deps.get_ocr_service`.
        self._queue: OcrJobQueue = queue or NullOcrJobQueue()
        self._access = access or OcrAccessPolicy()
        self._timeline: TimelineRecorder = timeline or NullTimelineRecorder()
        self._indexing: IndexScheduler = indexing or NullIndexScheduler()

    # ---------------------------------------------------------- scheduling #

    def schedule_for_document(
        self, document: Document, *, actor: User | None = None
    ) -> OcrResult | None:
        """Queue extraction for a document's current version.

        Called by :class:`~services.document.DocumentService` after an upload or
        a replacement is committed. **It never raises and never blocks**: the
        file is already stored and the response is already earned, so a queueing
        problem must not turn a successful upload into a 500 that invites a
        duplicating retry.

        Returns ``None`` — with a reason in the log — when there is nothing to
        schedule:

        * OCR is disabled for this deployment;
        * the document's type is not one OCR applies to. No row is created for a
          Word file or a plain-text note: a run that could never be anything but
          "not applicable" is not a record worth keeping, and the read endpoint
          says so explicitly instead.

        **Idempotent**, as the spec requires: a version that already has a run
        returns that run untouched rather than creating a second. The unique
        constraint on ``(document_id, document_version)`` is what makes that true
        under a race rather than merely likely.
        """
        try:
            return self._schedule(document, actor=actor, trigger="upload")
        except Exception:
            logger.exception(
                "ocr_scheduling_failed",
                document_id=str(document.id),
                document_version=document.version,
            )
            return None

    def _schedule(
        self, document: Document, *, actor: User | None, trigger: str
    ) -> OcrResult | None:
        """Create or reuse the run for a document's current version, then queue it."""
        if not settings.OCR_ENABLED:
            logger.info(
                "ocr_not_scheduled",
                reason="disabled",
                document_id=str(document.id),
                document_version=document.version,
            )
            return None

        if not is_supported(document.file_extension):
            logger.info(
                "ocr_not_scheduled",
                reason="unsupported_format",
                document_id=str(document.id),
                document_version=document.version,
                file_extension=document.file_extension,
            )
            return None

        existing = self._results.get_for_version(document.id, document.version)
        if existing is not None:
            logger.info(
                "ocr_not_scheduled",
                reason="already_recorded",
                document_id=str(document.id),
                document_version=document.version,
                ocr_result_id=str(existing.id),
                ocr_status=existing.status.value,
            )
            return existing

        result = self._results.create(
            OcrResult(
                id=uuid.uuid4(),
                document_id=document.id,
                document_version=document.version,
                status=OcrStatus.PENDING,
                requested_by=actor.id if actor is not None else None,
            )
        )

        logger.info(
            "ocr_requested",
            trigger=trigger,
            **self._event(result, actor=actor),
        )
        self._enqueue(result)
        return result

    def requeue_pending(self, limit: int = 100) -> int:
        """Re-queue runs left ``pending`` by a previous process.

        A job lives in an in-process queue (see :mod:`services.ocr_queue`), so a
        restart loses the schedule but not the *record* of the work — the row is
        still ``pending``, and nothing else would ever pick it up. Running this at
        startup is what closes that gap, and it is safe to run repeatedly: a run
        that has since been claimed is no longer pending, and the claim itself is
        atomic, so a double-queued job processes exactly once.

        Returns:
            How many jobs were re-queued.
        """
        if not settings.OCR_ENABLED:
            return 0

        pending = self._results.pending_results(limit=limit)
        for result in pending:
            self._enqueue(result)

        if pending:
            logger.info("ocr_pending_requeued", count=len(pending))
        return len(pending)

    def _enqueue(self, result: OcrResult) -> None:
        """Hand a committed run to the background queue."""
        self._queue.enqueue(
            OcrJob(
                ocr_result_id=result.id,
                document_id=result.document_id,
                document_version=result.document_version,
            )
        )

    # ------------------------------------------------------------- reading #

    def get_result(
        self, document_id: uuid.UUID, *, actor: User, version: int | None = None
    ) -> OcrResult:
        """Return the OCR run for a document, current version by default.

        Raises:
            DocumentNotFoundError: no live document has this identifier.
            OcrAccessDeniedError: the caller is not party to its case.
            OcrResultNotFoundError: the document has no run for that version.
        """
        document = self._require_document(document_id)
        self._access.require_document_access(actor, document)
        return self._require_result(document, version)

    def list_document_results(self, document_id: uuid.UUID, *, actor: User) -> list[OcrResult]:
        """Every run recorded for a document, oldest version first.

        A document accumulates one run per version, because a replacement is new
        bytes that need their own reading while the previous version's text stays
        valid.
        """
        document = self._require_document(document_id)
        self._access.require_document_access(actor, document)
        return self._results.list_for_document(document.id)

    def get_text(
        self, document_id: uuid.UUID, *, actor: User, version: int | None = None
    ) -> OcrResult:
        """Return the run whose pages hold the extracted text.

        The same authorization as :meth:`get_result`, because it is the same
        resource: *"extracted text inherits document permissions"*. The pages are
        eagerly loaded by the mapping, so this is one query rather than one per
        page.

        A run that has not completed is returned as it stands rather than
        refused — its ``pages`` are simply empty, and a client polling a document
        being processed needs the status more than it needs an error.
        """
        return self.get_result(document_id, actor=actor, version=version)

    def list_results(self, query: OcrListQuery, *, actor: User) -> OcrPageResult:
        """Return one page of OCR runs the caller is entitled to see.

        Filtering, sorting, pagination, **and the case scope** are all applied in
        the database, so the cost of a page does not grow with the number of runs
        and the totals describe only what the caller may access.
        """
        results, total = self._results.list_results(
            query, visible_to=self._access.visibility_scope(actor)
        )
        return OcrPageResult(
            results=results, total=total, page=query.page, page_size=query.page_size
        )

    def metrics(self, *, window_days: int | None = None) -> OcrMetrics:
        """Aggregate OCR health for the monitoring endpoint.

        No per-resource scope is applied, deliberately: this is an operational
        view of the *pipeline*, gated on the administrative ``ocr:monitor``
        permission, and it reports counts and timings only — never a document, a
        case, or a fragment of text. Scoping it per caller would make the
        platform's success rate mean something different for each of them, which
        is not a health metric.
        """
        since = (
            datetime.now(UTC) - timedelta(days=window_days) if window_days is not None else None
        )
        return OcrMetrics(
            counts=self._results.metrics(since=since),
            failures_by_code=self._results.failure_breakdown(since=since),
            window_days=window_days,
            engine=self._engine.name,
            engine_available=self._engine.is_available(),
            enabled=settings.OCR_ENABLED,
        )

    # ---------------------------------------------------------------- retry #

    def retry(
        self, document_id: uuid.UUID, *, actor: User, version: int | None = None
    ) -> OcrResult:
        """Queue a fresh extraction for a document already uploaded.

        *"Authorized users should be able to retry OCR without uploading the
        document again"* — so this takes a document, not a file, and touches
        neither the stored object nor the document's metadata.

        The existing run is **re-used**, not replaced: same row, status back to
        ``pending``, timing and failure fields cleared, ``attempt_count``
        preserved. That is the idempotency the spec requires — retrying the same
        document version can never produce a second, contradictory record of it.

        A version with no run at all is bootstrapped here rather than refused: a
        document uploaded while OCR was disabled, or before this feature existed,
        is exactly the case a retry should be able to fix.

        Raises:
            DocumentNotFoundError: no live document has this identifier.
            OcrAccessDeniedError: the caller is not party to its case.
            DocumentVersionNotFoundError: the document has no such version.
            OcrDisabledError: extraction is switched off for this deployment.
            OcrUnsupportedFormatError: OCR does not apply to this file type.
            OcrAlreadyRunningError: a run is already queued or extracting.
        """
        document = self._require_document(document_id)
        self._access.require_document_access(actor, document)

        entry = self._resolve_version(document, version)

        if not settings.OCR_ENABLED:
            logger.info(
                "ocr_retry_rejected",
                reason="disabled",
                document_id=str(document.id),
                document_version=entry.version,
                actor_id=str(actor.id),
            )
            raise OcrDisabledError

        if not is_supported(entry.file_extension):
            logger.info(
                "ocr_retry_rejected",
                reason="unsupported_format",
                document_id=str(document.id),
                document_version=entry.version,
                file_extension=entry.file_extension,
                actor_id=str(actor.id),
            )
            raise OcrUnsupportedFormatError(entry.file_extension, sorted_supported_extensions())

        existing = self._results.get_for_version(document.id, entry.version)

        if existing is None:
            # Nothing to retry — schedule the first run instead. Returned through
            # the same path, so a client sees one shape whether it asked for a
            # retry of a failure or of a document that was never processed.
            created = self._results.create(
                OcrResult(
                    id=uuid.uuid4(),
                    document_id=document.id,
                    document_version=entry.version,
                    status=OcrStatus.PENDING,
                    requested_by=actor.id,
                )
            )
            logger.info("ocr_requested", trigger="retry", **self._event(created, actor=actor))
            self._publish_retry(document, created, actor=actor, entry=entry)
            self._enqueue(created)
            return created

        if not can_retry(existing.status):
            # `_event` already carries `ocr_status`, which is the whole reason
            # this rejection happened.
            logger.info(
                "ocr_retry_rejected",
                reason="already_running",
                **self._event(existing, actor=actor),
            )
            raise OcrAlreadyRunningError(existing.status.value)

        previous_status = existing.status
        self._transition(existing, OcrStatus.PENDING)
        # Cleared rather than kept: they describe the *previous* attempt, and
        # leaving them would make a queued run read as though it had already
        # failed. `attempt_count` is deliberately not reset — how many times a
        # document has been attempted is exactly what a retry should preserve.
        existing.started_at = None
        existing.finished_at = None
        existing.duration_ms = None
        existing.error_code = None
        existing.error_message = None
        existing.requested_by = actor.id
        self._results.replace_pages(existing, [])
        saved = self._results.save(existing)

        logger.info(
            "ocr_retried",
            previous_status=previous_status.value,
            attempt=saved.attempt_count,
            **self._event(saved, actor=actor),
        )
        self._publish_retry(document, saved, actor=actor, entry=entry)
        self._enqueue(saved)
        return saved

    # ----------------------------------------------------------- processing #

    def process(self, job: OcrJob) -> OcrResult | None:
        """Run one queued extraction to completion.

        The worker's entry point, and the only method here that is not driven by
        an HTTP request. It runs on a background thread with its own database
        session, so it re-reads everything it needs rather than trusting anything
        the request that queued it had loaded.

        The sequence is deliberate:

        1. **Claim the run** — a conditional ``UPDATE`` from ``pending`` to
           ``processing``. A run someone else already claimed returns here, which
           is what makes "prevent multiple OCR jobs from processing the same
           document simultaneously" true rather than likely.
        2. Announce the start, on the timeline and in the log.
        3. Read the stored file, extract, persist the text, and record the
           metadata.

        **Never raises.** Every failure becomes a ``failed`` run carrying a
        machine-readable cause, because that is what the spec asks for: the
        original document stays available, its metadata stays intact, and the run
        stays retryable.
        """
        result = self._results.get_by_id(job.ocr_result_id)
        if result is None:
            logger.warning("ocr_job_orphaned", ocr_result_id=str(job.ocr_result_id))
            return None

        started_at = datetime.now(UTC)
        if not self._results.claim(result.id, started_at=started_at):
            # Someone else got there first, or the run has already finished.
            # Not an error: this is the guard doing its job.
            logger.info(
                "ocr_job_skipped",
                reason="not_claimable",
                ocr_result_id=str(result.id),
                ocr_status=result.status.value,
            )
            return None

        result = self._results.get_by_id(result.id)
        if result is None:  # pragma: no cover - the row was just updated
            return None

        document = result.document
        actor = result.requester

        logger.info("ocr_started", attempt=result.attempt_count, **self._event(result, actor=actor))
        self._publish(
            document,
            result,
            TimelineEventType.OCR_STARTED,
            actor=actor,
            description=(
                f'Text extraction started for "{document.original_filename}" '
                f"(version {result.document_version})."
            ),
        )

        monotonic_start = time.monotonic()

        try:
            entry = self._version_for(document, result.document_version)
            content = self._read_object(entry)
            extraction = self._engine.extract(content, extension=entry.file_extension)
        except _OcrRunFailed as failure:
            return self._fail(
                result,
                document,
                code=failure.code,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )
        except OcrEngineError as exc:
            return self._fail(
                result,
                document,
                code=exc.code,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )
        except Exception:
            # A fault nothing above anticipated. Logged with a traceback, then
            # recorded as an ordinary failed run: an unexpected bug must not
            # leave a document stuck at `processing` forever, which is the one
            # state nothing can recover from without operator intervention.
            logger.exception(
                "ocr_unexpected_failure",
                ocr_result_id=str(result.id),
                document_id=str(result.document_id),
            )
            return self._fail(
                result,
                document,
                code=OcrFailureCode.UNKNOWN,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )

        if not extraction.pages:
            # No page could be read at all — a zero-page render, or an image the
            # engine could not make anything of. Distinct from "pages that
            # contain no text", which is a legitimate result: a blank separator
            # sheet has nothing to say, and marking that failed would invite
            # retries that can never succeed.
            return self._fail(
                result,
                document,
                code=OcrFailureCode.UNREADABLE_DOCUMENT,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )

        return self._complete(
            result,
            document,
            extraction,
            actor=actor,
            elapsed_ms=self._elapsed_ms(monotonic_start),
        )

    # ------------------------------------------------------------- helpers #

    def _complete(
        self,
        result: OcrResult,
        document: Document,
        extraction: Extraction,
        *,
        actor: User | None,
        elapsed_ms: int,
    ) -> OcrResult:
        """Persist the extracted text and mark the run completed."""
        self._transition(result, OcrStatus.COMPLETED)
        self._results.replace_pages(
            result,
            [
                OcrPage(
                    id=uuid.uuid4(),
                    ocr_result_id=result.id,
                    page_number=page.page_number,
                    text=page.text,
                    confidence=normalize_confidence(page.confidence),
                )
                for page in extraction.pages
            ],
        )

        result.engine = extraction.engine or self._engine.name
        result.engine_version = extraction.engine_version
        result.detected_language = normalize_language(extraction.detected_language)
        result.page_count = extraction.page_count
        result.confidence = normalize_confidence(extraction.confidence)
        result.finished_at = datetime.now(UTC)
        result.duration_ms = elapsed_ms
        result.error_code = None
        result.error_message = None

        saved = self._save(result)

        logger.info(
            "ocr_completed",
            page_count=saved.page_count,
            # The *size* of the text, never the text: `code-standards.md` and the
            # spec both forbid logging document contents, and a character count is
            # what an operator actually needs to spot an empty extraction.
            character_count=saved.character_count,
            confidence=saved.confidence,
            duration_ms=saved.duration_ms,
            **self._event(saved, actor=actor),
        )
        self._publish(
            document,
            saved,
            TimelineEventType.OCR_COMPLETED,
            actor=actor,
            description=(
                f'Text extraction completed for "{document.original_filename}" — '
                f"{saved.page_count} page{'s' if saved.page_count != 1 else ''} read."
            ),
            extra={
                "page_count": saved.page_count,
                "character_count": saved.character_count,
                "confidence": saved.confidence,
                "duration_ms": saved.duration_ms,
                "engine": saved.engine,
            },
        )
        # The one line that hands the pipeline on. **After the commit and after
        # the timeline**, for the same reason publication is: the text is
        # persisted and the extraction has succeeded by this point, so nothing
        # indexing does may be able to undo either. `schedule_for_ocr_result`
        # swallows its own failures, so this cannot raise — and if it somehow
        # did, the run is already `completed` in the database.
        self._indexing.schedule_for_ocr_result(saved, actor=actor)
        return saved

    def _fail(
        self,
        result: OcrResult,
        document: Document,
        *,
        code: OcrFailureCode,
        actor: User | None,
        elapsed_ms: int,
    ) -> OcrResult:
        """Record a failed run, leaving the document and its file untouched.

        The three guarantees the spec names hold structurally rather than by
        care: this method writes to ``ocr_results`` and ``ocr_pages`` only, so it
        *cannot* delete an uploaded file or corrupt a document's metadata — it
        has no reference to either.
        """
        self._transition(result, OcrStatus.FAILED)
        result.finished_at = datetime.now(UTC)
        result.duration_ms = elapsed_ms
        result.error_code = code.value
        result.error_message = normalize_error_message(failure_message(code))
        result.engine = result.engine or self._engine.name

        saved = self._save(result)

        logger.warning(
            "ocr_failed",
            error_code=code.value,
            attempt=saved.attempt_count,
            duration_ms=saved.duration_ms,
            **self._event(saved, actor=actor),
        )
        self._publish(
            document,
            saved,
            TimelineEventType.OCR_FAILED,
            actor=actor,
            description=(
                f'Text extraction failed for "{document.original_filename}" — '
                f"{saved.error_message}"
            ),
            extra={"error_code": code.value, "attempt": saved.attempt_count},
        )
        return saved

    def _save(self, result: OcrResult) -> OcrResult:
        """Commit a run's changes, rolling back and re-raising on failure."""
        try:
            return self._results.save(result)
        except Exception:
            self._results.rollback()
            logger.error(
                "ocr_result_write_failed",
                ocr_result_id=str(result.id),
                document_id=str(result.document_id),
            )
            raise

    @staticmethod
    def _transition(result: OcrResult, target: OcrStatus) -> None:
        """Move a run to a new state, refusing an illegal move.

        Checked even though every caller here is already correct, because the
        transition table is the definition of the lifecycle and a future caller —
        a bulk re-processing job, an admin tool — must not be able to write a
        status directly. A run that reached ``completed`` without passing through
        ``processing`` would make every duration and every start time a lie.

        Raises:
            InvalidOcrTransitionError: the move is not in the transition table.
        """
        if not can_transition(result.status, target):
            raise InvalidOcrTransitionError(result.status.value, target.value)
        result.status = target

    @staticmethod
    def _elapsed_ms(monotonic_start: float) -> int:
        """Milliseconds since the run started.

        From :func:`time.monotonic` rather than from the two stored timestamps: a
        wall clock can be adjusted mid-run, and a negative duration in a
        monitoring average is worse than a slightly imprecise one.
        """
        return max(0, int((time.monotonic() - monotonic_start) * 1000))

    def _read_object(self, entry: DocumentVersion) -> bytes:
        """Read a stored version's bytes for the engine.

        Buffered rather than streamed, unavoidably: both page rendering and
        recognition need the whole file, and neither library accepts a stream.
        The ceiling is ``MAX_DOCUMENT_SIZE_MB``, enforced at upload, so this is
        bounded by the same limit that bounds the upload itself.

        Raises:
            _OcrRunFailed: object storage could not serve the file. The document
                row is untouched — this is an infrastructure fault, and the run
                stays retryable.
        """
        stream = None
        try:
            stream = self._storage.open_object(entry.storage_key)
            return b"".join(stream)
        except DocumentStorageError:
            logger.error(
                "ocr_storage_read_failed",
                document_id=str(entry.document_id),
                document_version=entry.version,
            )
            raise _OcrRunFailed(OcrFailureCode.STORAGE_FAILURE) from None
        finally:
            # The iterator closes the response when it is exhausted; this covers
            # the path where `open_object` succeeded and the join did not.
            if stream is not None:
                stream.close()

    @staticmethod
    def _version_for(document: Document, version: int) -> DocumentVersion:
        """The version row a run was recorded against.

        Raises:
            _OcrRunFailed: the version is gone, which can only be a data
                inconsistency — recorded as a storage failure rather than crashing
                the worker.
        """
        entry = next((item for item in document.versions if item.version == version), None)
        if entry is None:
            logger.error(
                "ocr_version_missing",
                document_id=str(document.id),
                document_version=version,
            )
            raise _OcrRunFailed(OcrFailureCode.STORAGE_FAILURE)
        return entry

    def _require_document(self, document_id: uuid.UUID) -> Document:
        """Load a live document, or fail with a 404.

        Deliberately excludes soft-deleted documents, exactly as every other
        read path does: a withdrawn document's extracted text is withdrawn with
        it, because the text inherits the document's permissions and the document
        is no longer readable.
        """
        document = self._documents.get_by_id(document_id)
        if document is None:
            logger.info("ocr_document_lookup_failed", document_id=str(document_id))
            raise DocumentNotFoundError
        return document

    def _require_result(self, document: Document, version: int | None) -> OcrResult:
        """The run for a document version, or a 404 that explains itself."""
        entry = self._resolve_version(document, version)
        result = self._results.get_for_version(document.id, entry.version)

        if result is None:
            logger.info(
                "ocr_result_lookup_failed",
                document_id=str(document.id),
                document_version=entry.version,
                file_extension=entry.file_extension,
                supported=is_supported(entry.file_extension),
            )
            raise OcrResultNotFoundError
        return result

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
                "ocr_version_lookup_failed",
                document_id=str(document.id),
                requested_version=wanted,
            )
            raise DocumentVersionNotFoundError
        return entry

    def _publish_retry(
        self,
        document: Document,
        result: OcrResult,
        *,
        actor: User,
        entry: DocumentVersion,
    ) -> None:
        """Announce that a user asked for extraction to be run again."""
        self._publish(
            document,
            result,
            TimelineEventType.OCR_RETRIED,
            actor=actor,
            description=(
                f'{actor.full_name} requested text extraction for '
                f'"{entry.original_filename}" (version {entry.version}).'
            ),
            extra={"attempt": result.attempt_count + 1},
        )

    def _publish(
        self,
        document: Document,
        result: OcrResult,
        event_type: TimelineEventType,
        *,
        actor: User | None,
        description: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Announce an OCR event on the document's case timeline.

        Every OCR event carries the same identifying metadata, assembled here so
        four call sites cannot each remember a different subset of it; ``extra``
        adds whatever is specific to one of them.

        **The filename appears here and nowhere in the application log**, and the
        extracted text appears in neither. That is the spec's separation: the
        timeline is served only to users already entitled to the document's case,
        while a log line goes to an operator who has no such entitlement — and
        the text itself is the document's contents, which nothing outside the
        authorized read path may reproduce.
        """
        self._timeline.record(
            case_id=document.case_id,
            event_type=event_type,
            actor=actor,
            description=description,
            metadata={
                "document_id": document.id,
                "filename": document.original_filename,
                "document_version": result.document_version,
                "ocr_result_id": result.id,
                "ocr_status": result.status.value,
                **(extra or {}),
            },
        )

    @staticmethod
    def _event(result: OcrResult, *, actor: User | None) -> dict[str, Any]:
        """The fields every OCR log entry carries.

        Identifiers, the status, and the shape of the work — **never a filename,
        never a description, and never a character of extracted text**. The first
        two can name a client or quote a matter; the third *is* the document's
        contents, and the spec forbids logging it in as many words.
        """
        return {
            "ocr_result_id": str(result.id),
            "document_id": str(result.document_id),
            "document_version": result.document_version,
            "ocr_status": result.status.value,
            "actor_id": str(actor.id) if actor is not None else None,
        }


class _OcrRunFailed(Exception):
    """Internal signal that a run failed for a reason outside the engine.

    Private to this module: a storage read or a missing version row is a failure
    of the *run*, not of the engine, and it needs to travel to the same recording
    path without pretending to be an :class:`~services.ocr_engine.OcrEngineError`.
    """

    def __init__(self, code: OcrFailureCode) -> None:
        self.code = code
        super().__init__(code.value)
