"""The background WhatsApp worker, and the retry sweeper beside it.

The one place that knows both halves of the picture: it builds a
:class:`~services.whatsapp_delivery.WhatsAppDeliveryService` for a job and hands
it to a queue. Keeping it separate from both is what lets
:mod:`services.job_queue` stay ignorant of the service (it is injected a callable)
and :mod:`services.whatsapp_delivery` stay ignorant of the queue's implementation
(it is injected a protocol) — neither imports the other, so there is no cycle to
work around with a deferred import. The same shape as
:mod:`services.email_worker`, :mod:`services.ocr_worker`,
:mod:`services.indexing_worker`, and :mod:`services.report_worker`.

**Each job gets its own database session.** A worker runs on a background thread
long after the notification that queued it was created, and a SQLAlchemy
``Session`` is not thread-safe — reusing another thread's would be a
use-after-free with extra steps. The session is opened here, per job, and closed
in a ``finally``, so a connection cannot leak however the send ends.

**Its own pool, and a fifth one is not extravagance.** OCR is subprocess-heavy,
indexing holds a two-gigabyte model, a report is a burst of calls to a metered
API, email is a round trip to a relay, and WhatsApp is a round trip to an API that
**rate-limits per business phone number**. That last one is the reason this pool
cannot be shared with email's: a relay that starts greylisting must not be able to
occupy the threads that deliver hearing updates, and a WhatsApp number that starts
getting ``429``s must not slow down password-reset mail. ``WHATSAPP_WORKER_CONCURRENCY``
defaults to **2** — enough that one hung request does not stop the queue, small
enough that the platform never bursts hard enough to be throttled for it.

**The sweeper is the same machinery the email channel introduced**, and reusing
it unchanged is the point: a transient failure writes a ``next_attempt_at`` onto
the row and returns the worker thread immediately; the sweeper wakes on an
interval, re-queues everything that has come due, and rescues anything a dead
process left stranded in ``sending``. It is a second instance of a proven pattern
rather than a second implementation of it — the two sweepers differ only in which
service they build, which is the strongest argument available that the pattern was
the right shape.

The service the worker builds is deliberately given **no** queue, unlike the
sweeper's: a WhatsApp job does not enqueue more work, and the sweeper *is* the
enqueueing path, so a bug cannot turn into a loop — :meth:`process` never calls
``enqueue``.
"""

from __future__ import annotations

import threading

import structlog

from core.config import settings
from db.session import SessionLocal
from services.job_queue import ThreadPoolJobQueue
from services.whatsapp_delivery import WhatsAppJob, build_whatsapp_delivery_service
from services.whatsapp_metrics import get_whatsapp_metrics
from services.whatsapp_provider import get_whatsapp_provider
from services.whatsapp_templates import get_whatsapp_template_renderer

logger = structlog.get_logger(__name__)


def run_whatsapp_job(job: WhatsAppJob) -> None:
    """Process one queued delivery on its own session.

    Never raises:
    :meth:`~services.whatsapp_delivery.WhatsAppDeliveryService.process` records
    every failure on the delivery row, and the queue catches anything that escapes
    even that.
    """
    session = SessionLocal()
    try:
        service = build_whatsapp_delivery_service(
            session,
            provider=get_whatsapp_provider(),
            templates=get_whatsapp_template_renderer(),
            # No queue: a send does not schedule more sends. The sweeper is the
            # only thing that enqueues, and it holds its own service.
            queue=None,
            metrics=get_whatsapp_metrics(),
        )
        service.process(job)
    finally:
        session.close()


#: The application's WhatsApp queue.
#:
#: A module-level singleton because the pool it owns is a process-wide resource:
#: one pool of ``WHATSAPP_WORKER_CONCURRENCY`` threads, shared by every
#: notification that produces a message, is the whole point of bounding it.
#: Started and stopped by the application lifespan (see :mod:`core.lifespan`); a
#: queue that was never started still works, because
#: :meth:`~services.job_queue.ThreadPoolJobQueue.enqueue` starts it on first use.
whatsapp_queue: ThreadPoolJobQueue[WhatsAppJob] = ThreadPoolJobQueue(
    run_whatsapp_job, max_workers=settings.WHATSAPP_WORKER_CONCURRENCY, name="whatsapp"
)


class WhatsAppRetrySweeper:
    """A timer thread that re-queues due retries and rescues stranded sends.

    A ``threading.Timer``-style loop rather than a scheduler dependency, matching
    :class:`~services.email_worker.EmailRetrySweeper` exactly — and the honest
    limits are the same three, worth restating because they decide when both
    should be replaced together:

    * it is **per process** — two API instances each sweep, and both may enqueue
      the same delivery. That is safe rather than merely tolerable: the claim is a
      conditional ``UPDATE``, so exactly one of them sends;
    * it does not survive the process, which is why the **startup pass** exists;
    * its resolution is ``WHATSAPP_RETRY_INTERVAL_SECONDS``, so a backoff shorter
      than the interval is rounded up to it.

    The reminder scheduling ``16-notifications.md`` left out of scope is still the
    feature that would outgrow this — and it now has two of these to replace rather
    than one, which is itself an argument for a real scheduler arriving with it.
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

        The **first pass is synchronous and happens at startup**, before the thread
        begins its loop, and that is the recovery half: a process stopped with
        deliveries queued leaves rows whose schedule lived only in its memory, and
        nothing else on the platform would ever pick them up.
        """
        with self._lock:
            if self._thread is not None:
                return
            self._stopping.clear()
            thread = threading.Thread(
                target=self._sweep_forever, name="whatsapp-sweeper", daemon=True
            )
            self._thread = thread

        self.sweep_once()
        thread.start()
        logger.info(
            "whatsapp_sweeper_started",
            interval_seconds=settings.WHATSAPP_RETRY_INTERVAL_SECONDS,
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
        logger.info("whatsapp_sweeper_stopped")

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
            service = build_whatsapp_delivery_service(
                session,
                provider=get_whatsapp_provider(),
                templates=get_whatsapp_template_renderer(),
                queue=whatsapp_queue,
                metrics=get_whatsapp_metrics(),
            )
            return service.sweep()
        except Exception:
            # A sweep that failed is a sweep the next one repeats; it must not take
            # the timer thread down with it, because that would silently stop every
            # retry on this channel while the API kept serving normally.
            logger.exception("whatsapp_sweep_pass_failed")
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
        interval = max(1, settings.WHATSAPP_RETRY_INTERVAL_SECONDS)
        while True:
            waited = 0.0
            while waited < interval:
                if self._stopping.wait(self._POLL_SECONDS):
                    return
                waited += self._POLL_SECONDS
            self.sweep_once()


#: The one sweeper the process shares.
whatsapp_sweeper = WhatsAppRetrySweeper()


def start_whatsapp_workers() -> None:
    """Start the worker pool and the retry sweeper.

    Neither half is allowed to abort startup. An API that refused to come up
    because its messaging pool could not be created would take down
    authentication, cases, and documents over a delivery channel every screen works
    without — the same contract
    :func:`~services.email_worker.start_email_workers` keeps.

    **This is also where the spec's "validate configuration during startup" is
    honoured.** The provider's missing settings are logged **by name** the moment
    the channel is switched on, so a deployment that set ``WHATSAPP_ENABLED=true``
    and forgot a token finds out at boot rather than from a monitoring endpoint
    nobody was watching. It is logged rather than raised, deliberately and for the
    reason above: this is a channel every screen works without, and refusing to
    start over it would be a strictly worse outcome than starting and saying so.
    """
    if not settings.WHATSAPP_ENABLED:
        logger.info("whatsapp_workers_disabled")
        return

    provider = get_whatsapp_provider()
    missing = provider.configuration_errors()
    if missing:
        # Setting **names**, never values — the whole point of
        # `configuration_errors` returning a list rather than a sentence.
        logger.warning(
            "whatsapp_configuration_incomplete",
            provider=provider.name,
            missing=missing,
        )
    elif not settings.whatsapp_base_url:
        # Not a failure and not blocking: messages go out correct and linkless.
        # Worth an info line because "why do the messages have no link?" is
        # otherwise a puzzle with no trace of its cause.
        logger.info("whatsapp_base_url_unset")

    try:
        whatsapp_queue.start()
    except Exception:
        logger.exception("whatsapp_workers_start_failed")
        return

    try:
        whatsapp_sweeper.start()
    except Exception:
        # The pool is already running, so new messages still go out; what is lost
        # is the retry of anything that fails. Worth a loud log and not worth
        # unwinding a working half of the feature.
        logger.exception("whatsapp_sweeper_start_failed")


def stop_whatsapp_workers() -> None:
    """Stop the sweeper and drain the worker pool on shutdown.

    The sweeper first, so it cannot queue new work into a pool that is closing. The
    pool is then **drained** rather than cancelled: a send stopped mid-flight
    leaves its row at ``sending``, which is the one state no other worker will
    claim until the stale reclaim finds it — and waiting a few seconds is cheaper
    than a hearing update that arrives ten minutes late.
    """
    whatsapp_sweeper.stop()
    whatsapp_queue.shutdown(wait=True)


__all__ = [
    "WhatsAppRetrySweeper",
    "run_whatsapp_job",
    "start_whatsapp_workers",
    "stop_whatsapp_workers",
    "whatsapp_queue",
    "whatsapp_sweeper",
]
