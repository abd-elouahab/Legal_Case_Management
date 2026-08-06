"""A reusable background-job boundary.

`architecture.md` invariant 7 says long-running operations execute
asynchronously, and ``10-document-indexing.md`` says the indexing pipeline *"must
execute asynchronously"*. This module is where that happens for indexing, and —
like :mod:`services.chunking`, :mod:`services.embedding`, and
:mod:`services.vector_store` — it is deliberately a **seam** rather than a
commitment.

It is the generic form of the pattern :mod:`services.ocr_queue` established:

* :class:`JobQueue` is the protocol a publisher depends on. It has one method. A
  publisher hands over a job and is done; nothing above this line knows whether
  the work runs in a thread, in a Celery worker, or on a Trigger.dev task.
* :class:`ThreadPoolJobQueue` is the implementation that ships: a small, bounded
  pool inside the API process.
* :class:`InlineJobQueue` runs the job synchronously, which is what an
  integration test wants — the assertion is about the *outcome*, and a test that
  has to wait for a thread is a test that will eventually be flaky.
* :class:`NullJobQueue` discards, and is the default for a service built without
  a queue.

**Generic rather than a second copy.** OCR's queue is the same machinery with
``OcrJob`` written into it in a dozen places; `code-standards.md` asks for
reusable services rather than duplication, and a thread pool has nothing
OCR-specific or indexing-specific about it. The job type is a type parameter, so
each feature keeps its own strongly-typed job dataclass and shares one runner.
:mod:`services.ocr_queue` is deliberately **left untouched** — OCR is a shipped
feature and this spec is not the place to refactor it — but it is now the
candidate to fold into this module the next time it is opened.

**Why a thread pool rather than Celery.** `architecture.md` names "Trigger.dev
(or Celery + Redis)" for background jobs, and neither exists yet — introducing
one would mean a new deployable, a new dependency, and new infrastructure inside
a feature whose spec is about building an index. What the specs actually require
is that the caller returns immediately, that a job is created, and that only one
runs per document; all three hold here, and the *durable* half of them is in
PostgreSQL rather than in the queue: a job's existence is a ``pending`` row, and
its claim is a conditional ``UPDATE``. Switching to Celery later replaces this
file and nothing else.

The honest limits of the in-process pool are recorded on
:class:`ThreadPoolJobQueue`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)

# The unit of work a queue carries is a type parameter. Each feature defines its
# own job — a frozen dataclass of **identifiers only**, never an ORM instance: a
# job crosses a thread boundary and outlives the request that created it, and a
# detached SQLAlchemy object on the far side of either is a source of stale reads
# and cross-session errors.
#
# PEP 695 syntax (`class Queue[JobT]`) rather than `Generic[JobT]`: the project
# targets Python 3.12, and it lets the variance be *inferred* — a queue is
# contravariant in its job type, and stating that by hand is a detail nothing
# here needs to get right twice.


class JobQueue[JobT](Protocol):
    """What a service requires of a background queue.

    One method, taking a job and returning nothing. The narrowness is the point:
    a publisher cannot accidentally depend on a queue's internals, and a
    replacement has one thing to implement.
    """

    def enqueue(self, job: JobT) -> None:
        """Schedule ``job`` to run outside the caller's request.

        **Never raises.** The row describing the work is already committed by the
        time this is called, so failing here would answer a successful request
        with a 500 and invite a retry that would duplicate it. A queue that
        cannot accept the job logs it and leaves the row ``pending`` — which is
        exactly the state a startup requeue looks for.
        """
        ...


class NullJobQueue[JobT]:
    """A queue that schedules nothing.

    The default for a service constructed without a queue — a script, a migration
    helper, or a unit test that is not about background execution. It exists so
    publishing code can be written as a plain call with no ``if self._queue``
    guard at every site, exactly as
    :class:`~services.timeline.NullTimelineRecorder` does for the timeline.

    The application wires a real queue in :mod:`api.deps`; a test asserts that it
    does, so "the default is a no-op" cannot quietly become the production
    behaviour.
    """

    def __init__(self, *, name: str = "job") -> None:
        self._name = name

    def enqueue(self, job: JobT) -> None:
        """Discard the job, leaving its row ``pending``."""
        logger.debug("job_discarded", queue=self._name, job=repr(job))


class InlineJobQueue[JobT]:
    """A queue that runs the job immediately, on the calling thread.

    For tests, and for any deployment that genuinely wants synchronous
    processing. It keeps the *contract* — a failing job must not fail the caller —
    which is what makes it a faithful double rather than a shortcut: a test that
    passes against this one is exercising the same error handling production uses.
    """

    def __init__(self, runner: Callable[[JobT], None], *, name: str = "job") -> None:
        self._runner = runner
        self._name = name

    def enqueue(self, job: JobT) -> None:
        """Run the job now, swallowing (and logging) any failure."""
        try:
            self._runner(job)
        except Exception:
            logger.exception("job_failed", queue=self._name, job=repr(job))


class ThreadPoolJobQueue[JobT]:
    """A bounded pool of worker threads inside the API process.

    Threads rather than a process pool because the work is dominated by waiting
    on things outside the interpreter — a model's native tensor code, an HTTP
    round trip to Qdrant, a database write — and the GIL is released for all
    three. A process pool would add serialisation of the job and a fork per
    document for no gain, and for indexing it would mean **a copy of a
    two-gigabyte embedding model per process**, which is the decisive argument.

    ``max_workers`` is small by default so that background work — which
    saturates a CPU core per job — cannot starve the request handlers sharing the
    process.

    **What this does not do**, stated plainly because it decides when the queue
    should be replaced:

    * it does not survive a restart on its own — an in-flight job's row is left
      at its "running" state and a queued one at ``pending``. A startup requeue
      picks the second group up; the first are recovered by a retry, which is a
      capability both specs require anyway;
    * it does not spread work across API instances — each processes what it
      accepted;
    * it has no backoff and no dead-letter handling of its own. A failed run is a
      ``failed`` row that an authorized user can retry, which is the recovery
      model the specs describe.

    Every one of those is a property of *this class*, not of the features using
    it: the job's identity, its state, and its concurrency control all live in
    the database.
    """

    def __init__(
        self,
        runner: Callable[[JobT], None],
        *,
        max_workers: int = 2,
        name: str = "job",
    ) -> None:
        self._runner = runner
        self._max_workers = max(1, max_workers)
        self._name = name
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        #: Jobs accepted and not yet finished. Used to drain cleanly on shutdown.
        self._in_flight: set[Future[None]] = set()

    # ------------------------------------------------------------ lifecycle #

    def start(self) -> None:
        """Create the worker pool. Idempotent."""
        with self._lock:
            if self._executor is not None:
                return
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix=f"{self._name}-worker"
            )
        logger.info("job_workers_started", queue=self._name, workers=self._max_workers)

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting work and let in-flight jobs finish. Idempotent.

        ``wait=True`` on purpose: a job stopped mid-run leaves its row in a
        "running" state, which no other worker will claim. Draining is a few
        seconds at worst and leaves the table consistent.
        """
        with self._lock:
            executor, self._executor = self._executor, None

        if executor is None:
            return

        executor.shutdown(wait=wait, cancel_futures=not wait)
        logger.info("job_workers_stopped", queue=self._name, drained=wait)

    @property
    def is_running(self) -> bool:
        """Whether the pool is accepting work."""
        return self._executor is not None

    # ----------------------------------------------------------- scheduling #

    def enqueue(self, job: JobT) -> None:
        """Hand the job to the pool.

        Starts the pool on first use, so a queue that was never explicitly
        started (a script, a test) still works rather than silently dropping
        every job.
        """
        if self._executor is None:
            self.start()

        executor = self._executor
        if executor is None:  # pragma: no cover - only reachable mid-shutdown
            logger.warning("job_rejected", queue=self._name, reason="queue_stopped")
            return

        try:
            future = executor.submit(self._run, job)
        except RuntimeError:
            # The pool was shut down between the check above and the submit. The
            # row stays `pending`, which the startup requeue will find.
            logger.warning("job_rejected", queue=self._name, reason="queue_stopped")
            return

        self._in_flight.add(future)
        future.add_done_callback(self._in_flight.discard)

    def _run(self, job: JobT) -> None:
        """Execute one job, absorbing any failure.

        A worker thread that raises loses the exception into the ``Future``
        nobody reads, so it is caught and logged here. The *run itself* records
        its own failure on its row; this is the backstop for a fault that escapes
        even that — and it must not take the pool's thread down with it.
        """
        try:
            self._runner(job)
        except Exception:
            logger.exception("job_failed", queue=self._name)


__all__ = [
    "InlineJobQueue",
    "JobQueue",
    "NullJobQueue",
    "ThreadPoolJobQueue",
]
