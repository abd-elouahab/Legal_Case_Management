"""Unit tests for one connection (`websocket/connection.py`).

Every rule here is one of the spec's reliability requirements expressed as state
a test can inspect without a socket: bounded queues, expiring authorization
grants, duplicate suppression, liveness, and rate limiting.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from core.events import case_topic, document_topic
from core.realtime import RealtimeErrorCode
from models.user import UserRole
from websocket.connection import ClientConnection, ConnectionIdentity


def _identity(user_id: uuid.UUID | None = None) -> ConnectionIdentity:
    return ConnectionIdentity(
        user_id=user_id or uuid.uuid4(),
        role=UserRole.LAWYER,
        session_generation=0,
        connected_at=datetime.now(UTC),
    )


def _connection(
    *, queue_size: int = 8, max_subscriptions: int = 4, dedupe_window: int = 4
) -> ClientConnection:
    return ClientConnection(
        _identity(),
        queue_size=queue_size,
        max_subscriptions=max_subscriptions,
        dedupe_window=dedupe_window,
    )


class TestSubscriptions:
    def test_granting_a_topic_makes_it_followed(self) -> None:
        connection = _connection()
        topic = case_topic(uuid.uuid4())
        connection.grant(topic)
        assert connection.is_following(topic)
        assert connection.topic_keys == [topic.key]

    def test_revoking_reports_whether_it_was_followed(self) -> None:
        connection = _connection()
        topic = case_topic(uuid.uuid4())
        assert connection.revoke(topic) is False
        connection.grant(topic)
        assert connection.revoke(topic) is True

    def test_the_ceiling_is_enforced(self) -> None:
        connection = _connection(max_subscriptions=2)
        connection.grant(case_topic(uuid.uuid4()))
        connection.grant(case_topic(uuid.uuid4()))
        assert connection.has_capacity_for(1) is False

    def test_topic_keys_are_sorted(self) -> None:
        """They are echoed on every change; an unordered set would make two
        identical states look different."""
        connection = _connection()
        for _ in range(3):
            connection.grant(case_topic(uuid.uuid4()))
        assert connection.topic_keys == sorted(connection.topic_keys)


class TestGrantFreshness:
    def test_a_new_grant_is_fresh(self) -> None:
        connection = _connection()
        topic = case_topic(uuid.uuid4())
        connection.grant(topic)
        assert connection.grant_is_fresh(topic, ttl_seconds=30) is True

    def test_an_ungranted_topic_is_never_fresh(self) -> None:
        assert _connection().grant_is_fresh(case_topic(uuid.uuid4()), ttl_seconds=30) is False

    def test_a_zero_ttl_means_re_authorize_every_delivery(self) -> None:
        """The safe reading of a zero, and the one an operator setting it intends.

        Read as "never expires" it would be the exact opposite of what somebody
        who set the strictest possible value was asking for.
        """
        connection = _connection()
        topic = case_topic(uuid.uuid4())
        connection.grant(topic)
        assert connection.grant_is_fresh(topic, ttl_seconds=0) is False

    def test_a_grant_expires(self) -> None:
        connection = _connection()
        topic = case_topic(uuid.uuid4())
        connection.grant(topic)
        # A negative TTL is "already expired" without making the test sleep.
        assert connection.grant_is_fresh(topic, ttl_seconds=-1) is False


class TestDeduplication:
    def test_the_same_event_is_delivered_once(self) -> None:
        connection = _connection()
        event_id = uuid.uuid4()
        assert connection.should_deliver(event_id) is True
        assert connection.should_deliver(event_id) is False

    def test_the_window_is_bounded(self) -> None:
        """A per-connection set that grew with traffic would be a leak that
        scales with how busy the platform is."""
        connection = _connection(dedupe_window=2)
        first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        for event_id in (first, second, third):
            assert connection.should_deliver(event_id) is True

        # `first` has been evicted, so it would be delivered again — which is why
        # the client keeps its own check and every event carries a stable id.
        assert connection.should_deliver(first) is True
        assert connection.should_deliver(third) is False

    def test_a_zero_window_disables_suppression(self) -> None:
        connection = _connection(dedupe_window=0)
        event_id = uuid.uuid4()
        assert connection.should_deliver(event_id) is True
        assert connection.should_deliver(event_id) is True


class TestOutbound:
    async def test_a_queued_frame_can_be_read_back(self) -> None:
        connection = _connection()
        assert connection.enqueue("frame", 5) is True
        assert await connection.next_frame(timeout=0.1) == "frame"
        assert connection.last_sequence == 5

    async def test_the_reader_times_out_so_the_heartbeat_can_fire(self) -> None:
        """The timeout is what turns a blocking read into the heartbeat clock."""
        assert await _connection().next_frame(timeout=0.01) is None

    def test_overflow_closes_the_connection_rather_than_dropping_events(self) -> None:
        """Dropping silently would desynchronize a client that believes it is live.

        Closing says "you are behind, reconnect and refetch", which is the only
        outcome that leaves the client *correct*.
        """
        connection = _connection(queue_size=2)
        assert connection.enqueue("a") is True
        assert connection.enqueue("b") is True
        assert connection.enqueue("c") is False
        assert connection.closed is True
        assert connection.close_reason is RealtimeErrorCode.SLOW_CONSUMER

    def test_a_closed_connection_accepts_nothing(self) -> None:
        connection = _connection()
        connection.mark_closed()
        assert connection.enqueue("frame") is False

    def test_the_first_close_reason_is_kept(self) -> None:
        """A later, vaguer reason must not overwrite the one that explains it."""
        connection = _connection()
        connection.mark_closed(RealtimeErrorCode.RATE_LIMITED)
        connection.mark_closed(RealtimeErrorCode.INTERNAL)
        assert connection.close_reason is RealtimeErrorCode.RATE_LIMITED


class TestLiveness:
    def test_a_fresh_connection_is_not_idle(self) -> None:
        assert _connection().is_idle(timeout_seconds=60) is False

    def test_idleness_is_measured_against_the_last_thing_heard(self) -> None:
        connection = _connection()
        connection.touch()
        assert connection.is_idle(timeout_seconds=-1) is True

    def test_the_frame_budget_is_enforced(self) -> None:
        connection = _connection()
        for _ in range(3):
            assert connection.within_rate_limit(max_frames_per_minute=3) is True
        assert connection.within_rate_limit(max_frames_per_minute=3) is False


class TestIdentity:
    def test_log_fields_carry_identifiers_and_nothing_else(self) -> None:
        """A topic names a case, and a case is a client's matter."""
        connection = _connection()
        connection.grant(document_topic(uuid.uuid4()))
        assert set(connection.log_fields) == {"connection_id", "user_id", "role"}

    def test_the_identity_holds_no_orm_instance(self) -> None:
        """A connection lives for hours; a detached row is a stale read waiting."""
        identity = _identity()
        assert not hasattr(identity, "user")
        assert isinstance(identity.user_id, uuid.UUID)
