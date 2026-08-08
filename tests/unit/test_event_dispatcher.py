"""Unit tests for the event dispatcher (`services/events.py`).

The dispatcher's contract is unusually strict, and every clause of it is here:
it never raises at a publisher, it isolates one consumer's failure from another's,
it assigns a total order, and it screens payloads before anybody sees them.
"""

from __future__ import annotations

import threading
import uuid

from core.events import DomainEvent, DomainEventType, case_topic, document_topic, user_topic
from services.event_metrics import InMemoryRealtimeMetrics
from services.events import (
    EventDispatcher,
    NullEventPublisher,
    RecordingEventPublisher,
)


class _Recorder:
    """A subscriber that keeps what it was handed."""

    def __init__(self, name: str = "recorder") -> None:
        self.name = name
        self.events: list[DomainEvent] = []

    def handle(self, event: DomainEvent) -> None:
        self.events.append(event)


class _Exploding:
    """A subscriber that always raises."""

    name = "exploding"

    def __init__(self) -> None:
        self.calls = 0

    def handle(self, event: DomainEvent) -> None:
        self.calls += 1
        raise RuntimeError("consumer is broken")


class TestPublishing:
    def test_an_event_reaches_every_subscriber(self) -> None:
        dispatcher = EventDispatcher()
        first, second = _Recorder("first"), _Recorder("second")
        dispatcher.subscribe(first)
        dispatcher.subscribe(second)

        dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
        )

        assert len(first.events) == 1
        assert len(second.events) == 1

    def test_publishing_returns_the_event_and_not_a_delivery_count(self) -> None:
        """A publisher that could see who is listening could start depending on it."""
        dispatcher = EventDispatcher()
        published = dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
        )
        assert isinstance(published, DomainEvent)

    def test_publishing_with_no_subscribers_is_not_an_error(self) -> None:
        dispatcher = EventDispatcher()
        assert (
            dispatcher.publish(
                event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
            )
            is not None
        )

    def test_a_broken_subscriber_does_not_deny_the_event_to_the_others(self) -> None:
        dispatcher = EventDispatcher()
        broken, working = _Exploding(), _Recorder()
        dispatcher.subscribe(broken)
        dispatcher.subscribe(working)

        dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
        )

        assert broken.calls == 1
        assert len(working.events) == 1

    def test_a_broken_subscriber_never_reaches_the_publisher(self) -> None:
        """The business change is already committed by the time this is called."""
        dispatcher = EventDispatcher()
        dispatcher.subscribe(_Exploding())

        # No `pytest.raises`: not raising is the assertion.
        dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
        )

    def test_a_bad_payload_is_dropped_rather_than_raised(self) -> None:
        dispatcher = EventDispatcher()
        recorder = _Recorder()
        dispatcher.subscribe(recorder)

        published = dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED,
            topic=case_topic(uuid.uuid4()),
            payload={"nested": {"not": "allowed"}},
        )

        assert published is None
        assert recorder.events == []

    def test_an_event_published_on_the_wrong_scope_is_refused(self) -> None:
        """A case event on a document topic would be delivered to the wrong people."""
        dispatcher = EventDispatcher()
        recorder = _Recorder()
        dispatcher.subscribe(recorder)

        published = dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=document_topic(uuid.uuid4())
        )

        assert published is None
        assert recorder.events == []

    def test_a_forbidden_payload_key_is_screened_before_any_subscriber_sees_it(self) -> None:
        dispatcher = EventDispatcher()
        recorder = _Recorder()
        dispatcher.subscribe(recorder)

        dispatcher.publish(
            event_type=DomainEventType.OCR_COMPLETED,
            topic=document_topic(uuid.uuid4()),
            payload={"page_count": 3, "full_text": "confidential"},
        )

        assert recorder.events[0].payload == {"page_count": 3}


class TestSequencing:
    def test_sequences_increase_by_one(self) -> None:
        dispatcher = EventDispatcher()
        topic = case_topic(uuid.uuid4())

        sequences = [
            event.sequence
            for _ in range(5)
            if (event := dispatcher.publish(event_type=DomainEventType.CASE_UPDATED, topic=topic))
        ]

        assert sequences == [1, 2, 3, 4, 5]

    def test_the_current_sequence_is_readable(self) -> None:
        """It is a client-visible contract: `ready` and `pong` both carry it."""
        dispatcher = EventDispatcher()
        assert dispatcher.current_sequence == 0
        dispatcher.publish(
            event_type=DomainEventType.CASE_UPDATED, topic=case_topic(uuid.uuid4())
        )
        assert dispatcher.current_sequence == 1

    def test_concurrent_publishers_produce_distinct_ordered_sequences(self) -> None:
        """Two threads publishing at once must not collide on a number.

        A gap on a client means "you missed something"; a *duplicate* sequence
        would mean the number says nothing at all.
        """
        dispatcher = EventDispatcher()
        recorder = _Recorder()
        dispatcher.subscribe(recorder)
        topic = case_topic(uuid.uuid4())
        barrier = threading.Barrier(4)

        def publish_many() -> None:
            barrier.wait()
            for _ in range(25):
                dispatcher.publish(event_type=DomainEventType.CASE_UPDATED, topic=topic)

        threads = [threading.Thread(target=publish_many) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        sequences = sorted(event.sequence for event in recorder.events)
        assert sequences == list(range(1, 101))


class TestSubscribers:
    def test_registering_the_same_name_twice_replaces_rather_than_duplicates(self) -> None:
        """A restarted component must not receive every event twice."""
        dispatcher = EventDispatcher()
        first, second = _Recorder("same"), _Recorder("same")
        dispatcher.subscribe(first)
        dispatcher.subscribe(second)

        dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
        )

        assert first.events == []
        assert len(second.events) == 1

    def test_unsubscribing_stops_delivery(self) -> None:
        dispatcher = EventDispatcher()
        recorder = _Recorder()
        dispatcher.subscribe(recorder)
        dispatcher.unsubscribe(recorder.name)

        dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
        )

        assert recorder.events == []

    def test_unsubscribing_something_unregistered_is_silent(self) -> None:
        EventDispatcher().unsubscribe("never-registered")

    def test_subscriber_names_are_reported_for_the_monitoring_view(self) -> None:
        dispatcher = EventDispatcher()
        dispatcher.subscribe(_Recorder("websocket"))
        dispatcher.subscribe(_Recorder("analytics"))
        assert dispatcher.subscriber_names == ["analytics", "websocket"]


class TestMetrics:
    def test_publications_and_failures_are_counted(self) -> None:
        metrics = InMemoryRealtimeMetrics()
        dispatcher = EventDispatcher(metrics=metrics)
        dispatcher.subscribe(_Exploding())

        dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
        )
        dispatcher.publish(
            event_type=DomainEventType.CASE_CREATED,
            topic=case_topic(uuid.uuid4()),
            payload={"bad": {"nested": 1}},
        )

        snapshot = metrics.snapshot(active_connections=0, present_users=0)
        assert snapshot.events_published == 1
        assert snapshot.events_rejected == 1
        assert snapshot.subscriber_failures == {"exploding": 1}


class TestNullAndRecordingPublishers:
    def test_the_null_publisher_announces_nothing(self) -> None:
        assert (
            NullEventPublisher().publish(
                event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
            )
            is None
        )

    def test_the_recording_publisher_keeps_order(self) -> None:
        publisher = RecordingEventPublisher()
        publisher.publish(
            event_type=DomainEventType.CASE_CREATED, topic=case_topic(uuid.uuid4())
        )
        publisher.publish(
            event_type=DomainEventType.CASE_ARCHIVED, topic=case_topic(uuid.uuid4())
        )

        assert publisher.types() == [
            DomainEventType.CASE_CREATED,
            DomainEventType.CASE_ARCHIVED,
        ]

    def test_the_recording_publisher_screens_payloads_like_the_real_one(self) -> None:
        """Otherwise a test would pass against a double that is more permissive."""
        publisher = RecordingEventPublisher()
        publisher.publish(
            event_type=DomainEventType.PRESENCE_CHANGED,
            topic=user_topic(uuid.uuid4()),
            payload={"online": True, "token": "secret"},
        )
        assert publisher.events[0].payload == {"online": True}
