"""The background OCR worker.

The one place that knows both halves of the picture: it builds an
:class:`~services.ocr.OcrService` for a job and hands it to a queue. Keeping it
separate from both is what lets :mod:`services.ocr_queue` stay ignorant of the
service (it is injected a callable) and :mod:`services.ocr` stay ignorant of the
queue's implementation (it is injected a protocol) — neither imports the other,
so there is no cycle to work around with a deferred import.

**Each job gets its own database session.** A worker runs on a background thread
long after the request that queued it has returned, and a SQLAlchemy ``Session``
is not thread-safe — reusing the request's session would be a use-after-free with
extra steps. The session is opened here, per job, and closed in a ``finally``, so
a connection cannot leak however the run ends.

The service the worker builds is deliberately given a
:class:`~services.ocr_queue.NullOcrJobQueue`: a job must not be able to enqueue
more work, which is what would turn a bug into an unbounded loop.
"""

from __future__ import annotations

import structlog

from db.session import SessionLocal
from repositories.case import CaseRepository
from repositories.document import DocumentRepository
from repositories.indexing import IndexingRepository
from repositories.ocr import OcrRepository
from repositories.timeline import TimelineRepository
from services.document_storage import DocumentStorageService
from services.indexing import IndexingService
from services.indexing_worker import index_queue
from services.ocr import OcrService
from services.ocr_engine import get_ocr_engine
from services.ocr_queue import (
    NullOcrJobQueue,
    OcrJob,
    ThreadPoolOcrJobQueue,
)
from services.timeline import TimelineService

logger = structlog.get_logger(__name__)


def run_ocr_job(job: OcrJob) -> None:
    """Process one queued extraction on its own session.

    Never raises: :meth:`~services.ocr.OcrService.process` records every failure
    as a ``failed`` run, and the queue catches anything that escapes even that.
    """
    session = SessionLocal()
    try:
        results = OcrRepository(session)
        documents = DocumentRepository(session)
        timeline = TimelineService(TimelineRepository(session), CaseRepository(session))

        # The indexing scheduler this run hands a completed extraction to. It is
        # given the *indexing* queue and nothing else it could use to do work
        # itself: scheduling from an OCR worker enqueues an indexing job, it does
        # not embed anything on this thread. Building it here rather than
        # importing a singleton keeps it on the same session as the extraction,
        # so the index row it creates is written by the transaction that has just
        # committed the text.
        indexing = IndexingService(
            IndexingRepository(session),
            documents,
            results,
            queue=index_queue,
            timeline=timeline,
        )

        service = OcrService(
            results,
            documents,
            DocumentStorageService(),
            get_ocr_engine(),
            # A worker may not queue more OCR work — see the module docstring.
            NullOcrJobQueue(),
            timeline=timeline,
            indexing=indexing,
        )
        service.process(job)
    finally:
        session.close()


#: The application's OCR queue.
#:
#: A module-level singleton because the pool it owns is a process-wide resource:
#: one pool of ``OCR_WORKER_CONCURRENCY`` threads, shared by every request that
#: schedules work, is the whole point of bounding it. Started and stopped by the
#: application lifespan (see :mod:`core.lifespan`); a queue that was never
#: started still works, because :meth:`ThreadPoolOcrJobQueue.enqueue` starts it
#: on first use.
ocr_queue = ThreadPoolOcrJobQueue(run_ocr_job)


def start_ocr_workers() -> None:
    """Start the worker pool and recover work stranded by a previous process.

    The requeue is the half that matters: a job's schedule lives in memory but
    its *record* lives in the database, so a restart leaves ``pending`` rows that
    nothing would ever pick up. Re-queueing them is safe to repeat — the claim is
    atomic, so a job queued twice still processes once.

    Neither half is allowed to abort startup. An API that refuses to come up
    because its OCR pool could not be created would take down authentication,
    cases, and documents over a background feature.
    """
    from core.config import settings

    if not settings.OCR_ENABLED:
        logger.info("ocr_workers_disabled")
        return

    try:
        ocr_queue.start()
    except Exception:
        logger.exception("ocr_workers_start_failed")
        return

    session = SessionLocal()
    try:
        service = OcrService(
            OcrRepository(session),
            DocumentRepository(session),
            queue=ocr_queue,
        )
        service.requeue_pending()
    except Exception:
        logger.exception("ocr_requeue_failed")
    finally:
        session.close()


def stop_ocr_workers() -> None:
    """Drain the worker pool on shutdown.

    Draining rather than cancelling: a job stopped mid-extraction leaves its row
    at ``processing``, and that is the one state no other worker will claim.
    """
    ocr_queue.shutdown(wait=True)
