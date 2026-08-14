"""The Notification Service's subscription to the platform's event dispatcher.

This is the class ``services/events.py`` has been describing since Real-Time
Synchronization shipped — *"so, in future, will the notification service"* — and
it is deliberately the whole of what that took: one
:class:`~services.events.EventSubscriber` implementation and one ``subscribe``
call in :mod:`core.lifespan`. **No business module changed to make notifications
exist**, because none of them knows that consumers exist at all: they hold
:class:`~services.events.EventPublisher`, which has one method and no way to ask
who is listening.

**Why there is a queue and a worker thread.** ``EventSubscriber.handle`` is
called *synchronously, on the publishing thread* — which is a request thread
serving an upload, or an OCR worker finishing an extraction. Resolving
recipients, checking preferences, checking for duplicates, and inserting a batch
is four database round trips; doing that inline would add them to the latency of
an operation that has already committed and already earned its response. So
:meth:`NotificationEventSubscriber.handle` is a queue put and a return, and the
work happens on a thread of this module's own — the shape
:class:`~websocket.manager.ConnectionManager` established for exactly the same
reason.

Three properties fall out of that, and each is one of the spec's requirements:

* **failures are isolated** — an exception on the worker thread cannot reach the
  request that caused the event, because there is no longer a call stack
  connecting them. *"Notification failures should never affect business
  operations"* stops being a rule to remember and becomes a fact about threads;
* **delivery is asynchronous**, which is what ``code-standards.md``'s Background
  Jobs section asks for;
* **the backlog is bounded** — a burst the platform cannot keep up with is
  dropped and *counted* rather than accumulated in memory. Dropping is the right
  failure here: the alternative is blocking a publisher, which trades a lost
  notification for a stalled operation.

**This module decides nothing.** Which events matter, what they say, who they are
for, and how urgent they are all live in :data:`~core.notifications.EVENT_RULES`;
who may actually be told lives in
:class:`~services.notification_recipients.NotificationRecipientResolver`; and
persistence, preferences, duplicates, and delivery live in
:class:`~services.notification.NotificationService`. What is left here is the
plumbing between them, which is why there is not a single ``if event.event_type
is …`` in the file.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager

import structlog
from sqlalchemy.orm import Session

from core.config import settings
from core.events import DomainEvent
from core.notifications import build_context, rule_for, target_for
from repositories.notification import NotificationRepository
from services.events import EventPublisher, NullEventPublisher
from services.notification import NotificationDispatcher, NotificationService
from services.notification_metrics import (
    NotificationFailureReason,
    NotificationMetricsRecorder,
    NullNotificationMetrics,
)
from services.notification_recipients import NotificationRecipientResolver

logger = structlog.get_logger(__name__)

#: How long a worker waits for an event before checking whether it should stop.
#: Short enough that shutdown is not perceptibly delayed, long enough that an
#: idle process is not spinning. The same value, for the same reason, as
#: :data:`~websocket.manager._DISPATCH_POLL_SECONDS`.
_POLL_SECONDS = 0.25


class NotificationEventSubscriber:
    """Turns domain events into notifications, off the publisher's thread.

    Constructed once per process and registered on the dispatcher at startup by
    :mod:`core.lifespan`. It is **not** a request-scoped dependency: a subscriber
    per request would be registered on nothing and would own no worker, which is
    the same reason the job queues, the metrics recorders, and the connection
    manager are all process-wide.
    """

    #: The identifier the dispatcher registers this subscriber under.
    #:
    #: Registration is idempotent by this name (see
    #: :meth:`~services.events.EventDispatcher.subscribe`), which is what stops a
    #: restarted component from receiving every event twice.
    name = "notifications"

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
        *,
        recipients: NotificationRecipientResolver | None = None,
        publisher: EventPublisher | None = None,
        metrics: NotificationMetricsRecorder | None = None,
        channels: Callable[[Session], Sequence[NotificationDispatcher]] | None = None,
    ) -> None:
        # Imported lazily so that constructing a subscriber does not require an
        # importable engine — `db.session` builds one at import time, and a unit
        # test supplies a factory of its own. The same reasoning
        # `RealtimeAccessPolicy.__init__` and `NotificationRecipientResolver`
        # record.
        if session_factory is None:
            from db.session import SessionLocal

            session_factory = SessionLocal

        self._session_factory: Callable[[], AbstractContextManager[Session]] = session_factory
        self._recipients = recipients or NotificationRecipientResolver(session_factory)
        #: A **publisher**, never the dispatcher: this consumer announces the
        #: notifications it creates and must not be able to enumerate, register,
        #: or reach another consumer.
        self._publisher = publisher or NullEventPublisher()
        self._metrics = metrics or NullNotificationMetrics()
        #: Builds the delivery channels for one session. A **factory** rather than
        #: instances, because a channel holds repositories and a session is opened
        #: per event — the same reason this class takes a session factory rather
        #: than a session.
        #:
        #: Defaulting to *no channels* keeps a unit test's subscriber from
        #: touching a mail provider it never asked for; the application supplies
        #: the real one in :func:`get_notification_subscriber`, and a test asserts
        #: that it does, so "the default is nothing" cannot quietly become the
        #: production behaviour.
        self._channels: Callable[[Session], Sequence[NotificationDispatcher]] = (
            channels or (lambda _session: ())
        )

        self._inbox: queue.Queue[DomainEvent] = queue.Queue(
            maxsize=settings.NOTIFICATION_QUEUE_SIZE
        )
        self._workers: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._lock = threading.RLock()

    # ------------------------------------------------------------- lifecycle #

    def start(self) -> None:
        """Start the worker threads. Idempotent."""
        with self._lock:
            if self._workers:
                return
            self._stopping.clear()
            for index in range(max(1, settings.NOTIFICATION_WORKER_CONCURRENCY)):
                worker = threading.Thread(
                    target=self._work_forever,
                    name=f"notifications-{index}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)

        logger.info("notification_workers_started", workers=len(self._workers))

    def stop(self, *, timeout: float = 5.0) -> None:
        """Drain what is queued, then stop. Idempotent.

        Drains rather than abandoning, unlike the WebSocket manager's shutdown —
        and the difference is the difference between the two features. An
        undelivered *event* is nothing: the client reconnects and refetches. An
        uncreated *notification* is a person never being told something happened,
        and the row that would have said so does not exist anywhere else.

        Bounded by ``timeout`` all the same: a shutdown that waited indefinitely
        on a backlog is a shutdown a slow database can hold open.
        """
        with self._lock:
            workers, self._workers = self._workers, []

        if not workers:
            return

        # The workers are still running at this point, and deliberately: the
        # queue is drained *before* they are told to stop, because telling them
        # first is what would abandon the backlog.
        drained = self._drain(timeout=timeout)
        self._stopping.set()
        for worker in workers:
            worker.join(timeout=1.0)

        logger.info(
            "notification_workers_stopped",
            workers=len(workers),
            drained=drained,
            abandoned=self._inbox.qsize(),
        )

    def _drain(self, *, timeout: float) -> bool:
        """Wait for the workers to empty the queue, up to ``timeout`` seconds.

        Returns:
            Whether the queue actually emptied. ``False`` means the deadline
            arrived first and the remaining events were abandoned — which the
            caller logs as a count, because a shutdown that dropped work should
            be visible rather than inferred from a gap in somebody's feed.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while self._inbox.qsize() and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
        return not self._inbox.qsize()

    @property
    def is_running(self) -> bool:
        """Whether the workers are accepting events."""
        return bool(self._workers) and not self._stopping.is_set()

    @property
    def pending(self) -> int:
        """Events accepted and not yet turned into notifications. The backlog gauge."""
        return self._inbox.qsize()

    # -------------------------------------------------------------- the hook #

    def handle(self, event: DomainEvent) -> None:
        """Accept one event from the dispatcher.

        The :class:`~services.events.EventSubscriber` contract: **must not raise,
        must not block**. Both are satisfied by doing almost nothing here.

        The rule lookup *is* done inline, and deliberately: it is a dictionary
        read with no I/O, and it is what keeps the queue from filling with the
        events this feature has no interest in — the timeline updates and
        presence changes that make up most of the platform's traffic. Queueing
        them to discard them on a worker would be a bounded resource spent on
        events that were never going to produce anything.

        A full inbox drops the event and counts it. That is the right failure:
        the alternative is to block the publisher — a request that has already
        committed its change — until a queue drains, which trades a lost
        notification for a stalled operation.
        """
        if not settings.NOTIFICATIONS_ENABLED or self._stopping.is_set():
            return

        if rule_for(event.event_type, event.payload) is None:
            return

        try:
            self._inbox.put_nowait(event)
        except queue.Full:
            self._metrics.record_dropped()
            logger.warning(
                "notification_queue_overflow",
                event_type=event.event_type.value,
                pending=self._inbox.qsize(),
            )

    # ------------------------------------------------------------ the worker #

    def _work_forever(self) -> None:
        """Drain the inbox until stopped. Runs on a worker thread."""
        while not self._stopping.is_set():
            try:
                event = self._inbox.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                continue

            try:
                self.process(event)
            except Exception:
                # A fault must not take the worker down with it, because that
                # would silently stop every notification on the platform while
                # the API kept serving requests normally — the same reasoning
                # `ConnectionManager._dispatch_forever` records.
                self._metrics.record_failed(NotificationFailureReason.UNKNOWN)
                logger.exception(
                    "notification_processing_failed", event_type=event.event_type.value
                )
            finally:
                self._inbox.task_done()

    def process(self, event: DomainEvent) -> int:
        """Turn one event into notifications. Returns how many were created.

        Public and synchronous so the feature is testable **without a thread**:
        a test hands it an event and asserts about rows, rather than queueing one
        and waiting to see. The worker loop above is the only production caller.

        Every failure is a recorded, counted, logged outcome and never an
        exception that escapes — see the module docstring.
        """
        rule = rule_for(event.event_type, event.payload)
        if rule is None:  # pragma: no cover - filtered in `handle`
            return 0

        audience = self._recipients.resolve(event, rule)
        if audience.failed:
            self._metrics.record_failed(NotificationFailureReason.RECIPIENT_LOOKUP_FAILED)
            logger.warning(
                "notification_recipients_unresolved",
                event_type=event.event_type.value,
                rule=rule.key,
                reason=audience.reason,
            )
            return 0

        if audience.truncated:
            # Logged rather than silently trimmed: an audience that has quietly
            # stopped being complete is worse than one that is visibly bounded.
            logger.warning(
                "notification_audience_truncated",
                rule=rule.key,
                dropped=audience.truncated,
                ceiling=settings.NOTIFICATION_MAX_RECIPIENTS,
            )

        if audience.is_empty:
            logger.debug(
                "notification_no_recipients",
                event_type=event.event_type.value,
                rule=rule.key,
                reason=audience.reason,
            )
            return 0

        target = target_for(rule, case_id=audience.case_id, payload=event.payload)
        context = build_context(rule, event.payload)

        with self._session_factory() as session:
            service = NotificationService(
                NotificationRepository(session),
                events=self._publisher,
                metrics=self._metrics,
                # Built on this event's session, so a channel's writes share the
                # transaction boundary of the notifications it is delivering.
                channels=self._channels(session),
            )
            created = service.create(
                rule=rule,
                recipient_ids=audience.recipient_ids,
                context=context,
                case_id=audience.case_id,
                actor_id=event.actor_id,
                target=target,
                event_id=event.event_id,
                event_type=event.event_type.value,
                occurred_at=event.occurred_at,
            )

        return len(created)


def _delivery_channels(session: Session) -> Sequence[NotificationDispatcher]:
    """Build the delivery channels a created batch is offered to.

    The **worker-thread counterpart** of :func:`api.deps.get_notification_channels`,
    and deliberately assembled from the same shared providers, renderers, queues,
    and metrics recorders a request would get — which is what makes a message
    produced by this worker identical to one produced by any other path. The two
    functions exist separately because a worker thread has no request to resolve a
    dependency from, so **both have to be right**; a test asserts that each wires
    every channel.

    Imported inside the function rather than at module scope, and that placement is
    the interesting part: this module is the *notification* subscriber, and a
    top-level import of a delivery service would put a mail provider and an HTTP
    provider into the import graph of every unit test that touches notifications.
    It also keeps the dependency pointing the way it should — nothing in either
    channel imports anything from here.

    **Each channel is built only when it is switched on**, so a deployment with
    ``EMAIL_ENABLED=false`` and ``WHATSAPP_ENABLED=false`` constructs nothing at
    all per event — and one with only one of them on pays for only that one. The
    order is email then WhatsApp, matching :func:`api.deps.get_notification_channels`
    so that the two paths cannot differ even in something that does not matter.
    """
    channels: list[NotificationDispatcher] = []

    if settings.EMAIL_ENABLED:
        from services.email_delivery import build_delivery_service
        from services.email_metrics import get_email_metrics
        from services.email_provider import get_email_provider
        from services.email_templates import get_email_template_renderer
        from services.email_worker import email_queue

        channels.append(
            build_delivery_service(
                session,
                provider=get_email_provider(),
                templates=get_email_template_renderer(),
                queue=email_queue,
                metrics=get_email_metrics(),
            )
        )

    if settings.WHATSAPP_ENABLED:
        from services.whatsapp_delivery import build_whatsapp_delivery_service
        from services.whatsapp_metrics import get_whatsapp_metrics
        from services.whatsapp_provider import get_whatsapp_provider
        from services.whatsapp_templates import get_whatsapp_template_renderer
        from services.whatsapp_worker import whatsapp_queue

        channels.append(
            build_whatsapp_delivery_service(
                session,
                provider=get_whatsapp_provider(),
                templates=get_whatsapp_template_renderer(),
                queue=whatsapp_queue,
                metrics=get_whatsapp_metrics(),
            )
        )

    return tuple(channels)


#: The one subscriber the process shares.
#:
#: Module-level for the reason the dispatcher, the connection manager, and the
#: job queues are: it owns worker threads and a bounded queue, and a per-request
#: instance would own neither. It is handed the dispatcher **as a publisher**,
#: not as a dispatcher — this consumer announces what it creates and must not be
#: able to reach another consumer. The reverse direction, the dispatcher learning
#: about this subscriber, is wired in :func:`core.lifespan._start_notifications`
#: and nowhere else.
_shared: NotificationEventSubscriber | None = None
_shared_lock = threading.Lock()


def get_notification_subscriber() -> NotificationEventSubscriber:
    """Return the process-wide notification subscriber.

    Built on first use rather than at import, unlike the connection manager, and
    for one concrete reason: constructing it resolves ``db.session.SessionLocal``,
    which builds an engine — and a module-level instance would make importing
    *any* module that transitively reaches this one require a database URL that
    parses. The lazy form keeps a script, a migration, and a unit test able to
    import the notification vocabulary without one.
    """
    global _shared
    with _shared_lock:
        if _shared is None:
            from services.events import get_event_dispatcher
            from services.notification_metrics import get_notification_metrics

            _shared = NotificationEventSubscriber(
                publisher=get_event_dispatcher(),
                metrics=get_notification_metrics(),
                channels=_delivery_channels,
            )
        return _shared


def reset_notification_subscriber() -> None:
    """Discard the process-wide subscriber. For tests."""
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.stop(timeout=0.0)
        _shared = None


__all__ = [
    "NotificationEventSubscriber",
    "get_notification_subscriber",
    "reset_notification_subscriber",
]
