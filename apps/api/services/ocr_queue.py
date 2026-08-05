"""The background-job boundary for OCR.

``09-ocr-processing.md`` requires that *"the upload request must never wait for
OCR to complete"*, and `architecture.md` invariant 7 says long-running operations
execute asynchronously. This module is where that happens, and — like
:mod:`services.ocr_engine` — it is deliberately a **seam** rather than a
commitment.

The shape:

* :class:`OcrJobQueue` is the protocol :mod:`services.ocr` depends on. It has one
  method. A publisher hands over a job and is done; nothing above this line knows
  whether the work runs in a thread, in a Celery worker, or on a Trigger.dev
  task.
* :class:`ThreadPoolOcrJobQueue` is the implementation this feature ships: a
  small, bounded pool inside the API process.
* :class:`InlineOcrJobQueue` runs the job synchronously, which is what an
  integration test wants — the assertion is about the *outcome*, and a test that
  has to wait for a thread is a test that will eventually be flaky.
* :class:`NullOcrJobQueue` discards, and is the default for a service built
  without a queue.

**Why a thread pool rather than Celery.** `architecture.md` names "Trigger.dev
(or Celery + Redis)" for background jobs, and neither exists yet — introducing
one would mean a new deployable, a new dependency, and new infrastructure in a
feature whose spec is about extracting text. What the spec actually requires is
that the upload returns immediately, that a job is created, and that only one
runs per document; all three hold here, and the *durable* half of them is in the
database rather than in the queue: a job's existence is a ``pending`` row, and
its claim is a conditional ``UPDATE``. That means switching to Celery later
replaces this file and nothing else — the rows, the states, and the concurrency
guarantee are all already outside it.

The honest limits of the in-process pool are recorded on
:class:`ThreadPoolOcrJobQueue`.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import structlog

from core.config import settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OcrJob:
    """One unit of background OCR work.

    Carries **identifiers only**, never an ORM instance. A job crosses a thread
    boundary and outlives the request that created it, and a detached SQLAlchemy
    object on the far side of either is a source of stale reads and
    cross-session errors. The worker re-reads the row it is about to change,
    which is also what makes the claim honest.
    """

    ocr_result_id: uuid.UUID
    document_id: uuid.UUID
    document_version: int


#: What a queue calls to actually do the work. Injected rather than imported, so
#: this module never depends on :mod:`services.ocr` — which depends on it.
OcrJobRunner = Callable[[OcrJob], None]


class OcrJobQueue(Protocol):
    """What the OCR service requires of a background queue.

    One method, taking a job and returning nothing. The narrowness is the point:
    a publisher cannot accidentally depend on a queue's internals, and a
    replacement has one thing to implement.
    """

    def enqueue(self, job: OcrJob) -> None:
        """Schedule ``job`` to run outside the caller's request.

        **Never raises.** The OCR row is already committed by the time this is
        called, so failing here would answer a successful upload with a 500 and
        invite a retry that would duplicate the upload. A queue that cannot
        accept the job logs it and leaves the row ``pending`` — which is exactly
        the state the startup requeue looks for.
        """
        ...


class NullOcrJobQueue:
    """A queue that schedules nothing.

    The default for a service constructed without a queue — a script, a
    migration helper, or a unit test that is not about background execution. It
    exists so publishing code can be written as a plain call with no
    ``if self._queue`` guard at every site, exactly as
    :class:`~services.timeline.NullTimelineRecorder` does for the timeline.

    The application wires a real queue in :mod:`api.deps`; a test asserts that it
    does, so "the default is a no-op" cannot quietly become the production
    behaviour.
    """

    def enqueue(self, job: OcrJob) -> None:
        """Discard the job, leaving its row ``pending``."""
        logger.debug(
            "ocr_job_discarded",
            ocr_result_id=str(job.ocr_result_id),
            document_id=str(job.document_id),
        )


class InlineOcrJobQueue:
    """A queue that runs the job immediately, on the calling thread.

    For tests, and for any deployment that genuinely wants synchronous
    extraction. It keeps the *contract* — a failing job must not fail the caller
    — which is what makes it a faithful double rather than a shortcut: a test
    that passes against this one is testing the same error handling production
    uses.
    """

    def __init__(self, runner: OcrJobRunner) -> None:
        self._runner = runner

    def enqueue(self, job: OcrJob) -> None:
        """Run the job now, swallowing (and logging) any failure."""
        try:
            self._runner(job)
        except Exception:
            logger.exception(
                "ocr_job_failed",
                ocr_result_id=str(job.ocr_result_id),
                document_id=str(job.document_id),
            )


class ThreadPoolOcrJobQueue:
    """A bounded pool of worker threads inside the API process.

    Threads rather than a process pool because the work is almost entirely spent
    *outside* Python: ``pdftoppm`` and ``tesseract`` are separate binaries, and
    the interpreter releases the GIL while waiting on them. A process pool would
    add serialisation of the job and a fork per document for no gain.

    ``OCR_WORKER_CONCURRENCY`` is small by default (2) so that OCR — which
    saturates a CPU core per job — cannot starve the request handlers sharing the
    process.

    **What this does not do**, stated plainly because it decides when the queue
    should be replaced:

    * it does not survive a restart on its own — an in-flight job's row is left
      at ``processing`` and a queued one at ``pending``. :func:`requeue_pending`
      picks the second group up at startup; the first are recovered by a retry,
      which is a capability the spec requires anyway;
    * it does not spread work across API instances — each processes what it
      accepted;
    * it has no backoff and no dead-letter handling of its own. A failed run is
      a ``failed`` row that an authorized user can retry, which is the recovery
      model the spec describes.

    Every one of those is a property of *this class*, not of the OCR feature: the
    job's identity, its state, and its concurrency control all live in the
    database.
    """

    def __init__(self, runner: OcrJobRunner, *, max_workers: int | None = None) -> None:
        self._runner = runner
        self._max_workers = max(1, max_workers or settings.OCR_WORKER_CONCURRENCY)
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        #: Jobs accepted and not yet finished. Bounded only by what has been
        #: enqueued; used to drain cleanly on shutdown.
        self._in_flight: set[Future[None]] = set()

    # ------------------------------------------------------------ lifecycle #

    def start(self) -> None:
        """Create the worker pool. Idempotent."""
        with self._lock:
            if self._executor is not None:
                return
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix="ocr-worker"
            )
        logger.info("ocr_workers_started", workers=self._max_workers)

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting work and let in-flight jobs finish. Idempotent.

        ``wait=True`` on purpose: a job that is stopped mid-extraction leaves its
        row at ``processing``, which no other worker will claim. Draining is a
        few seconds at worst and leaves the table consistent.
        """
        with self._lock:
            executor, self._executor = self._executor, None

        if executor is None:
            return

        executor.shutdown(wait=wait, cancel_futures=not wait)
        logger.info("ocr_workers_stopped", drained=wait)

    @property
    def is_running(self) -> bool:
        """Whether the pool is accepting work."""
        return self._executor is not None

    # ------------------------------------------------------------ scheduling #

    def enqueue(self, job: OcrJob) -> None:
        """Hand the job to the pool.

        Starts the pool on first use, so a queue that was never explicitly
        started (a script, a test) still works rather than silently dropping
        every job.
        """
        if self._executor is None:
            self.start()

        executor = self._executor
        if executor is None:  # pragma: no cover - only reachable mid-shutdown
            logger.warning(
                "ocr_job_rejected",
                reason="queue_stopped",
                ocr_result_id=str(job.ocr_result_id),
            )
            return

        try:
            future = executor.submit(self._run, job)
        except RuntimeError:
            # The pool was shut down between the check above and the submit.
            # The row stays `pending`, which the startup requeue will find.
            logger.warning(
                "ocr_job_rejected",
                reason="queue_stopped",
                ocr_result_id=str(job.ocr_result_id),
            )
            return

        self._in_flight.add(future)
        future.add_done_callback(self._in_flight.discard)

        logger.info(
            "ocr_job_enqueued",
            ocr_result_id=str(job.ocr_result_id),
            document_id=str(job.document_id),
            document_version=job.document_version,
        )

    def _run(self, job: OcrJob) -> None:
        """Execute one job, absorbing any failure.

        A worker thread that raises loses the exception into the ``Future``
        nobody reads, so it is caught and logged here. The *run itself* records
        its own failure on the row; this is the backstop for a fault that escapes
        even that — and it must not take the pool's thread down with it.
        """
        try:
            self._runner(job)
        except Exception:
            logger.exception(
                "ocr_job_failed",
                ocr_result_id=str(job.ocr_result_id),
                document_id=str(job.document_id),
            )
