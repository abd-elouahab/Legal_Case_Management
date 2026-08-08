"""Every connection at once: routing, authorization, and presence.

This is the platform's **single** :class:`~services.events.EventSubscriber`. It is
the one object in the system that sees both a domain event and a socket, and it
is deliberately the only one: business modules publish to the dispatcher, the
dispatcher hands events here, and everything about *who gets what* is decided in
this file.

**Three threads' worth of concerns, kept apart.**

The API serves its synchronous endpoints on a thread pool and its background jobs
on two more (OCR, indexing, reports), while the WebSockets live on the asyncio
event loop. An event can therefore be published from any thread and must reach
sockets on exactly one. Rather than making every publisher aware of that, the
manager owns a **dispatch thread**:

    publisher (any thread) → handle() → dispatch queue → dispatch thread
        → authorization (database) → loop.call_soon_threadsafe → connection queue
            → endpoint's writer task → socket

Three properties fall out of that shape, and each is one of the spec's
requirements:

* :meth:`handle` **never blocks a publisher** — it is a queue put and a return,
  so uploading a document costs the uploader nothing whether zero or five hundred
  clients are connected;
* **no database work ever runs on the event loop** — re-authorizing a stale grant
  is a query, and doing it inline would stall every other connection in the
  process while it ran;
* **delivery is bounded** — the dispatch queue and every connection queue are
  both finite, so a burst the platform cannot deliver is dropped and *counted*
  rather than accumulated in memory.

**Authorization is applied here, on every delivery, and it is not a formality.**
A connection's grant carries the moment it was authorized; past
``REALTIME_AUTHORIZATION_TTL_SECONDS`` the grant is re-resolved against the
database — including the account itself, so a deactivated user's open socket goes
quiet — and a refusal *revokes* the subscription rather than merely skipping the
event. See :mod:`services.realtime_access` for the whole rule and for the bound
the TTL creates.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import structlog

from core.config import settings
from core.events import (
    CASE_FANOUT_SCOPES,
    DomainEvent,
    DomainEventType,
    EventTopic,
    case_topic,
    user_topic,
)
from core.realtime import RealtimeErrorCode
from models.user import User
from services.event_metrics import (
    NullRealtimeMetrics,
    RealtimeMetricsRecorder,
    RealtimeMetricsSnapshot,
    get_realtime_metrics,
)
from services.events import EventPublisher, NullEventPublisher, get_event_dispatcher
from services.realtime_access import RealtimeAccessPolicy
from websocket.connection import ClientConnection
from websocket.protocol import encode_event

logger = structlog.get_logger(__name__)

#: How long the dispatch thread waits for work before checking whether it should
#: stop. Short enough that shutdown is not perceptibly delayed, long enough that
#: an idle process is not spinning.
_DISPATCH_POLL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class PresenceEntry:
    """One account that currently holds at least one connection.

    Deliberately **counts rather than lists** connections: how many tabs somebody
    has open is not information anyone needs, and an operator reading the presence
    roster is asking who is working, not from how many devices. The connection
    identifiers stay inside the manager.
    """

    user_id: uuid.UUID
    role: str
    connections: int
    #: When this account's *oldest* live connection was established — i.e. how
    #: long they have been present, not how long the newest tab has been open.
    since: datetime


class ConnectionManager:
    """Holds every live connection and routes events to the ones entitled to them.

    Constructed once per process and registered on the dispatcher at startup by
    :mod:`core.lifespan`. It is **not** a request-scoped dependency: a manager per
    request would hold no connections, which is the same reason the job queues and
    the metrics recorders are process-wide.
    """

    #: The identifier the dispatcher registers this subscriber under.
    name = "websocket"

    def __init__(
        self,
        *,
        access: RealtimeAccessPolicy | None = None,
        metrics: RealtimeMetricsRecorder | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._access = access or RealtimeAccessPolicy()
        self._metrics = metrics or NullRealtimeMetrics()
        #: Used only to announce presence changes. A publisher rather than the
        #: dispatcher itself, so the manager can announce and cannot subscribe,
        #: enumerate, or reach another consumer.
        self._publisher = publisher or NullEventPublisher()

        self._lock = threading.RLock()
        self._connections: dict[uuid.UUID, ClientConnection] = {}
        #: topic → connection ids. The routing index: without it, delivering one
        #: event would mean asking every connection whether it cares, which is
        #: O(connections) per event and is how a broadcast becomes a bottleneck.
        self._by_topic: dict[EventTopic, set[uuid.UUID]] = {}
        #: account → connection ids. Presence, and the per-account ceiling.
        self._by_user: dict[uuid.UUID, set[uuid.UUID]] = {}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._inbox: queue.Queue[DomainEvent] = queue.Queue(
            maxsize=max(64, settings.REALTIME_SEND_QUEUE_SIZE * 4)
        )
        self._dispatcher_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------- lifecycle #

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to the running event loop and start the dispatch thread.

        The loop is captured rather than looked up per event because
        :func:`asyncio.get_running_loop` fails on the dispatch thread by
        definition — it is not the loop's thread, which is the entire point of it.
        """
        with self._lock:
            if self._dispatcher_thread is not None:
                return
            self._loop = loop
            self._stopping.clear()
            self._dispatcher_thread = threading.Thread(
                target=self._dispatch_forever, name="realtime-dispatch", daemon=True
            )
            self._dispatcher_thread.start()

        logger.info("realtime_manager_started")

    def stop(self) -> None:
        """Stop dispatching and close every connection. Idempotent.

        Connections are marked closed rather than awaited: the endpoint tasks own
        the sockets and notice on their next write, and a shutdown that blocked on
        a client that is not reading would be a shutdown a slow client could hold
        open.
        """
        with self._lock:
            thread, self._dispatcher_thread = self._dispatcher_thread, None
            connections = list(self._connections.values())

        self._stopping.set()
        if thread is not None:
            thread.join(timeout=2.0)

        for connection in connections:
            connection.mark_closed()

        logger.info("realtime_manager_stopped", connections=len(connections))

    @property
    def is_running(self) -> bool:
        """Whether the dispatch thread is accepting events."""
        return self._dispatcher_thread is not None and not self._stopping.is_set()

    # ------------------------------------------------------------ connections #

    def register(self, connection: ClientConnection) -> ClientConnection | None:
        """Take ownership of a newly authenticated connection.

        Returns:
            The connection that was **evicted** to make room, or ``None``. A user
            past :data:`~core.config.Settings.REALTIME_MAX_CONNECTIONS_PER_USER`
            loses their *oldest* connection rather than being refused the newest:
            the newest is the tab the person is looking at, and refusing it would
            make a browser refresh in a stale tab break the tab in front of them.
            That is also the honest answer to "duplicate connections" — a refresh
            leaves a socket the server has not yet noticed is dead, and evicting
            the oldest is what keeps a reloading tab from consuming the budget.

        Raises:
            RuntimeError: the process is already at
                :data:`~core.config.Settings.REALTIME_MAX_CONNECTIONS`. Raised
                rather than evicting somebody else's connection — a global
                ceiling reached is a capacity problem, and solving it by
                disconnecting an unrelated user would hide it.
        """
        evicted: ClientConnection | None = None

        with self._lock:
            if len(self._connections) >= settings.REALTIME_MAX_CONNECTIONS:
                raise RuntimeError("The platform is at its connection capacity.")

            existing = self._by_user.setdefault(connection.user_id, set())
            if len(existing) >= settings.REALTIME_MAX_CONNECTIONS_PER_USER:
                oldest_id = min(
                    existing,
                    key=lambda cid: self._connections[cid].identity.connected_at,
                )
                evicted = self._connections.get(oldest_id)
                self._remove_locked(oldest_id)
                # `existing` may have been discarded by the removal above when it
                # emptied, so re-anchor before adding the newcomer.
                existing = self._by_user.setdefault(connection.user_id, set())

            self._connections[connection.id] = connection
            existing.add(connection.id)
            became_present = len(existing) == 1

        if evicted is not None:
            evicted.mark_closed(RealtimeErrorCode.RATE_LIMITED)
            logger.info("realtime_connection_evicted", **evicted.log_fields)

        logger.info("realtime_client_connected", **connection.log_fields)

        if became_present:
            self._announce_presence(connection, online=True)

        return evicted

    def unregister(self, connection_id: uuid.UUID) -> None:
        """Release a connection and everything indexed against it. Idempotent."""
        with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                return
            self._remove_locked(connection_id)
            went_absent = not self._by_user.get(connection.user_id)

        connection.mark_closed()
        self._metrics.record_disconnected()
        logger.info(
            "realtime_client_disconnected",
            **connection.log_fields,
            reason=connection.close_reason.value if connection.close_reason else "closed",
            subscriptions=connection.subscription_count,
        )

        if went_absent:
            self._announce_presence(connection, online=False)

    def _remove_locked(self, connection_id: uuid.UUID) -> None:
        """Drop one connection from every index. Caller holds the lock."""
        connection = self._connections.pop(connection_id, None)
        if connection is None:
            return

        for topic in connection.topics:
            followers = self._by_topic.get(topic)
            if followers is None:
                continue
            followers.discard(connection_id)
            if not followers:
                del self._by_topic[topic]

        peers = self._by_user.get(connection.user_id)
        if peers is not None:
            peers.discard(connection_id)
            if not peers:
                del self._by_user[connection.user_id]

    # ---------------------------------------------------------- subscriptions #

    def subscribe(
        self, connection: ClientConnection, topics: Iterable[EventTopic], *, user: User
    ) -> tuple[list[str], list[str]]:
        """Authorize and record subscriptions.

        **Synchronous, and it performs database work** — so the endpoint calls it
        in a worker thread rather than on the event loop. That is a deliberate
        cost paid at subscribe time so that delivery, which happens orders of
        magnitude more often, can usually skip it.

        Returns:
            ``(granted, refused)`` as sorted lists of topic keys. Both are
            reported, always: a client told only what succeeded cannot distinguish
            a refusal from a dropped frame, and would keep waiting for events on a
            topic it does not hold.
        """
        granted: list[str] = []
        refused: list[str] = []

        for topic in topics:
            if connection.is_following(topic):
                # Idempotent: re-subscribing refreshes the grant rather than
                # duplicating it, which is what makes a client's reconnect logic
                # safe to run unconditionally.
                connection.grant(topic)
                granted.append(topic.key)
                continue

            if not connection.has_capacity_for(1):
                refused.append(topic.key)
                continue

            decision = self._access.decide(user, topic)
            if not decision.allowed:
                refused.append(topic.key)
                logger.info(
                    "realtime_subscription_refused",
                    **connection.log_fields,
                    scope=topic.scope.value,
                    reason=decision.reason,
                )
                continue

            connection.grant(topic)
            with self._lock:
                self._by_topic.setdefault(topic, set()).add(connection.id)
            granted.append(topic.key)

        if granted or refused:
            logger.info(
                "realtime_subscription_updated",
                **connection.log_fields,
                # Counts, never the topics themselves: a topic names a case, and
                # a list of cases is a list of the caller's matters.
                granted=len(granted),
                refused=len(refused),
                active=connection.subscription_count,
            )

        return sorted(granted), sorted(refused)

    def unsubscribe(
        self, connection: ClientConnection, topics: Iterable[EventTopic]
    ) -> list[str]:
        """Stop following topics. Returns the keys that were actually followed."""
        removed: list[str] = []

        for topic in topics:
            if not connection.revoke(topic):
                continue
            removed.append(topic.key)
            with self._lock:
                followers = self._by_topic.get(topic)
                if followers is not None:
                    followers.discard(connection.id)
                    if not followers:
                        del self._by_topic[topic]

        return sorted(removed)

    # -------------------------------------------------------------- delivery #

    def handle(self, event: DomainEvent) -> None:
        """Accept one event from the dispatcher.

        The :class:`~services.events.EventSubscriber` contract: **must not raise,
        must not block**. Both are satisfied by doing nothing here except handing
        the event to the dispatch thread.

        A full inbox drops the event and counts it. That is the right failure:
        the alternative is to block the publisher — a request that has already
        committed its change — until a queue drains, which trades a lost
        *notification* for a stalled *operation*. The client is not left wrong,
        only late: its next refetch is authoritative.
        """
        if self._stopping.is_set() or self._loop is None:
            return

        try:
            self._inbox.put_nowait(event)
        except queue.Full:
            self._metrics.record_delivery_failed(RealtimeErrorCode.SLOW_CONSUMER)
            logger.warning(
                "realtime_dispatch_overflow", event_type=event.event_type.value
            )

    def _dispatch_forever(self) -> None:
        """Drain the inbox until stopped. Runs on the dispatch thread."""
        while not self._stopping.is_set():
            try:
                event = self._inbox.get(timeout=_DISPATCH_POLL_SECONDS)
            except queue.Empty:
                continue

            try:
                self._route(event)
            except Exception:  # pragma: no cover - defensive
                # A routing fault must not take the dispatch thread down with it,
                # because that would silently stop every client's updates while
                # the process kept serving requests normally.
                logger.exception(
                    "realtime_dispatch_failed", event_type=event.event_type.value
                )

    def _route(self, event: DomainEvent) -> None:
        """Decide who receives one event, then queue it for each of them."""
        recipients = self._candidates(event)
        if not recipients:
            return

        frame = encode_event(event)
        published_at = event.occurred_at.timestamp()

        for connection in recipients:
            self._deliver(connection, event, frame, published_at=published_at)

    def _candidates(self, event: DomainEvent) -> list[ClientConnection]:
        """Every connection following a topic this event travels on.

        Two topics at most: the event's own, and — for the scopes in
        :data:`~core.events.CASE_FANOUT_SCOPES` — its case's. See that constant
        for why a report is deliberately not fanned into its case.
        """
        topics = [event.topic]
        if event.case_id is not None and event.scope in CASE_FANOUT_SCOPES:
            fanout = case_topic(event.case_id)
            if fanout != event.topic:
                topics.append(fanout)

        with self._lock:
            ids: set[uuid.UUID] = set()
            for topic in topics:
                ids |= self._by_topic.get(topic, set())
            return [
                connection
                for connection_id in ids
                if (connection := self._connections.get(connection_id)) is not None
                and not connection.closed
            ]

    def _deliver(
        self,
        connection: ClientConnection,
        event: DomainEvent,
        frame: str,
        *,
        published_at: float,
    ) -> None:
        """Authorize, de-duplicate, and queue one event for one connection."""
        topic = self._delivery_topic(connection, event)
        if topic is None:
            self._metrics.record_delivery_denied()
            return

        if not self._authorized(connection, topic):
            self._metrics.record_delivery_denied()
            return

        if not connection.should_deliver(event.event_id):
            self._metrics.record_delivery_deduplicated()
            return

        loop = self._loop
        if loop is None or loop.is_closed():  # pragma: no cover - shutdown race
            self._metrics.record_delivery_failed(RealtimeErrorCode.INTERNAL)
            return

        # `enqueue` mutates an asyncio.Queue, which is not thread-safe: it is
        # marshalled onto the loop rather than called here. This is the only
        # point in the module where the dispatch thread touches loop state.
        loop.call_soon_threadsafe(connection.enqueue, frame, event.sequence)

        # Publication → queued-for-this-socket. Deliberately not "→ acknowledged
        # by the client": that would measure the client's network, which the
        # platform neither controls nor can distinguish from a user's laptop
        # going to sleep. This measures what the *platform* is responsible for.
        self._metrics.record_delivered(
            latency_ms=max(0.0, time.time() - published_at) * 1000.0
        )

    @staticmethod
    def _delivery_topic(connection: ClientConnection, event: DomainEvent) -> EventTopic | None:
        """Which of the connection's own grants this event is being delivered under.

        A connection may follow a document *and* its case; the event is
        authorized under whichever grant it actually arrived on, because that is
        the grant a refusal must revoke. Returning ``None`` means the connection
        is in the index but holds no matching grant — which happens for one
        moment during an unsubscribe, and is a refusal rather than a delivery.
        """
        if connection.is_following(event.topic):
            return event.topic

        if event.case_id is not None and event.scope in CASE_FANOUT_SCOPES:
            fanout = case_topic(event.case_id)
            if connection.is_following(fanout):
                return fanout

        return None

    def _authorized(self, connection: ClientConnection, topic: EventTopic) -> bool:
        """Whether ``connection`` may still receive events on ``topic``.

        A fresh grant is honoured without a query. A stale one is re-resolved
        against the database — the account included — and a refusal **revokes the
        subscription** rather than merely skipping this event: leaving it in place
        would mean re-running the same failing query for every subsequent event,
        and would leave the client believing it is still following something it
        is not.
        """
        ttl = float(settings.REALTIME_AUTHORIZATION_TTL_SECONDS)
        if connection.grant_is_fresh(topic, ttl_seconds=ttl):
            return True

        decision = self._access.recheck(connection.user_id, topic)
        if decision.allowed:
            connection.grant(topic)
            return True

        self.unsubscribe(connection, [topic])
        logger.info(
            "realtime_subscription_revoked",
            **connection.log_fields,
            scope=topic.scope.value,
            reason=decision.reason,
        )
        return False

    # -------------------------------------------------------------- presence #

    def _announce_presence(self, connection: ClientConnection, *, online: bool) -> None:
        """Publish a presence change on the account's own topic.

        Published **through the dispatcher** rather than delivered directly, even
        though the manager is the only consumer that exists today. Going around
        the dispatcher here would be this module doing exactly what the spec
        forbids every other module from doing — and a future notification or
        analytics consumer would silently never see presence.

        Scoped to the user's own topic, so a presence change reaches that
        person's other tabs and nobody else. Broadcasting who is online to
        everyone on a case is a *product* decision this spec puts out of scope
        ("presence visualization itself is out of scope"), and it would be a
        disclosure that cannot be taken back.
        """
        self._publisher.publish(
            event_type=DomainEventType.PRESENCE_CHANGED,
            topic=user_topic(connection.user_id),
            actor_id=connection.user_id,
            payload={
                "online": online,
                "connections": self.connection_count_for(connection.user_id),
            },
        )

    def connection_count_for(self, user_id: uuid.UUID) -> int:
        """How many live connections one account holds."""
        with self._lock:
            return len(self._by_user.get(user_id, set()))

    def presence(self) -> list[PresenceEntry]:
        """Who currently holds at least one connection, most recent first."""
        with self._lock:
            entries: list[PresenceEntry] = []
            for user_id, connection_ids in self._by_user.items():
                connections = [
                    self._connections[cid] for cid in connection_ids if cid in self._connections
                ]
                if not connections:
                    continue
                oldest = min(connections, key=lambda c: c.identity.connected_at)
                entries.append(
                    PresenceEntry(
                        user_id=user_id,
                        role=oldest.identity.role.value,
                        connections=len(connections),
                        since=oldest.identity.connected_at,
                    )
                )

        return sorted(entries, key=lambda entry: entry.since, reverse=True)

    # ------------------------------------------------------------- monitoring #

    @property
    def connection_count(self) -> int:
        """Live connections held by this process."""
        with self._lock:
            return len(self._connections)

    @property
    def present_user_count(self) -> int:
        """Distinct accounts holding at least one connection."""
        with self._lock:
            return len(self._by_user)

    @property
    def topic_count(self) -> int:
        """Distinct topics being followed. A shape figure, never the topics."""
        with self._lock:
            return len(self._by_topic)

    @property
    def pending_dispatches(self) -> int:
        """Events accepted and not yet routed. The backlog gauge."""
        return self._inbox.qsize()

    def metrics_snapshot(self) -> RealtimeMetricsSnapshot:
        """The counters, with the two live gauges filled in from this manager.

        The gauges are read here rather than counted by the recorder, for the
        reason stated on :meth:`~services.event_metrics.RealtimeMetricsRecorder.snapshot`:
        a counter incremented on connect and decremented on disconnect drifts the
        first time a socket dies without either, while a gauge read from the
        thing that owns the connections cannot.
        """
        return self._metrics.snapshot(
            active_connections=self.connection_count,
            present_users=self.present_user_count,
        )

    def reset(self) -> None:
        """Forget every connection and index. For tests."""
        with self._lock:
            self._connections.clear()
            self._by_topic.clear()
            self._by_user.clear()


#: The one manager the process shares.
#:
#: Module-level for the reason the job queues are: it *is* the set of live
#: connections, and a per-request instance would hold none of them.
#:
#: It is handed the dispatcher **as a publisher**, not as a dispatcher: presence
#: changes are announced through the same channel as everything else, and the
#: narrow protocol is what keeps the one consumer that exists from being able to
#: enumerate or reach the others. The reverse direction — the dispatcher learning
#: about this manager — is wired in :func:`core.lifespan._start_realtime` and
#: nowhere else.
_shared = ConnectionManager(
    metrics=get_realtime_metrics(), publisher=get_event_dispatcher()
)


def get_connection_manager() -> ConnectionManager:
    """Return the process-wide WebSocket connection manager."""
    return _shared


__all__ = [
    "ConnectionManager",
    "PresenceEntry",
    "get_connection_manager",
]
