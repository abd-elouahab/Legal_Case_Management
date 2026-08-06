"""The background document-indexing worker.

The one place that knows both halves of the picture: it builds an
:class:`~services.indexing.IndexingService` for a job and hands it to a queue.
Keeping it separate from both is what lets :mod:`services.job_queue` stay
ignorant of the service (it is injected a callable) and :mod:`services.indexing`
stay ignorant of the queue's implementation (it is injected a protocol) — neither
imports the other, so there is no cycle to work around with a deferred import.
The same shape as :mod:`services.ocr_worker`.

**Each job gets its own database session.** A worker runs on a background thread
long after the request that queued it has returned, and a SQLAlchemy ``Session``
is not thread-safe — reusing the request's session would be a use-after-free with
extra steps. The session is opened here, per job, and closed in a ``finally``, so
a connection cannot leak however the run ends.

**The embedder is *not* per job.** :func:`~services.embedding.get_embedder`
returns a process-wide instance holding a model measured in gigabytes; building
one per document would make indexing unusable, and building one per thread would
multiply the platform's memory by the pool size. The model is stateless once
loaded, so sharing it is safe as well as necessary.

The service the worker builds is deliberately given a
:class:`~services.job_queue.NullJobQueue`: a job must not be able to enqueue more
work, which is what would turn a bug into an unbounded loop.
"""

from __future__ import annotations

import structlog

from core.config import settings
from db.session import SessionLocal
from repositories.case import CaseRepository
from repositories.document import DocumentRepository
from repositories.indexing import IndexingRepository
from repositories.ocr import OcrRepository
from repositories.timeline import TimelineRepository
from services.chunking import get_chunker
from services.embedding import get_embedder
from services.indexing import IndexingService, IndexJob
from services.job_queue import NullJobQueue, ThreadPoolJobQueue
from services.timeline import TimelineService
from services.vector_store import get_vector_store

logger = structlog.get_logger(__name__)


def run_index_job(job: IndexJob) -> None:
    """Process one queued index on its own session.

    Never raises: :meth:`~services.indexing.IndexingService.process` records
    every failure as a ``failed`` run, and the queue catches anything that
    escapes even that.
    """
    session = SessionLocal()
    try:
        timeline = TimelineService(TimelineRepository(session), CaseRepository(session))

        service = IndexingService(
            IndexingRepository(session),
            DocumentRepository(session),
            OcrRepository(session),
            get_chunker(),
            get_embedder(),
            get_vector_store(),
            # A worker may not queue more work — see the module docstring.
            NullJobQueue(name="indexing"),
            timeline=timeline,
        )
        service.process(job)
    finally:
        session.close()


#: The application's indexing queue.
#:
#: A module-level singleton because the pool it owns is a process-wide resource:
#: one pool of ``INDEXING_WORKER_CONCURRENCY`` threads, shared by every request
#: and every completed extraction that schedules work, is the whole point of
#: bounding it. Started and stopped by the application lifespan (see
#: :mod:`core.lifespan`); a queue that was never started still works, because
#: :meth:`ThreadPoolJobQueue.enqueue` starts it on first use.
#:
#: **Separate from the OCR pool, deliberately.** They compete for the same CPU
#: but fail differently and are sized differently — extraction is subprocess-
#: bound and parallelises cheaply, while embedding holds a large model and
#: benefits from a single worker on a CPU-only host. One shared pool would make
#: a backlog of scans delay every index, and a slow index stall every upload's
#: extraction.
index_queue: ThreadPoolJobQueue[IndexJob] = ThreadPoolJobQueue(
    run_index_job,
    max_workers=settings.INDEXING_WORKER_CONCURRENCY,
    name="indexing",
)


def start_index_workers() -> None:
    """Start the worker pool and recover work stranded by a previous process.

    The requeue is the half that matters: a job's schedule lives in memory but
    its *record* lives in the database, so a restart leaves ``pending`` rows that
    nothing would ever pick up. Re-queueing them is safe to repeat — the claim is
    atomic, so a job queued twice still processes once.

    Neither half is allowed to abort startup. An API that refuses to come up
    because its indexing pool could not be created would take down
    authentication, cases, and documents over a background feature. **The
    embedding model is deliberately not loaded here either**: it is fetched on
    first use, so a deployment with no model cached still starts, serves every
    other feature, and reports ``embedding_available: false`` on the monitoring
    endpoint.
    """
    if not settings.INDEXING_ENABLED:
        logger.info("index_workers_disabled")
        return

    try:
        index_queue.start()
    except Exception:
        logger.exception("index_workers_start_failed")
        return

    session = SessionLocal()
    try:
        service = IndexingService(
            IndexingRepository(session),
            DocumentRepository(session),
            OcrRepository(session),
            queue=index_queue,
        )
        service.requeue_pending()
    except Exception:
        logger.exception("index_requeue_failed")
    finally:
        session.close()


def stop_index_workers() -> None:
    """Drain the worker pool on shutdown.

    Draining rather than cancelling: a job stopped mid-run leaves its row at
    ``indexing``, and that is the one state no other worker will claim.
    """
    index_queue.shutdown(wait=True)
