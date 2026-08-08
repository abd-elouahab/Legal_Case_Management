"""The event dispatcher: the platform's single publication point.

``15-real-time-synchronization.md`` states the rule this module exists to make
structural: *"Application modules should never communicate directly with
WebSockets. Everything should go through the dispatcher"*, and *"Business modules
should publish events but should never know who consumes them."*

The shape is the one :mod:`services.timeline` already established for the
timeline, and it is deliberately the same shape so nobody has to learn a second
one:

* :class:`EventPublisher` is the **narrow half** a business module depends on.
  One method. The case service, the document service, the OCR service, the
  indexing service, and the report agent all take this protocol — never
  :class:`EventDispatcher` — so none of them can reach a subscriber, a
  connection, or the socket layer, and a unit test for any of them substitutes a
  recorder with no infrastructure at all.
* :class:`NullEventPublisher` publishes nothing and is the default for a service
  built without one, so publishing code is a plain call rather than an
  ``if self._events`` at every site — which is where a forgotten branch would
  silently drop events.
* :class:`EventSubscriber` is the **other** narrow half. The WebSocket connection
  manager implements it; so, in future, will the notification service, the email
  sender, the WhatsApp sender, and the analytics collector. The dispatcher knows
  none of them by name.
* :class:`EventDispatcher` is the implementation, and it is the *only* object
  that sees both halves.

**Why in-process rather than Redis Pub/Sub.** `architecture.md` lists Redis for
"WebSocket Pub/Sub", and that is the right answer for *several API processes* —
an event published on instance A must reach a client connected to instance B.
This ships the in-process dispatcher because the seam, not the transport, is what
the spec asks for: :class:`EventSubscriber` is a protocol, and a
``RedisEventBridge`` that subscribes to this dispatcher, publishes onto a Redis
channel, and re-publishes what it receives from other instances is **one class
plus one line in** :mod:`api.deps` — with no business module, no connection, and
no client changing. The honest limits of the in-process form are recorded on
:class:`EventDispatcher` rather than left to be discovered.

**Publishing never raises, and never blocks the publisher on a consumer.** An
event is a side effect of a change that has already committed; a subscriber that
throws must not turn a successful upload into a 500, and a subscriber that is
slow must not make one. So every subscriber call is wrapped, and the WebSocket
subscriber — the only one that ships — hands the event to a per-connection queue
rather than to a socket. Publishing is O(subscribers), not O(clients).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import structlog

from core.events import (
    DomainEvent,
    DomainEventType,
    EventTopic,
    InvalidEventPayloadError,
    UnknownEventScopeError,
    scope_for,
    screen_payload,
)
from services.event_metrics import (
    NullRealtimeMetrics,
    RealtimeMetricsRecorder,
    get_realtime_metrics,
)

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# The two narrow halves
# --------------------------------------------------------------------------- #


class EventPublisher(Protocol):
    """What a business module requires in order to announce something.

    One method, and it deliberately takes primitives rather than a built
    :class:`~core.events.DomainEvent`: the *identity* and *sequence* of an event
    are the dispatcher's to assign, and a publisher that supplied its own would
    make deduplication a matter of publisher discipline rather than a property of
    the system.

    A module holding this can publish and can do nothing else — it cannot read
    the subscriber list, cannot reach a connection, and cannot discover who (if
    anyone) is listening.
    """

    def publish(
        self,
        *,
        event_type: DomainEventType,
        topic: EventTopic,
        case_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> DomainEvent | None:
        """Announce one event. Never raises."""
        ...


class EventSubscriber(Protocol):
    """What the dispatcher requires of a consumer.

    One method, called synchronously on the publishing thread. An implementation
    that does real work — a network call, a database write, an email — **must
    hand it off** rather than perform it here, because the publisher is inside a
    request or a worker job that has already earned its response. The connection
    manager that ships does exactly that: it enqueues onto per-connection
    asyncio queues and returns.
    """

    #: A stable identifier for this subscriber, used in logs and metrics.
    name: str

    def handle(self, event: DomainEvent) -> None:
        """Consume one event. Must not raise; must not block."""
        ...


class NullEventPublisher:
    """A publisher that announces nothing.

    The default for a service constructed without one — a script, a migration
    helper, or a unit test that is not about events. Same role, and same
    reasoning, as :class:`~services.timeline.NullTimelineRecorder` and
    :class:`~services.job_queue.NullJobQueue`.

    The application wires the real dispatcher in :mod:`api.deps`; a test asserts
    that it does, so "the default is a no-op" cannot quietly become the
    production behaviour.
    """

    def publish(
        self,
        *,
        event_type: DomainEventType,
        topic: EventTopic,
        case_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> DomainEvent | None:
        """Discard the event and return ``None``."""
        return None


class RecordingEventPublisher:
    """A publisher that keeps what it was given, in order.

    For tests: it makes "did uploading a document announce it?" an assertion
    about a list rather than about a socket. Not used by the application.
    """

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []
        self._sequence = 0

    def publish(
        self,
        *,
        event_type: DomainEventType,
        topic: EventTopic,
        case_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> DomainEvent | None:
        """Build the event and remember it."""
        self._sequence += 1
        event = DomainEvent.create(
            event_type=event_type,
            topic=topic,
            sequence=self._sequence,
            case_id=case_id,
            actor_id=actor_id,
            payload=payload,
        )
        self.events.append(event)
        return event

    def types(self) -> list[DomainEventType]:
        """The event types recorded, in order. A readable assertion target."""
        return [event.event_type for event in self.events]

    def reset(self) -> None:
        """Forget everything."""
        self.events.clear()


# --------------------------------------------------------------------------- #
# The dispatcher
# --------------------------------------------------------------------------- #


class EventDispatcher:
    """Publishes domain events to registered subscribers.

    **What this class guarantees.**

    * *Producers are isolated from consumers.* A publisher passes a type, a
      topic, and a payload; it learns nothing about who received it, not even how
      many did — :meth:`publish` returns the event rather than a delivery count,
      deliberately, so no business rule can come to depend on there being a
      listener.
    * *Publishing never fails the caller.* A subscriber that raises is logged and
      skipped; the remaining subscribers still receive the event. A payload the
      publisher built wrongly is logged and dropped. Nothing propagates.
    * *Every event is uniquely identified and totally ordered.* The sequence is
      assigned under the same lock that hands the event to subscribers, so two
      threads publishing at once produce two distinct, ordered events — which is
      what makes a gap on a client detectable rather than ambiguous.
    * *Confidential material cannot travel.* Payload screening
      (:func:`~core.events.screen_payload`) happens **here**, once, rather than
      in each publisher, and a screened key is logged by name so the publisher's
      mistake is visible.

    **What it does not do**, stated plainly because it decides when to replace it:

    * it does not cross process boundaries — an event published in this API
      process reaches only the clients connected to *this* process. With one API
      instance that is the whole platform; with several, a ``RedisEventBridge``
      registered as one more :class:`EventSubscriber` is the fix, and nothing
      above this line changes;
    * it does not persist events, so an event published while nobody was
      connected is gone. That is correct for *synchronization* — a client that
      reconnects refetches, which is authoritative where a replayed event is only
      a hint — and it is not correct for *notifications*, which is why
      Notifications is specified as a persistent module of its own and is out of
      this feature's scope;
    * it has no retry and no dead letter. A subscriber that fails has failed;
      the event is not redelivered, because redelivering to the socket layer
      would be delivering a duplicate to fix a delivery.
    """

    def __init__(self, *, metrics: RealtimeMetricsRecorder | None = None) -> None:
        self._metrics = metrics or NullRealtimeMetrics()
        #: Guards both the subscriber set and the sequence counter. One lock
        #: rather than two, because assigning a sequence and delivering under it
        #: must be atomic — otherwise two events can reach a client in the
        #: opposite order to their numbers, which is precisely the confusion the
        #: number exists to remove.
        self._lock = threading.RLock()
        self._subscribers: dict[str, EventSubscriber] = {}
        self._sequence = 0

    # ---------------------------------------------------------- subscribers #

    def subscribe(self, subscriber: EventSubscriber) -> None:
        """Register a consumer. Idempotent by :attr:`EventSubscriber.name`.

        Idempotent because the alternative — a second registration under the same
        name — is how a restarted component ends up receiving every event twice,
        and a duplicate delivery is exactly what the spec asks this system to
        avoid.
        """
        with self._lock:
            replaced = subscriber.name in self._subscribers
            self._subscribers[subscriber.name] = subscriber

        logger.info(
            "event_subscriber_registered", subscriber=subscriber.name, replaced=replaced
        )

    def unsubscribe(self, name: str) -> None:
        """Remove a consumer. Silent if it was never registered."""
        with self._lock:
            removed = self._subscribers.pop(name, None)

        if removed is not None:
            logger.info("event_subscriber_removed", subscriber=name)

    @property
    def subscriber_names(self) -> list[str]:
        """The registered consumers, for the monitoring view."""
        with self._lock:
            return sorted(self._subscribers)

    @property
    def current_sequence(self) -> int:
        """The sequence most recently assigned.

        Read-only and published, because it is a **client-visible contract**: a
        connection is told it on ``ready`` and on every ``pong``, which is what
        lets a client that has been idle notice it has fallen behind without the
        server tracking what each of them has seen. There is deliberately no
        setter — the counter is the dispatcher's alone, and a caller that could
        move it could make every connected client believe it had missed events.
        """
        with self._lock:
            return self._sequence

    # ------------------------------------------------------------ publishing #

    def publish(
        self,
        *,
        event_type: DomainEventType,
        topic: EventTopic,
        case_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> DomainEvent | None:
        """Announce one event to every registered subscriber.

        Args:
            event_type: a member of the central registry.
            topic: what the event is about, and therefore who may receive it.
                Built by the publisher from the row it just wrote — never from
                anything a client supplied.
            case_id: the case to refresh, when the topic is not itself a case.
                Carried so a client receiving a *document* event knows which case
                workspace is stale without a round trip to find out.
            actor_id: who caused it, when a user did.
            payload: the few identifiers and statuses a client needs. Screened
                here; see :func:`~core.events.screen_payload`.

        Returns:
            The published event, or ``None`` if it could not be built. The event
            is returned rather than a delivery count **on purpose**: a publisher
            that could see how many consumers exist is a publisher that could
            start behaving differently when none do.

        **Never raises**, for the reason recorded on the class: the business
        change is already committed by the time this is called.
        """
        try:
            event = self._build(
                event_type=event_type,
                topic=topic,
                case_id=case_id,
                actor_id=actor_id,
                payload=payload,
            )
        except (InvalidEventPayloadError, UnknownEventScopeError) as exc:
            # A publisher's bug, not a user's. Logged loudly and by *code* — the
            # payload that caused it is exactly what must not reach a log.
            logger.error(
                "event_publication_rejected",
                event_type=event_type.value,
                scope=topic.scope.value,
                reason=str(exc),
            )
            self._metrics.record_publish_rejected()
            return None
        except Exception:  # pragma: no cover - defensive
            logger.exception("event_publication_failed", event_type=event_type.value)
            self._metrics.record_publish_rejected()
            return None

        self._deliver(event)
        return event

    def _build(
        self,
        *,
        event_type: DomainEventType,
        topic: EventTopic,
        case_id: uuid.UUID | None,
        actor_id: uuid.UUID | None,
        payload: Mapping[str, Any] | None,
    ) -> DomainEvent:
        """Screen the payload, check the scope, and stamp the sequence."""
        expected_scope = scope_for(event_type)
        if topic.scope is not expected_scope:
            raise UnknownEventScopeError(
                f"Event {event_type.value!r} is scoped {expected_scope.value!r} but was published "
                f"on a {topic.scope.value!r} topic."
            )

        screened, dropped = screen_payload(payload)
        if dropped:
            # By name only. That a publisher tried to put `full_text` on an event
            # is a bug worth seeing; what was in it is the thing this whole
            # module exists to keep off the wire.
            logger.error(
                "event_payload_screened",
                event_type=event_type.value,
                dropped_keys=sorted(dropped),
            )

        with self._lock:
            self._sequence += 1
            sequence = self._sequence

        return DomainEvent.create(
            event_type=event_type,
            topic=topic,
            sequence=sequence,
            case_id=case_id,
            actor_id=actor_id,
            payload=screened,
        )

    def _deliver(self, event: DomainEvent) -> None:
        """Hand the event to every subscriber, isolating each one's failures."""
        with self._lock:
            subscribers = list(self._subscribers.values())

        self._metrics.record_published(event.event_type)

        for subscriber in subscribers:
            try:
                subscriber.handle(event)
            except Exception:
                # One broken consumer must not deny the event to the others, and
                # must not fail the request that produced it.
                logger.exception(
                    "event_subscriber_failed",
                    subscriber=subscriber.name,
                    event_type=event.event_type.value,
                    event_id=str(event.event_id),
                )
                self._metrics.record_subscriber_failure(subscriber.name)

        logger.debug(
            "event_published",
            # Identifiers, the type, and the scope. **Not the topic's resource
            # id** at debug level and never the payload: a topic names a case,
            # and a case is a client's matter.
            event_id=str(event.event_id),
            sequence=event.sequence,
            event_type=event.event_type.value,
            scope=event.scope.value,
            subscribers=len(subscribers),
        )

    # --------------------------------------------------------------- testing #

    def reset(self) -> None:
        """Drop every subscriber and restart the sequence.

        For tests, and for nothing else: the sequence is a client-visible
        contract, and restarting it in a running process would make a connected
        client believe it had jumped backwards in time. A real restart is a new
        process, where every client reconnects and resynchronizes anyway.
        """
        with self._lock:
            self._subscribers.clear()
            self._sequence = 0


# --------------------------------------------------------------------------- #
# The process-wide instance
# --------------------------------------------------------------------------- #

#: The one dispatcher the process shares.
#:
#: Module-level rather than per request for the reason the job queues and the
#: metrics recorders are: a dispatcher rebuilt per request would have no
#: subscribers, because the WebSocket manager registered with the one that
#: existed at startup. The *sequence* is a property of the process too — a
#: per-request counter would restart at one on every publish.
_shared = EventDispatcher(metrics=get_realtime_metrics())


def get_event_dispatcher() -> EventDispatcher:
    """Return the process-wide event dispatcher."""
    return _shared


def reset_event_dispatcher() -> None:
    """Clear the process-wide dispatcher. For tests."""
    _shared.reset()


__all__ = [
    "EventDispatcher",
    "EventPublisher",
    "EventSubscriber",
    "NullEventPublisher",
    "RecordingEventPublisher",
    "get_event_dispatcher",
    "reset_event_dispatcher",
]
