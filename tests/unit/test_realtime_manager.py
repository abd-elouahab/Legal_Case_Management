"""Unit tests for routing and delivery (`websocket/manager.py`).

These are the spec's central promises — *"events are delivered"*, *"unauthorized
users receive nothing"*, *"duplicate events are avoided"* — asserted where they
are actually decided.

**The event loop is a double, and the dispatch thread is real.** Substituting the
loop is what lets a test read the frames a connection was handed without a
socket; keeping the thread is what makes the test exercise the threading path
that production uses, which is the part most likely to be wrong.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from core.events import (
    DomainEvent,
    DomainEventType,
    EventTopic,
    case_topic,
    document_topic,
    report_topic,
    user_topic,
)
from models.user import UserRole
from services.event_metrics import InMemoryRealtimeMetrics
from services.events import RecordingEventPublisher
from services.realtime_access import RealtimeAccessPolicy, TopicDecision
from websocket.connection import ClientConnection, ConnectionIdentity
from websocket.manager import ConnectionManager


class _ImmediateLoop:
    """An event loop that runs callbacks on the calling thread.

    ``call_soon_threadsafe`` is the only member the manager uses, and running it
    inline rather than scheduling it is what makes a delivery observable in the
    same test that caused it.
    """

    def __init__(self) -> None:
        self.scheduled = 0

    def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        self.scheduled += 1
        callback(*args)

    def is_closed(self) -> bool:
        return False


class _Policy:
    """A scripted access policy.

    Substituted for :class:`~services.realtime_access.RealtimeAccessPolicy` so
    these tests are about *routing* — which connection an event reaches — rather
    than about the case and document rules, which have their own module and their
    own tests.
    """

    def __init__(self, allowed: set[EventTopic] | None = None) -> None:
        self.allowed = allowed if allowed is not None else set()
        self.decide_calls = 0
        self.recheck_calls = 0

    def decide(self, user: Any, topic: EventTopic) -> TopicDecision:
        self.decide_calls += 1
        return (
            TopicDecision.allow() if topic in self.allowed else TopicDecision.deny("not_allowed")
        )

    def recheck(self, user_id: uuid.UUID, topic: EventTopic) -> TopicDecision:
        self.recheck_calls += 1
        return (
            TopicDecision.allow() if topic in self.allowed else TopicDecision.deny("not_allowed")
        )


class _Reader:
    """Drains a connection's queue without an event loop."""

    @staticmethod
    def frames(connection: ClientConnection) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        queue = connection._queue
        while not queue.empty():
            collected.append(json.loads(queue.get_nowait()))
        return collected


def _connection(user_id: uuid.UUID | None = None) -> ClientConnection:
    return ClientConnection(
        ConnectionIdentity(
            user_id=user_id or uuid.uuid4(),
            role=UserRole.LAWYER,
            session_generation=0,
            connected_at=datetime.now(UTC),
        ),
        queue_size=32,
        max_subscriptions=16,
        dedupe_window=32,
    )


def _event(
    event_type: DomainEventType,
    topic: EventTopic,
    *,
    sequence: int = 1,
    case_id: uuid.UUID | None = None,
) -> DomainEvent:
    return DomainEvent.create(
        event_type=event_type, topic=topic, sequence=sequence, case_id=case_id
    )


def _wait_for_delivery(manager: ConnectionManager, loop: _ImmediateLoop, *, expected: int) -> None:
    """Wait for the dispatch thread to finish routing.

    Bounded and polled rather than a fixed sleep: a sleep long enough to be safe
    on a loaded machine is a second added to every run, and one short enough not
    to be is a flake.
    """
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if manager.pending_dispatches == 0 and loop.scheduled >= expected:
            return
        time.sleep(0.01)


@pytest.fixture
def loop() -> _ImmediateLoop:
    return _ImmediateLoop()


@pytest.fixture
def policy() -> _Policy:
    return _Policy()


@pytest.fixture
def manager(loop: _ImmediateLoop, policy: _Policy):  # type: ignore[no-untyped-def]
    built = ConnectionManager(
        access=policy,  # type: ignore[arg-type]
        metrics=InMemoryRealtimeMetrics(),
        publisher=RecordingEventPublisher(),
    )
    built.start(loop)  # type: ignore[arg-type]
    yield built
    built.stop()


class TestDelivery:
    def test_a_subscriber_receives_an_event_on_its_topic(
        self, manager: ConnectionManager, policy: _Policy, loop: _ImmediateLoop
    ) -> None:
        connection = _connection()
        topic = case_topic(uuid.uuid4())
        policy.allowed = {topic}

        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        manager.handle(_event(DomainEventType.CASE_UPDATED, topic))
        _wait_for_delivery(manager, loop, expected=1)

        frames = _Reader.frames(connection)
        assert [frame["event"] for frame in frames] == ["case.updated"]

    def test_a_connection_that_did_not_subscribe_receives_nothing(
        self, manager: ConnectionManager, loop: _ImmediateLoop
    ) -> None:
        connection = _connection()
        manager.register(connection)

        manager.handle(_event(DomainEventType.CASE_UPDATED, case_topic(uuid.uuid4())))
        _wait_for_delivery(manager, loop, expected=0)

        assert _Reader.frames(connection) == []

    def test_a_case_follower_receives_a_document_event(
        self, manager: ConnectionManager, policy: _Policy, loop: _ImmediateLoop
    ) -> None:
        """One subscription keeps a whole case workspace live.

        Safe because document access follows case access exactly — a caller
        authorized for a case is, by construction, authorized for every document
        in it.
        """
        case_id = uuid.uuid4()
        topic = case_topic(case_id)
        policy.allowed = {topic}

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        manager.handle(
            _event(
                DomainEventType.OCR_COMPLETED,
                document_topic(uuid.uuid4()),
                case_id=case_id,
            )
        )
        _wait_for_delivery(manager, loop, expected=1)

        assert [frame["event"] for frame in _Reader.frames(connection)] == ["ocr.completed"]

    def test_a_case_follower_never_receives_a_report_event(
        self, manager: ConnectionManager, policy: _Policy, loop: _ImmediateLoop
    ) -> None:
        """A report is its author's private work product.

        It is *about* the case and carries its identifier, which is exactly why
        the fan-in rule is a set of scopes rather than a rule about ``case_id``.
        """
        case_id = uuid.uuid4()
        topic = case_topic(case_id)
        policy.allowed = {topic}

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        manager.handle(
            _event(DomainEventType.REPORT_GENERATED, report_topic(uuid.uuid4()), case_id=case_id)
        )
        _wait_for_delivery(manager, loop, expected=0)

        assert _Reader.frames(connection) == []

    def test_the_same_event_is_delivered_once(
        self, manager: ConnectionManager, policy: _Policy, loop: _ImmediateLoop
    ) -> None:
        topic = case_topic(uuid.uuid4())
        policy.allowed = {topic}

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        event = _event(DomainEventType.CASE_UPDATED, topic)
        manager.handle(event)
        manager.handle(event)
        _wait_for_delivery(manager, loop, expected=1)

        assert len(_Reader.frames(connection)) == 1

    def test_a_connection_following_both_a_document_and_its_case_receives_one_copy(
        self, manager: ConnectionManager, policy: _Policy, loop: _ImmediateLoop
    ) -> None:
        """The fan-in must not become a fan-out."""
        case_id, document_id = uuid.uuid4(), uuid.uuid4()
        topics = {case_topic(case_id), document_topic(document_id)}
        policy.allowed = topics

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, list(topics), user=object())  # type: ignore[arg-type]

        manager.handle(
            _event(DomainEventType.OCR_STARTED, document_topic(document_id), case_id=case_id)
        )
        _wait_for_delivery(manager, loop, expected=1)

        assert len(_Reader.frames(connection)) == 1


class TestAuthorizationOnDelivery:
    def test_a_revoked_grant_stops_delivery_and_the_subscription(
        self, manager: ConnectionManager, policy: _Policy, loop: _ImmediateLoop
    ) -> None:
        """A refusal revokes rather than merely skipping.

        Leaving it in place would re-run the same failing query for every
        subsequent event, and leave the client believing it still follows
        something it does not.
        """
        topic = case_topic(uuid.uuid4())
        policy.allowed = {topic}

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        # The lawyer is un-assigned. The grant is expired by hand so the
        # re-check happens on the next event rather than after the real TTL.
        policy.allowed = set()
        connection._grants[topic].authorized_at -= 10_000

        manager.handle(_event(DomainEventType.CASE_UPDATED, topic))
        _wait_for_delivery(manager, loop, expected=0)

        assert _Reader.frames(connection) == []
        assert connection.is_following(topic) is False
        assert policy.recheck_calls == 1

    def test_a_fresh_grant_is_honoured_without_a_query(
        self, manager: ConnectionManager, policy: _Policy, loop: _ImmediateLoop
    ) -> None:
        """The whole reason delivery is not one database round trip per event."""
        topic = case_topic(uuid.uuid4())
        policy.allowed = {topic}

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        for sequence in range(1, 6):
            manager.handle(_event(DomainEventType.CASE_UPDATED, topic, sequence=sequence))
        _wait_for_delivery(manager, loop, expected=5)

        assert len(_Reader.frames(connection)) == 5
        assert policy.recheck_calls == 0


class TestSubscriptions:
    def test_a_refused_topic_is_reported_and_not_indexed(
        self, manager: ConnectionManager, policy: _Policy
    ) -> None:
        connection = _connection()
        topic = case_topic(uuid.uuid4())
        manager.register(connection)

        granted, refused = manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        assert granted == []
        assert refused == [topic.key]
        assert manager.topic_count == 0

    def test_re_subscribing_refreshes_rather_than_duplicates(
        self, manager: ConnectionManager, policy: _Policy
    ) -> None:
        """Which is what makes a client's reconnect logic safe to run blindly."""
        topic = case_topic(uuid.uuid4())
        policy.allowed = {topic}

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        assert connection.subscription_count == 1
        # The second call short-circuits, so it costs no authorization lookup.
        assert policy.decide_calls == 1

    def test_unsubscribing_clears_the_routing_index(
        self, manager: ConnectionManager, policy: _Policy
    ) -> None:
        topic = case_topic(uuid.uuid4())
        policy.allowed = {topic}

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]
        assert manager.topic_count == 1

        manager.unsubscribe(connection, [topic])
        assert manager.topic_count == 0


class TestConnections:
    def test_a_disconnect_clears_every_index(
        self, manager: ConnectionManager, policy: _Policy
    ) -> None:
        topic = case_topic(uuid.uuid4())
        policy.allowed = {topic}

        connection = _connection()
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        manager.unregister(connection.id)

        assert manager.connection_count == 0
        assert manager.topic_count == 0
        assert manager.present_user_count == 0

    def test_unregistering_twice_is_silent(self, manager: ConnectionManager) -> None:
        connection = _connection()
        manager.register(connection)
        manager.unregister(connection.id)
        manager.unregister(connection.id)

    def test_the_oldest_connection_is_evicted_rather_than_the_newest_refused(
        self, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The newest is the tab the person is looking at.

        Refusing it would make a browser refresh break the tab in front of them —
        which is also the honest answer to "duplicate connections", since a
        refresh leaves a socket the server has not yet noticed is dead.
        """
        from core.config import settings

        monkeypatch.setattr(settings, "REALTIME_MAX_CONNECTIONS_PER_USER", 1)

        user_id = uuid.uuid4()
        first = _connection(user_id)
        second = _connection(user_id)

        manager.register(first)
        evicted = manager.register(second)

        assert evicted is first
        assert first.closed is True
        assert manager.connection_count == 1

    def test_the_process_ceiling_raises_rather_than_evicting_a_stranger(
        self, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A global ceiling reached is a capacity problem.

        Solving it by disconnecting an unrelated user would hide it.
        """
        from core.config import settings

        monkeypatch.setattr(settings, "REALTIME_MAX_CONNECTIONS", 1)
        manager.register(_connection())

        with pytest.raises(RuntimeError):
            manager.register(_connection())


class TestPresence:
    def test_presence_counts_connections_and_names_the_account(
        self, manager: ConnectionManager
    ) -> None:
        user_id = uuid.uuid4()
        manager.register(_connection(user_id))
        manager.register(_connection(user_id))

        entries = manager.presence()

        assert len(entries) == 1
        assert entries[0].user_id == user_id
        assert entries[0].connections == 2

    def test_a_presence_change_is_announced_through_the_dispatcher(
        self, manager: ConnectionManager
    ) -> None:
        """Not delivered directly, even though the manager is the only consumer.

        Going around the dispatcher here would be this module doing exactly what
        the spec forbids every other module from doing — and a future consumer
        would silently never see presence.
        """
        publisher = manager._publisher
        assert isinstance(publisher, RecordingEventPublisher)

        connection = _connection()
        manager.register(connection)

        assert DomainEventType.PRESENCE_CHANGED in publisher.types()
        assert publisher.events[0].topic == user_topic(connection.user_id)

    def test_presence_is_announced_once_per_account_not_per_tab(
        self, manager: ConnectionManager
    ) -> None:
        publisher = manager._publisher
        assert isinstance(publisher, RecordingEventPublisher)

        user_id = uuid.uuid4()
        manager.register(_connection(user_id))
        manager.register(_connection(user_id))

        assert publisher.types().count(DomainEventType.PRESENCE_CHANGED) == 1


class TestBackpressure:
    def test_handling_never_raises_at_the_publisher(
        self, manager: ConnectionManager
    ) -> None:
        """A publisher is inside a request that has already committed."""
        manager.stop()
        # No `pytest.raises`: not raising is the assertion.
        manager.handle(_event(DomainEventType.CASE_UPDATED, case_topic(uuid.uuid4())))

    def test_a_slow_consumer_is_closed_rather_than_buffered(
        self, manager: ConnectionManager, policy: _Policy, loop: _ImmediateLoop
    ) -> None:
        topic = case_topic(uuid.uuid4())
        policy.allowed = {topic}

        connection = ClientConnection(
            ConnectionIdentity(
                user_id=uuid.uuid4(),
                role=UserRole.LAWYER,
                session_generation=0,
                connected_at=datetime.now(UTC),
            ),
            queue_size=2,
            max_subscriptions=4,
            dedupe_window=32,
        )
        manager.register(connection)
        manager.subscribe(connection, [topic], user=object())  # type: ignore[arg-type]

        for sequence in range(1, 6):
            manager.handle(_event(DomainEventType.CASE_UPDATED, topic, sequence=sequence))
        _wait_for_delivery(manager, loop, expected=3)

        assert connection.closed is True


class TestAccessPolicyContract:
    def test_the_manager_defaults_to_the_real_policy(self) -> None:
        """A manager built without one must not default to allowing everything."""
        assert isinstance(
            ConnectionManager()._access,
            RealtimeAccessPolicy,
        )
