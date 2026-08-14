"""Dashboard observability.

``19-dashboard-analytics.md``'s Monitoring section names five figures:
**dashboard load time, widget load time, refresh frequency, failed widget
requests, and active dashboard users**. All five are here, in the shape
:mod:`services.search_metrics`, :mod:`services.rag_metrics`,
:mod:`services.assistant_metrics`, :mod:`services.event_metrics`, and
:mod:`services.notification_metrics` established — a protocol, an in-memory
implementation, a null implementation, and a frozen snapshot.

Unlike every one of those, **none of these figures could have been a SQL
aggregate**, and the reason is what makes this module simple: the dashboard
persists nothing. There is no dashboard table, no saved layout, and no recorded
load, so there is no split between "counted in this process" and "counted in the
database" to explain — every number here carries ``since`` and every one of them
resets on restart. That is stated once, and the endpoint says it too.

**Active dashboard users is the one figure that needed a decision.** Counting
*distinct* people requires remembering who has been seen, and every other
recorder on this platform is built so that there is nowhere for an identity to
go. The resolution is that identities are not kept: a user is folded in as a
**salted digest**, the salt is random per process and never leaves it, and only
the *cardinality* of the resulting set is ever readable. So the recorder can
answer "seventeen people opened a dashboard" and cannot answer "was Amina one of
them" — not because nothing asks, but because the answer was destroyed on the way
in. The set is capped for the same reason a payload is bounded: an unbounded
in-memory structure that grows with traffic is a leak with a chart on it.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

#: Most distinct users the "active dashboard users" estimate tracks.
#:
#: Past it the count stops rising and :attr:`DashboardMetricsSnapshot.active_users_capped`
#: says so, exactly as the notification badge's unread count does: a figure that
#: silently stopped growing would be a lie, while one that says "this many or
#: more" is a measurement with a stated limit.
MAX_TRACKED_USERS: Final[int] = 10_000

#: Bytes of digest kept per user. Sixteen is far past the collision floor for a
#: population this platform will ever have, and the truncation is what keeps the
#: set's memory proportional to the cap rather than to SHA-256's output.
_DIGEST_BYTES: Final[int] = 16


class WidgetFailureReason(StrEnum):
    """Why a widget did not produce data.

    A closed vocabulary rather than free text, for the reason every other
    ``*_metrics`` module here uses one: these are grouped in a monitoring view,
    and a message interpolated from an exception would produce a breakdown with
    one bucket per occurrence.
    """

    #: The widget's query failed — a database error, a constraint, a timeout
    #: inside the driver. The reason to page somebody.
    QUERY_FAILED = "query_failed"
    #: The dashboard ran out of its time budget before reaching this widget, so
    #: it was never attempted. **Not a fault**: it is the load shedding that keeps
    #: one slow widget from delaying the whole page, and it is counted separately
    #: so a rising number reads as "the dashboard is too big" rather than "the
    #: database is broken".
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: Anything the loader did not anticipate. Always worth investigating.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DashboardMetricsSnapshot:
    """The counters at one instant, as the monitoring endpoint reports them.

    Frozen rather than a live view of the recorder, for the reason every other
    snapshot here is: reading a dozen separately-updated counters while other
    threads increment them produces a report whose numbers contradict each other,
    and a monitoring page that does not add up is worse than one that is a second
    stale.
    """

    #: When this process started counting.
    since: datetime

    #: Full dashboard loads served.
    loads: int
    #: Single-widget refreshes served. Separate from :attr:`loads` because the
    #: spec asks for *refresh frequency*, and the whole point of the per-widget
    #: endpoint is that a refresh is not a load.
    refreshes: int

    #: Widgets that produced data, across both endpoints.
    widgets_loaded: int
    #: Widgets that did not. The spec's "failed widget requests".
    widgets_failed: int

    #: Mean wall time of a full dashboard load. ``None`` rather than ``0`` when
    #: none has been served: an average over no observations is undefined, while
    #: zero would read as "instantaneous".
    average_load_ms: float | None
    #: Mean wall time of one widget, across every widget.
    average_widget_ms: float | None

    #: Distinct people who have loaded a dashboard in this process, estimated
    #: from salted digests — see the module docstring.
    active_users: int
    #: Whether that estimate has reached :data:`MAX_TRACKED_USERS`.
    active_users_capped: bool

    #: Mean wall time per widget key. What turns "the dashboard is slow" into
    #: "``storage_usage`` is slow", which is the only form of that sentence
    #: anybody can act on.
    average_ms_by_widget: dict[str, float]
    #: Failures per widget key.
    failures_by_widget: dict[str, int]
    #: Failures per cause.
    failures_by_reason: dict[str, int]

    @property
    def widget_success_rate(self) -> float:
        """Share of widget loads that produced data, as a percentage.

        ``0.0`` when nothing has been attempted — there is nothing to have
        succeeded at yet. Same shape and reasoning as every other rate here.
        """
        attempted = self.widgets_loaded + self.widgets_failed
        if attempted <= 0:
            return 0.0
        return round(self.widgets_loaded / attempted * 100, 2)


class DashboardMetricsRecorder(Protocol):
    """What the dashboard service requires of a metrics backend."""

    def record_load(self, *, duration_ms: float, user_id: uuid.UUID) -> None:
        """Record one full dashboard load and who made it.

        The identifier is folded into a salted digest and discarded; see the
        module docstring. It is passed as a ``uuid.UUID`` rather than as a string
        so a caller cannot hand this a name, an email, or a case number by mistake.
        """
        ...

    def record_refresh(self, *, user_id: uuid.UUID) -> None:
        """Record one single-widget refresh."""
        ...

    def record_widget(self, widget: str, *, duration_ms: float) -> None:
        """Record one widget that produced data."""
        ...

    def record_widget_failure(self, widget: str, reason: WidgetFailureReason) -> None:
        """Record one widget that did not."""
        ...

    def snapshot(self) -> DashboardMetricsSnapshot:
        """Read the counters as one consistent value."""
        ...


class InMemoryDashboardMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a snapshot has to be internally
    consistent: counters read without one can report more widget loads than
    dashboard loads. The critical sections are a handful of additions on a path
    that has already done several database queries, so contention is not a
    consideration.

    Sums are accumulated rather than storing every observation, for the reason
    :class:`~services.search_metrics.InMemorySearchMetrics` records: a running
    total and a count give the same mean in constant memory.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        # Random per process and never exposed. Two API instances therefore
        # produce different digests for the same person, which is a feature: the
        # sets cannot be joined to reconstruct a population.
        self._salt = secrets.token_bytes(16)
        self._loads = 0
        self._refreshes = 0
        self._load_ms = 0.0
        self._widgets_loaded = 0
        self._widgets_failed = 0
        self._widget_ms = 0.0
        self._users: set[bytes] = set()
        self._users_capped = False
        self._by_widget_ms: dict[str, float] = {}
        self._by_widget_count: dict[str, int] = {}
        self._failures_by_widget: dict[str, int] = {}
        self._failures_by_reason: dict[str, int] = {}

    def record_load(self, *, duration_ms: float, user_id: uuid.UUID) -> None:
        """Record one full dashboard load and who made it."""
        with self._lock:
            self._loads += 1
            self._load_ms += max(0.0, duration_ms)
            self._remember(user_id)

    def record_refresh(self, *, user_id: uuid.UUID) -> None:
        """Record one single-widget refresh.

        The duration is **not** folded into the load average: a refresh reads one
        widget and a load reads a page of them, so averaging the two together
        would make a dashboard look faster every time somebody refreshed a tile.
        The widget's own duration is recorded by
        :meth:`record_widget`, which the refresh path calls like any other.
        """
        with self._lock:
            self._refreshes += 1
            self._remember(user_id)

    def record_widget(self, widget: str, *, duration_ms: float) -> None:
        """Record one widget that produced data."""
        elapsed = max(0.0, duration_ms)
        with self._lock:
            self._widgets_loaded += 1
            self._widget_ms += elapsed
            self._by_widget_ms[widget] = self._by_widget_ms.get(widget, 0.0) + elapsed
            self._by_widget_count[widget] = self._by_widget_count.get(widget, 0) + 1

    def record_widget_failure(self, widget: str, reason: WidgetFailureReason) -> None:
        """Record one widget that did not.

        The duration is deliberately **not** folded into the widget average — the
        same reasoning every other recorder here gives for excluding failures: a
        widget that failed in eight milliseconds because a table was missing says
        nothing about how fast the dashboard is, and averaging it in would make
        the platform look quick when what it actually is, is broken.
        """
        with self._lock:
            self._widgets_failed += 1
            self._failures_by_widget[widget] = self._failures_by_widget.get(widget, 0) + 1
            self._failures_by_reason[reason.value] = (
                self._failures_by_reason.get(reason.value, 0) + 1
            )

    def snapshot(self) -> DashboardMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return DashboardMetricsSnapshot(
                since=self._since,
                loads=self._loads,
                refreshes=self._refreshes,
                widgets_loaded=self._widgets_loaded,
                widgets_failed=self._widgets_failed,
                average_load_ms=(
                    round(self._load_ms / self._loads, 2) if self._loads > 0 else None
                ),
                average_widget_ms=(
                    round(self._widget_ms / self._widgets_loaded, 2)
                    if self._widgets_loaded > 0
                    else None
                ),
                active_users=len(self._users),
                active_users_capped=self._users_capped,
                average_ms_by_widget={
                    widget: round(total / self._by_widget_count[widget], 2)
                    for widget, total in self._by_widget_ms.items()
                    if self._by_widget_count.get(widget)
                },
                failures_by_widget=dict(self._failures_by_widget),
                failures_by_reason=dict(self._failures_by_reason),
            )

    def reset(self) -> None:
        """Discard every counter, and re-salt.

        For tests, and for an operator who wants a fresh window. Not called by the
        application — the counters are the process's history, and clearing them on
        a schedule would make the ``since`` timestamp a lie.
        """
        with self._lock:
            self._since = datetime.now(UTC)
            self._salt = secrets.token_bytes(16)
            self._loads = 0
            self._refreshes = 0
            self._load_ms = 0.0
            self._widgets_loaded = 0
            self._widgets_failed = 0
            self._widget_ms = 0.0
            self._users.clear()
            self._users_capped = False
            self._by_widget_ms.clear()
            self._by_widget_count.clear()
            self._failures_by_widget.clear()
            self._failures_by_reason.clear()

    # --------------------------------------------------------------- helpers #

    def _remember(self, user_id: uuid.UUID) -> None:
        """Fold one identity into the distinct-user estimate, then forget it.

        Called with the lock already held. The digest is the only thing kept, and
        it is unusable outside this process because the salt is.
        """
        if self._users_capped:
            return
        digest = hashlib.blake2b(
            user_id.bytes, key=self._salt, digest_size=_DIGEST_BYTES
        ).digest()
        self._users.add(digest)
        if len(self._users) >= MAX_TRACKED_USERS:
            self._users_capped = True


class NullDashboardMetrics:
    """A recorder that counts nothing.

    The default for a service constructed without observability — a script, or a
    unit test that is not about metrics. Same role and reasoning as
    :class:`~services.notification_metrics.NullNotificationMetrics`.
    """

    def record_load(self, *, duration_ms: float, user_id: uuid.UUID) -> None:
        """Discard the observation."""

    def record_refresh(self, *, user_id: uuid.UUID) -> None:
        """Discard the observation."""

    def record_widget(self, widget: str, *, duration_ms: float) -> None:
        """Discard the observation."""

    def record_widget_failure(self, widget: str, reason: WidgetFailureReason) -> None:
        """Discard the observation."""

    def snapshot(self) -> DashboardMetricsSnapshot:
        """Report an empty window."""
        return DashboardMetricsSnapshot(
            since=datetime.now(UTC),
            loads=0,
            refreshes=0,
            widgets_loaded=0,
            widgets_failed=0,
            average_load_ms=None,
            average_widget_ms=None,
            active_users=0,
            active_users_capped=False,
            average_ms_by_widget={},
            failures_by_widget={},
            failures_by_reason={},
        )


#: The one recorder the process shares.
#:
#: Module-level for the reason every other recorder here is: a counter rebuilt
#: per request counts to one.
_shared = InMemoryDashboardMetrics()


def get_dashboard_metrics() -> DashboardMetricsRecorder:
    """Return the process-wide dashboard metrics recorder."""
    return _shared


def reset_dashboard_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "MAX_TRACKED_USERS",
    "DashboardMetricsRecorder",
    "DashboardMetricsSnapshot",
    "InMemoryDashboardMetrics",
    "NullDashboardMetrics",
    "WidgetFailureReason",
    "get_dashboard_metrics",
    "reset_dashboard_metrics",
]
