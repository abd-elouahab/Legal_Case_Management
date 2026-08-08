"""Real-time observability.

``15-real-time-synchronization.md``'s Monitoring section names five figures:
**active connections, event throughput, average delivery latency, failed
deliveries, and reconnect count**. This module records them, in the shape
:mod:`services.search_metrics`, :mod:`services.rag_metrics`, and
:mod:`services.assistant_metrics` already established — a protocol, an in-memory
implementation, a null implementation, and a frozen snapshot.

**Why there is no table for this, and no migration.** The same reasoning search
recorded, only more so: a connection is not a persisted run with a lifecycle
anybody polls, and a *delivery* is the platform's highest-frequency operation by
an order of magnitude. A row per delivered event would be write amplification
proportional to the number of connected users times the number of things that
happen, to store something derived entirely from data that is already in
PostgreSQL.

So the figures accumulate **in the process**, and the limits of that are stated
rather than hidden: counters reset when the API restarts, and each instance
counts only its own connections. That is not a gap in the same way it is for
search — *active connections* is inherently per-process, because a connection is
held by exactly one process — but throughput and latency are, and ``since`` is
the honest caveat on the endpoint.

**Nothing identifying can be recorded here, by construction.** The recorder is
handed durations, counts, and enum members; there is no parameter for a user, a
case, a topic, or a payload, so there is nowhere for one to go. That is the same
structural guarantee :class:`~services.search_metrics.SearchMetricsRecorder`
makes, and it matters more here: a per-topic counter would be a live index of
which matters are being worked on.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from core.events import DomainEventType
from core.realtime import RealtimeErrorCode


@dataclass(frozen=True, slots=True)
class RealtimeMetricsSnapshot:
    """The counters at one instant, as the monitoring endpoint reports them.

    Frozen rather than a live view of the recorder, for the reason
    :class:`~services.search_metrics.SearchMetricsSnapshot` records: reading a
    dozen separately-updated counters while other threads increment them
    produces a report whose numbers contradict each other, and a monitoring page
    that does not add up is worse than one that is a second stale.
    """

    #: When this process started counting.
    since: datetime

    # --- Connections -------------------------------------------------------- #
    active_connections: int
    #: Distinct accounts holding at least one connection. The "presence" figure —
    #: a user with three tabs is one person, and reporting three would make the
    #: number a measure of browsing habits rather than of who is working.
    present_users: int
    total_connections: int
    total_disconnections: int
    #: Connections that arrived declaring a previous sequence — the spec's
    #: "reconnect count". Counted separately from `total_connections` because the
    #: ratio is the signal: a healthy deployment reconnects rarely.
    reconnections: int
    rejected_connections: int

    # --- Events ------------------------------------------------------------- #
    events_published: int
    #: Events the dispatcher refused to build — a bad payload or an unscoped
    #: type. Always a publisher bug, so a non-zero value here is actionable in a
    #: way none of the other counters are.
    events_rejected: int
    events_delivered: int
    #: Deliveries suppressed because the connection was not authorized for the
    #: topic *at the moment of delivery*. Not a failure: it is the authorization
    #: layer doing its job, and it is reported so an operator can see that it is.
    events_denied: int
    #: Deliveries suppressed because the connection had already received that
    #: event — the duplicate-avoidance the spec asks for, made visible.
    events_deduplicated: int
    failed_deliveries: int

    # --- Latency ------------------------------------------------------------ #
    #: Mean time from an event being published to it being written to a socket.
    #: The spec's "average delivery latency". ``None`` rather than ``0`` when
    #: nothing has been delivered: an average over no deliveries is undefined,
    #: while zero would read as "instantaneous", which is a very different claim.
    average_delivery_latency_ms: float | None

    events_by_type: dict[str, int]
    failures_by_code: dict[str, int]
    subscriber_failures: dict[str, int]

    @property
    def delivery_success_rate(self) -> float:
        """Share of attempted deliveries that reached a socket, as a percentage.

        ``0.0`` when nothing has been attempted — there is nothing to have
        succeeded at yet. Same shape and reasoning as
        :attr:`~services.search_metrics.SearchMetricsSnapshot.success_rate`.
        """
        attempted = self.events_delivered + self.failed_deliveries
        if attempted <= 0:
            return 0.0
        return round(self.events_delivered / attempted * 100, 2)

    @property
    def average_fanout(self) -> float | None:
        """Mean number of connections one published event reached.

        The figure that says whether the channel is earning its keep: a fanout
        near zero means events are being published to nobody, which is either a
        quiet platform or a client that is not subscribing.
        """
        if self.events_published <= 0:
            return None
        return round(self.events_delivered / self.events_published, 2)


class RealtimeMetricsRecorder(Protocol):
    """What the dispatcher and the connection manager require of a backend.

    Every method takes counts, durations, and enum members — and **no user, no
    case, no topic, and no payload**. A recorder that cannot be handed one cannot
    leak one.
    """

    def record_connected(self, *, resumed: bool) -> None:
        """Record one client connecting, and whether it declared a prior session."""
        ...

    def record_disconnected(self) -> None:
        """Record one client disconnecting."""
        ...

    def record_connection_rejected(self, code: RealtimeErrorCode) -> None:
        """Record one connection refused before it became usable."""
        ...

    def record_published(self, event_type: DomainEventType) -> None:
        """Record one event accepted by the dispatcher."""
        ...

    def record_publish_rejected(self) -> None:
        """Record one event the dispatcher refused to build."""
        ...

    def record_delivered(self, *, latency_ms: float) -> None:
        """Record one event written towards one connection."""
        ...

    def record_delivery_denied(self) -> None:
        """Record one delivery suppressed by authorization."""
        ...

    def record_delivery_deduplicated(self) -> None:
        """Record one delivery suppressed as a duplicate."""
        ...

    def record_delivery_failed(self, code: RealtimeErrorCode) -> None:
        """Record one delivery that could not be made."""
        ...

    def record_subscriber_failure(self, subscriber: str) -> None:
        """Record one subscriber raising out of its handler."""
        ...

    def snapshot(self, *, active_connections: int, present_users: int) -> RealtimeMetricsSnapshot:
        """Read the counters.

        The two live gauges are passed **in** rather than counted here, and that
        is deliberate: the connection manager is the authority on how many
        connections it holds, and a counter incremented on connect and
        decremented on disconnect would drift the first time a socket died
        without either. A gauge read from the thing that owns it cannot drift.
        """
        ...


class InMemoryRealtimeMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a snapshot has to be internally
    consistent: a dozen separately-updated counters read without one can report
    more deliveries than publications. The critical sections are a handful of
    additions on a path that is already doing JSON encoding and socket writes, so
    contention is not a consideration.

    Sums are accumulated rather than storing every observation, for the reason
    :class:`~services.search_metrics.InMemorySearchMetrics` records: a running
    total and a count give the same mean in constant memory, and a metrics
    recorder that grows with traffic is a leak with a chart on it. The trade is
    that percentiles are unavailable — which is what a future implementation of
    this protocol would add.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._connections = 0
        self._disconnections = 0
        self._reconnections = 0
        self._rejected_connections = 0
        self._published = 0
        self._publish_rejected = 0
        self._delivered = 0
        self._denied = 0
        self._deduplicated = 0
        self._delivery_failed = 0
        self._latency_sum = 0.0
        self._by_type: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._subscriber_failures: dict[str, int] = {}

    # --------------------------------------------------------- connections #

    def record_connected(self, *, resumed: bool) -> None:
        """Record one client connecting."""
        with self._lock:
            self._connections += 1
            if resumed:
                self._reconnections += 1

    def record_disconnected(self) -> None:
        """Record one client disconnecting."""
        with self._lock:
            self._disconnections += 1

    def record_connection_rejected(self, code: RealtimeErrorCode) -> None:
        """Record one connection refused before it became usable."""
        with self._lock:
            self._rejected_connections += 1
            self._failures[code.value] = self._failures.get(code.value, 0) + 1

    # -------------------------------------------------------------- events #

    def record_published(self, event_type: DomainEventType) -> None:
        """Record one event accepted by the dispatcher.

        Counted **by type**, which is safe where counting by topic would not be:
        "eleven documents were uploaded" is a throughput figure, while "eleven
        events on case X" is a statement about a client's matter.
        """
        with self._lock:
            self._published += 1
            self._by_type[event_type.value] = self._by_type.get(event_type.value, 0) + 1

    def record_publish_rejected(self) -> None:
        """Record one event the dispatcher refused to build."""
        with self._lock:
            self._publish_rejected += 1

    def record_delivered(self, *, latency_ms: float) -> None:
        """Record one event written towards one connection."""
        with self._lock:
            self._delivered += 1
            self._latency_sum += max(0.0, latency_ms)

    def record_delivery_denied(self) -> None:
        """Record one delivery suppressed by authorization."""
        with self._lock:
            self._denied += 1

    def record_delivery_deduplicated(self) -> None:
        """Record one delivery suppressed as a duplicate."""
        with self._lock:
            self._deduplicated += 1

    def record_delivery_failed(self, code: RealtimeErrorCode) -> None:
        """Record one delivery that could not be made, and why.

        The duration is deliberately **not** folded into the latency sum — the
        same reasoning the search recorder gives for excluding failures: a
        delivery that failed because a queue overflowed says nothing about how
        fast delivery is, and averaging it in would make the platform look slow
        when what it actually is, is overloaded.
        """
        with self._lock:
            self._delivery_failed += 1
            self._failures[code.value] = self._failures.get(code.value, 0) + 1

    def record_subscriber_failure(self, subscriber: str) -> None:
        """Record one subscriber raising out of its handler."""
        with self._lock:
            self._subscriber_failures[subscriber] = (
                self._subscriber_failures.get(subscriber, 0) + 1
            )

    # ------------------------------------------------------------ snapshot #

    def snapshot(self, *, active_connections: int, present_users: int) -> RealtimeMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return RealtimeMetricsSnapshot(
                since=self._since,
                active_connections=max(0, active_connections),
                present_users=max(0, present_users),
                total_connections=self._connections,
                total_disconnections=self._disconnections,
                reconnections=self._reconnections,
                rejected_connections=self._rejected_connections,
                events_published=self._published,
                events_rejected=self._publish_rejected,
                events_delivered=self._delivered,
                events_denied=self._denied,
                events_deduplicated=self._deduplicated,
                failed_deliveries=self._delivery_failed,
                average_delivery_latency_ms=(
                    round(self._latency_sum / self._delivered, 2) if self._delivered > 0 else None
                ),
                events_by_type=dict(self._by_type),
                failures_by_code=dict(self._failures),
                subscriber_failures=dict(self._subscriber_failures),
            )

    def reset(self) -> None:
        """Discard every counter.

        For tests, and for an operator who wants a fresh window. Not called by
        the application — the counters are the process's history, and clearing
        them on a schedule would make the ``since`` timestamp a lie.
        """
        with self._lock:
            self._since = datetime.now(UTC)
            self._connections = 0
            self._disconnections = 0
            self._reconnections = 0
            self._rejected_connections = 0
            self._published = 0
            self._publish_rejected = 0
            self._delivered = 0
            self._denied = 0
            self._deduplicated = 0
            self._delivery_failed = 0
            self._latency_sum = 0.0
            self._by_type.clear()
            self._failures.clear()
            self._subscriber_failures.clear()


class NullRealtimeMetrics:
    """A recorder that counts nothing.

    The default for a dispatcher or manager constructed without observability —
    a script, or a unit test that is not about metrics. Same role and reasoning
    as :class:`~services.search_metrics.NullSearchMetrics`.
    """

    def record_connected(self, *, resumed: bool) -> None:
        """Discard the observation."""

    def record_disconnected(self) -> None:
        """Discard the observation."""

    def record_connection_rejected(self, code: RealtimeErrorCode) -> None:
        """Discard the observation."""

    def record_published(self, event_type: DomainEventType) -> None:
        """Discard the observation."""

    def record_publish_rejected(self) -> None:
        """Discard the observation."""

    def record_delivered(self, *, latency_ms: float) -> None:
        """Discard the observation."""

    def record_delivery_denied(self) -> None:
        """Discard the observation."""

    def record_delivery_deduplicated(self) -> None:
        """Discard the observation."""

    def record_delivery_failed(self, code: RealtimeErrorCode) -> None:
        """Discard the observation."""

    def record_subscriber_failure(self, subscriber: str) -> None:
        """Discard the observation."""

    def snapshot(self, *, active_connections: int, present_users: int) -> RealtimeMetricsSnapshot:
        """Report an empty window."""
        return RealtimeMetricsSnapshot(
            since=datetime.now(UTC),
            active_connections=max(0, active_connections),
            present_users=max(0, present_users),
            total_connections=0,
            total_disconnections=0,
            reconnections=0,
            rejected_connections=0,
            events_published=0,
            events_rejected=0,
            events_delivered=0,
            events_denied=0,
            events_deduplicated=0,
            failed_deliveries=0,
            average_delivery_latency_ms=None,
            events_by_type={},
            failures_by_code={},
            subscriber_failures={},
        )


#: The one recorder the process shares.
#:
#: Module-level for the reason the search and RAG recorders are: per-request
#: counters would count to one and reset, and the metric is a property of the
#: *process* — which for active connections it is not merely by convention but by
#: definition, since a socket is held by exactly one process.
_shared = InMemoryRealtimeMetrics()


def get_realtime_metrics() -> RealtimeMetricsRecorder:
    """Return the process-wide real-time metrics recorder."""
    return _shared


def reset_realtime_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "InMemoryRealtimeMetrics",
    "NullRealtimeMetrics",
    "RealtimeMetricsRecorder",
    "RealtimeMetricsSnapshot",
    "get_realtime_metrics",
    "reset_realtime_metrics",
]
