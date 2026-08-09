"""Notification observability.

``16-notifications.md``'s Monitoring section names five figures: **notifications
created, notifications delivered, unread notifications, failed deliveries, and
average delivery latency**. Four of them are recorded here, in the shape
:mod:`services.search_metrics`, :mod:`services.rag_metrics`,
:mod:`services.assistant_metrics`, and :mod:`services.event_metrics` already
established — a protocol, an in-memory implementation, a null implementation, and
a frozen snapshot.

**The fifth is deliberately not here.** *Unread notifications* is a property of
persisted rows, so it is a SQL aggregate
(:meth:`~repositories.notification.NotificationRepository.statistics`) rather
than a counter: a process-local unread count would reset on restart *and* be
wrong, because reading a notification in one API instance would not decrement a
counter held in another. That is the same split
:mod:`services.assistant_metrics` makes — conversation counts are queried,
request latency is counted — and the reason ``/notifications/metrics`` carries a
``since`` caveat on some of its figures and not on others.

**Nothing identifying can be recorded here, by construction.** The recorder is
handed durations, counts, and short stable strings — a rule key, a failure
reason. There is no parameter for a user, a case, a notification, or a rendered
message, so there is nowhere for one to go. That matters more here than anywhere
else this pattern is used: a per-recipient counter would be a live index of who
is being told about what.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class NotificationFailureReason(StrEnum):
    """Why a notification could not be created or announced.

    A closed vocabulary rather than free text, for the reason every other
    ``*_metrics`` module here uses one: these are grouped in a monitoring view,
    and a message interpolated from an exception would produce a breakdown with
    one bucket per occurrence.
    """

    #: The recipients could not be resolved — the case, document, or report the
    #: event names could not be read.
    RECIPIENT_LOOKUP_FAILED = "recipient_lookup_failed"
    #: The batch insert failed.
    PERSISTENCE_FAILED = "persistence_failed"
    #: The notification was stored but could not be announced onto the channel.
    DELIVERY_FAILED = "delivery_failed"
    #: Anything the subscriber did not anticipate. Always worth investigating.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NotificationMetricsSnapshot:
    """The counters at one instant, as the monitoring endpoint reports them.

    Frozen rather than a live view of the recorder, for the reason
    :class:`~services.event_metrics.RealtimeMetricsSnapshot` records: reading a
    dozen separately-updated counters while other threads increment them produces
    a report whose numbers contradict each other, and a monitoring page that does
    not add up is worse than one that is a second stale.
    """

    #: When this process started counting.
    since: datetime

    created: int
    delivered: int
    failed: int

    #: Notifications not created because the recipient had switched them off.
    #: Not a failure — it is the preference system doing its job, and it is
    #: reported so an operator can see that it is.
    suppressed_by_preference: int
    #: Notifications not created because that person already had that one.
    deduplicated: int
    #: Events discarded because the queue was full. The one counter here that is
    #: a *capacity* signal rather than a health one.
    dropped: int

    #: Mean time from the originating event being published to the notification
    #: being persisted and announced. ``None`` rather than ``0`` when nothing has
    #: been delivered: an average over no deliveries is undefined, while zero
    #: would read as "instantaneous", which is a very different claim.
    average_delivery_latency_ms: float | None

    created_by_rule: dict[str, int]
    failures_by_reason: dict[str, int]

    @property
    def delivery_success_rate(self) -> float:
        """Share of created notifications that were announced, as a percentage.

        ``0.0`` when nothing has been created — there is nothing to have
        succeeded at yet. Same shape and reasoning as every other rate here.
        """
        attempted = self.delivered + self.failed
        if attempted <= 0:
            return 0.0
        return round(self.delivered / attempted * 100, 2)


class NotificationMetricsRecorder(Protocol):
    """What the notification service and its subscriber require of a backend."""

    def record_created(self, rule_key: str) -> None:
        """Record one notification persisted."""
        ...

    def record_delivered(self, *, latency_ms: float) -> None:
        """Record one notification announced onto the event channel.

        The latency is measured from the **originating event** to the
        announcement, deliberately — not to a browser acknowledging it. That
        would measure a client's network, which the platform neither controls nor
        can distinguish from a laptop going to sleep. This measures what the
        platform is responsible for, which is the same line
        :meth:`~services.event_metrics.RealtimeMetricsRecorder.record_delivered`
        draws.
        """
        ...

    def record_failed(self, reason: NotificationFailureReason) -> None:
        """Record one notification that could not be created or announced."""
        ...

    def record_suppressed(self, count: int = 1) -> None:
        """Record notifications not created because of a preference."""
        ...

    def record_deduplicated(self, count: int = 1) -> None:
        """Record notifications not created because they were duplicates."""
        ...

    def record_dropped(self) -> None:
        """Record one event discarded because the queue was full."""
        ...

    def snapshot(self) -> NotificationMetricsSnapshot:
        """Read the counters as one consistent value."""
        ...


class InMemoryNotificationMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a snapshot has to be internally
    consistent: counters read without one can report more deliveries than
    creations. The critical sections are a handful of additions on a path that is
    already doing a database write, so contention is not a consideration.

    Sums are accumulated rather than storing every observation, for the reason
    :class:`~services.search_metrics.InMemorySearchMetrics` records: a running
    total and a count give the same mean in constant memory, and a metrics
    recorder that grows with traffic is a leak with a chart on it.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._created = 0
        self._delivered = 0
        self._failed = 0
        self._suppressed = 0
        self._deduplicated = 0
        self._dropped = 0
        self._latency_sum = 0.0
        self._by_rule: dict[str, int] = {}
        self._failures: dict[str, int] = {}

    def record_created(self, rule_key: str) -> None:
        """Record one notification persisted.

        Counted **by rule**, which is safe where counting by recipient or by case
        would not be: "eleven documents were uploaded" is a throughput figure,
        while "eleven notifications for Amina" is a statement about a person's
        work.
        """
        with self._lock:
            self._created += 1
            self._by_rule[rule_key] = self._by_rule.get(rule_key, 0) + 1

    def record_delivered(self, *, latency_ms: float) -> None:
        """Record one notification announced onto the event channel."""
        with self._lock:
            self._delivered += 1
            self._latency_sum += max(0.0, latency_ms)

    def record_failed(self, reason: NotificationFailureReason) -> None:
        """Record one notification that could not be created or announced.

        The duration is deliberately **not** folded into the latency sum — the
        same reasoning every other recorder here gives for excluding failures: a
        notification that failed because the database was unreachable says
        nothing about how fast delivery is, and averaging it in would make the
        platform look slow when what it actually is, is broken.
        """
        with self._lock:
            self._failed += 1
            self._failures[reason.value] = self._failures.get(reason.value, 0) + 1

    def record_suppressed(self, count: int = 1) -> None:
        """Record notifications not created because of a preference."""
        with self._lock:
            self._suppressed += max(0, count)

    def record_deduplicated(self, count: int = 1) -> None:
        """Record notifications not created because they were duplicates."""
        with self._lock:
            self._deduplicated += max(0, count)

    def record_dropped(self) -> None:
        """Record one event discarded because the queue was full."""
        with self._lock:
            self._dropped += 1

    def snapshot(self) -> NotificationMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return NotificationMetricsSnapshot(
                since=self._since,
                created=self._created,
                delivered=self._delivered,
                failed=self._failed,
                suppressed_by_preference=self._suppressed,
                deduplicated=self._deduplicated,
                dropped=self._dropped,
                average_delivery_latency_ms=(
                    round(self._latency_sum / self._delivered, 2)
                    if self._delivered > 0
                    else None
                ),
                created_by_rule=dict(self._by_rule),
                failures_by_reason=dict(self._failures),
            )

    def reset(self) -> None:
        """Discard every counter.

        For tests, and for an operator who wants a fresh window. Not called by
        the application — the counters are the process's history, and clearing
        them on a schedule would make the ``since`` timestamp a lie.
        """
        with self._lock:
            self._since = datetime.now(UTC)
            self._created = 0
            self._delivered = 0
            self._failed = 0
            self._suppressed = 0
            self._deduplicated = 0
            self._dropped = 0
            self._latency_sum = 0.0
            self._by_rule.clear()
            self._failures.clear()


class NullNotificationMetrics:
    """A recorder that counts nothing.

    The default for a service constructed without observability — a script, or a
    unit test that is not about metrics. Same role and reasoning as
    :class:`~services.event_metrics.NullRealtimeMetrics`.
    """

    def record_created(self, rule_key: str) -> None:
        """Discard the observation."""

    def record_delivered(self, *, latency_ms: float) -> None:
        """Discard the observation."""

    def record_failed(self, reason: NotificationFailureReason) -> None:
        """Discard the observation."""

    def record_suppressed(self, count: int = 1) -> None:
        """Discard the observation."""

    def record_deduplicated(self, count: int = 1) -> None:
        """Discard the observation."""

    def record_dropped(self) -> None:
        """Discard the observation."""

    def snapshot(self) -> NotificationMetricsSnapshot:
        """Report an empty window."""
        return NotificationMetricsSnapshot(
            since=datetime.now(UTC),
            created=0,
            delivered=0,
            failed=0,
            suppressed_by_preference=0,
            deduplicated=0,
            dropped=0,
            average_delivery_latency_ms=None,
            created_by_rule={},
            failures_by_reason={},
        )


#: The one recorder the process shares.
#:
#: Module-level for the reason the search, RAG, assistant, and real-time
#: recorders are: a counter rebuilt per request counts to one. It matters more
#: here than for those three, because most notifications are created on a
#: **background thread** with no request to hang a per-request instance off at
#: all.
_shared = InMemoryNotificationMetrics()


def get_notification_metrics() -> NotificationMetricsRecorder:
    """Return the process-wide notification metrics recorder."""
    return _shared


def reset_notification_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "InMemoryNotificationMetrics",
    "NotificationFailureReason",
    "NotificationMetricsRecorder",
    "NotificationMetricsSnapshot",
    "NullNotificationMetrics",
    "get_notification_metrics",
    "reset_notification_metrics",
]
