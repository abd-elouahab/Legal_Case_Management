"""The background email worker, and the retry sweeper beside it.

The one place that knows both halves of the picture: it builds an
:class:`~services.email_delivery.EmailDeliveryService` for a job and hands it to
a queue. Keeping it separate from both is what lets :mod:`services.job_queue` stay
ignorant of the service (it is injected a callable) and
:mod:`services.email_delivery` stay ignorant of the queue's implementation (it is
injected a protocol) — neither imports the other, so there is no cycle to work
around with a deferred import. The same shape as :mod:`services.ocr_worker`,
:mod:`services.indexing_worker`, and :mod:`services.report_worker`.

**Each job gets its own database session.** A worker runs on a background thread
long after the notification that queued it was created, and a SQLAlchemy
``Session`` is not thread-safe — reusing another thread's would be a
use-after-free with extra steps. The session is opened here, per job, and closed
in a ``finally``, so a connection cannot leak however the send ends.

**Its own pool, and a fourth one is not extravagance.** OCR is subprocess-heavy,
indexing holds a two-gigabyte model, a report is a burst of calls to a metered
API, and an email is a short network round trip to a relay that may be slow, may
be greylisting, and may be down. They fail differently and are sized differently:
sharing a pool with reports would make one slow relay delay every report, and
sharing with indexing would make a re-index of a 900-page bundle delay a password
reset. ``EMAIL_WORKER_CONCURRENCY`` defaults to **2** — enough that one hung
connection does not stop the mail, small enough that the platform never looks
like a sender worth rate-limiting.

**The sweeper is this feature's one genuinely new piece of machinery**, and it is
what makes the retry policy honest. A transient failure writes a
``next_attempt_at`` onto the row and returns the worker thread immediately; the
sweeper wakes on an interval, re-queues everything that has come due, and rescues
anything a dead process left stranded in ``sending``. The alternative — a worker
sleeping out its own backoff — would hold one of two threads for up to an hour
and lose the schedule entirely on restart, which is precisely the failure a
persisted delivery row exists to prevent.

The service the worker builds is deliberately given the **real** queue rather
than a null one, unlike the report worker's: an email job does not enqueue more
work, and the sweeper *is* the enqueueing path, so the same service object serves
both without a bug being able to turn into a loop — :meth:`process` never calls
``enqueue``.
"""

from __future__ import annotations

import threading

import structlog

from core.config import settings
from db.session import SessionLocal
from services.email_delivery import EmailJob, build_delivery_service
from services.email_metrics import get_email_metrics
from services.email_provider import get_email_provider
from services.email_templates import get_email_template_renderer
from services.job_queue import ThreadPoolJobQueue

logger = structlog.get_logger(__name__)


def run_email_job(job: EmailJob) -> None:
    """Process one queued delivery on its own session.

    Never raises: :meth:`~services.email_delivery.EmailDeliveryService.process`
    records every failure on the delivery row, and the queue catches anything that
    escapes even that.
    """
    session = SessionLocal()
    try:
        service = build_delivery_service(
            session,
            provider=get_email_provider(),
            templates=get_email_template_renderer(),
            # No queue: a send does not schedule more sends. The sweeper is the
            # only thing that enqueues, and it holds its own service.
            queue=None,
            metrics=get_email_metrics(),
        )
        service.process(job)
    finally:
        session.close()


#: The application's email queue.
#:
#: A module-level singleton because the pool it owns is a process-wide resource:
#: one pool of ``EMAIL_WORKER_CONCURRENCY`` threads, shared by every notification
#: that produces an email, is the whole point of bounding it. Started and stopped
#: by the application lifespan (see :mod:`core.lifespan`); a queue that was never
#: started still works, because
#: :meth:`~services.job_queue.ThreadPoolJobQueue.enqueue` starts it on first use.
email_queue: ThreadPoolJobQueue[EmailJob] = ThreadPoolJobQueue(
    run_email_job, max_workers=settings.EMAIL_WORKER_CONCURRENCY, name="email"
)


class EmailRetrySweeper:
    """A timer thread that re-queues due retries and rescues stranded sends.

    A ``threading.Timer``-style loop rather than a scheduler dependency, and
    rather than folding the work into the request path: this is the platform's
    first piece of *periodic* background work, and introducing APScheduler or
    Celery beat for it would mean a new dependency and — for Celery — a new
    deployable, inside a feature whose spec is about sending mail.

    What it does is small enough to justify that: two bounded queries and a
    handful of ``enqueue`` calls per pass. What it demonstrably is **not** is a
    general scheduler, and the honest limits are worth stating because they decide
    when it should be replaced:

    * it is **per process** — two API instances each sweep, and both may enqueue
      the same delivery. That is safe rather than merely tolerable: the claim is a
      conditional ``UPDATE``, so exactly one of them sends;
    * it does not survive the process, which is why the **startup pass** exists;
    * its resolution is ``EMAIL_RETRY_INTERVAL_SECONDS``, so a backoff shorter
      than the interval is rounded up to it. Deliberate — a delivery that becomes
      due one second after a pass waits for the next one, and a schedule accurate
      to the minute is exactly as useful as one accurate to the second for mail.

    The reminder scheduling ``16-notifications.md`` left out of scope is the
    feature that would outgrow this, and it is the one that should bring a real
    scheduler with it.
    """

    #: How long the loop waits between checks for the stop signal. Short enough
    #: that shutdown is not perceptibly delayed by a long sweep interval, which is
    #: the whole reason the wait is a poll rather than a single long sleep.
    _POLL_SECONDS = 0.5

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.RLock()

    # ------------------------------------------------------------ lifecycle #

    def start(self) -> None:
        """Sweep once, then keep sweeping on an interval. Idempotent.

        The **first pass is synchronous and happens at startup**, before the
        thread begins its loop, and that is the recovery half: a process stopped
        with deliveries queued leaves rows whose schedule lived only in its
        memory, and nothing else on the platform would ever pick them up.
        """
        with self._lock:
            if self._thread is not None:
                return
            self._stopping.clear()
            thread = threading.Thread(
                target=self._sweep_forever, name="email-sweeper", daemon=True
            )
            self._thread = thread

        self.sweep_once()
        thread.start()
        logger.info(
            "email_sweeper_started", interval_seconds=settings.EMAIL_RETRY_INTERVAL_SECONDS
        )

    def stop(self, *, timeout: float = 2.0) -> None:
        """Stop the loop. Idempotent.

        Does **not** drain, unlike the notification subscriber's shutdown, and the
        difference is what each one would lose. An undrained notification is a
        person never told something, with no other record of it; an unswept
        delivery is a row sitting in ``pending`` with a time on it, which the next
        process's startup pass finds. There is nothing to wait for.
        """
        with self._lock:
            thread, self._thread = self._thread, None

        if thread is None:
            return

        self._stopping.set()
        # Guarded because `start` registers the thread before it starts it — the
        # first sweep runs synchronously in between — so a shutdown arriving in
        # that window would otherwise join a thread that was never started, which
        # raises rather than returning.
        if thread.is_alive():
            thread.join(timeout=timeout)
        logger.info("email_sweeper_stopped")

    @property
    def is_running(self) -> bool:
        """Whether the sweeper is looping."""
        return self._thread is not None and not self._stopping.is_set()

    # --------------------------------------------------------------- work #

    def sweep_once(self) -> int:
        """Run one pass on its own session. Never raises.

        Returns:
            How many deliveries were handed back to the queue.
        """
        session = SessionLocal()
        try:
            service = build_delivery_service(
                session,
                provider=get_email_provider(),
                templates=get_email_template_renderer(),
                queue=email_queue,
                metrics=get_email_metrics(),
            )
            return service.sweep()
        except Exception:
            # A sweep that failed is a sweep the next one repeats; it must not
            # take the timer thread down with it, because that would silently stop
            # every retry on the platform while the API kept serving normally —
            # the same reasoning `ConnectionManager._dispatch_forever` records.
            logger.exception("email_sweep_pass_failed")
            return 0
        finally:
            session.close()

    def _sweep_forever(self) -> None:
        """Sweep on an interval until stopped. Runs on the sweeper thread.

        The interval is waited out in short slices rather than as one long sleep,
        so a shutdown during a quiet period is not delayed by however long is left
        of it — the same reason the notification worker polls its queue with a
        timeout instead of blocking on it.
        """
        interval = max(1, settings.EMAIL_RETRY_INTERVAL_SECONDS)
        while True:
            waited = 0.0
            while waited < interval:
                if self._stopping.wait(self._POLL_SECONDS):
                    return
                waited += self._POLL_SECONDS
            self.sweep_once()


#: The one sweeper the process shares.
email_sweeper = EmailRetrySweeper()


def start_email_workers() -> None:
    """Start the worker pool and the retry sweeper.

    Neither half is allowed to abort startup. An API that refused to come up
    because its mail pool could not be created would take down authentication,
    cases, and documents over a delivery channel every screen works without — the
    same contract :func:`~services.report_worker.start_report_workers` keeps.
    """
    if not settings.EMAIL_ENABLED:
        logger.info("email_workers_disabled")
        return

    try:
        email_queue.start()
    except Exception:
        logger.exception("email_workers_start_failed")
        return

    try:
        email_sweeper.start()
    except Exception:
        # The pool is already running, so new mail still goes out; what is lost is
        # the retry of anything that fails. Worth a loud log and not worth
        # unwinding a working half of the feature.
        logger.exception("email_sweeper_start_failed")


def stop_email_workers() -> None:
    """Stop the sweeper and drain the worker pool on shutdown.

    The sweeper first, so it cannot queue new work into a pool that is closing.
    The pool is then **drained** rather than cancelled: a send stopped mid-flight
    leaves its row at ``sending``, which is the one state no other worker will
    claim until the stale reclaim finds it — and waiting a few seconds is cheaper
    than a message that arrives ten minutes late.
    """
    email_sweeper.stop()
    email_queue.shutdown(wait=True)


__all__ = [
    "EmailRetrySweeper",
    "email_queue",
    "email_sweeper",
    "run_email_job",
    "start_email_workers",
    "stop_email_workers",
]
