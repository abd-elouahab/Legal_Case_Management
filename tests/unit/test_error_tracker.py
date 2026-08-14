"""Unit tests for the error tracker.

Two claims, and the second is the one that makes an error list readable during an
incident rather than after it: failures are **grouped** by what they are and where
they happen, never by their message; and the buffer evicts by **staleness**, so a
rare failure that just happened outranks a common one that stopped an hour ago.
"""

from __future__ import annotations

from core.observability import ErrorCategory, MetricName, MonitoringComponent
from services.error_tracker import InMemoryErrorTracker, NullErrorTracker
from services.metrics_registry import InMemoryMetricsRegistry


def _record(
    tracker: InMemoryErrorTracker,
    *,
    exception_type: str = "ValueError",
    location: str | None = "case.py:12",
    message: str | None = None,
    category: ErrorCategory = ErrorCategory.UNHANDLED,
    component: MonitoringComponent = MonitoringComponent.API,
    operation: str | None = None,
) -> str:
    return tracker.record(
        category=category,
        component=component,
        exception_type=exception_type,
        message=message,
        location=location,
        operation=operation,
    )


class TestGrouping:
    """A class of failure, not a list of occurrences."""

    def test_a_fresh_tracker_reports_an_empty_window(self) -> None:
        snapshot = InMemoryErrorTracker().snapshot()

        assert snapshot.total_errors == 0
        assert snapshot.distinct_errors == 0
        assert snapshot.groups == ()

    def test_the_same_failure_twice_is_one_group_with_two_occurrences(self) -> None:
        tracker = InMemoryErrorTracker()
        _record(tracker)
        _record(tracker)

        snapshot = tracker.snapshot()
        assert snapshot.total_errors == 2
        assert snapshot.distinct_errors == 1
        assert snapshot.groups[0].occurrences == 2

    def test_the_message_does_not_split_a_group(self) -> None:
        """Messages carry identifiers; grouping on one gives a group per request."""
        tracker = InMemoryErrorTracker()
        _record(tracker, message="case 9f2c not found")
        _record(tracker, message="case 41ab not found")

        assert tracker.snapshot().distinct_errors == 1

    def test_a_different_location_is_a_different_group(self) -> None:
        tracker = InMemoryErrorTracker()
        _record(tracker, location="case.py:12")
        _record(tracker, location="document.py:44")

        assert tracker.snapshot().distinct_errors == 2

    def test_a_different_category_is_a_different_group(self) -> None:
        """A 503 the platform answered on purpose is not an unhandled crash."""
        tracker = InMemoryErrorTracker()
        _record(tracker, category=ErrorCategory.UNHANDLED)
        _record(tracker, category=ErrorCategory.BACKGROUND_JOB)

        assert tracker.snapshot().distinct_errors == 2

    def test_it_keeps_totals_by_category_and_component(self) -> None:
        tracker = InMemoryErrorTracker()
        _record(tracker, category=ErrorCategory.EXTERNAL_SERVICE, component=MonitoringComponent.EMAIL)
        _record(tracker, category=ErrorCategory.EXTERNAL_SERVICE, component=MonitoringComponent.EMAIL)
        _record(tracker, category=ErrorCategory.WEBSOCKET, component=MonitoringComponent.REALTIME)

        snapshot = tracker.snapshot()
        assert snapshot.errors_by_category["external_service"] == 2
        assert snapshot.errors_by_component["realtime"] == 1


class TestWhatIsKept:
    """Enough to act on, and nothing that needed protecting."""

    def test_the_sample_message_is_the_most_recent(self) -> None:
        tracker = InMemoryErrorTracker()
        _record(tracker, message="first")
        _record(tracker, message="second")

        assert tracker.snapshot().groups[0].sample_message == "second"

    def test_the_sample_message_is_bounded_and_single_line(self) -> None:
        tracker = InMemoryErrorTracker()
        _record(tracker, message="a" * 5_000 + "\nsecond line")

        message = tracker.snapshot().groups[0].sample_message
        assert message is not None
        assert "\n" not in message
        assert len(message) <= 200

    def test_first_and_last_seen_bracket_the_group(self) -> None:
        tracker = InMemoryErrorTracker()
        _record(tracker)
        _record(tracker)

        group = tracker.snapshot().groups[0]
        assert group.first_seen <= group.last_seen

    def test_a_fingerprint_is_returned_so_a_caller_can_correlate(self) -> None:
        tracker = InMemoryErrorTracker()
        first = _record(tracker)
        second = _record(tracker)

        assert first and first == second


class TestBounds:
    """A tracker that grew with the failure modes would be its own incident."""

    def test_the_group_count_is_capped(self) -> None:
        tracker = InMemoryErrorTracker(max_groups=3)
        for index in range(10):
            _record(tracker, location=f"module{index}.py:1")

        snapshot = tracker.snapshot()
        assert snapshot.distinct_errors == 3
        assert snapshot.evicted_groups == 7

    def test_eviction_drops_the_stalest_rather_than_the_rarest(self) -> None:
        """A rare failure that just happened is news; a common stopped one is history."""
        tracker = InMemoryErrorTracker(max_groups=2)
        for _ in range(50):
            _record(tracker, location="noisy.py:1")
        _record(tracker, location="quiet.py:1")
        _record(tracker, location="newest.py:1")

        locations = {group.location for group in tracker.snapshot().groups}
        assert "noisy.py:1" not in locations
        assert locations == {"quiet.py:1", "newest.py:1"}

    def test_groups_are_reported_most_recently_seen_first(self) -> None:
        tracker = InMemoryErrorTracker()
        _record(tracker, location="older.py:1")
        _record(tracker, location="newer.py:1")

        assert tracker.snapshot().groups[0].location == "newer.py:1"


class TestMetrics:
    """Every failure is also a counter, so an alert has something to read."""

    def test_it_counts_into_the_registry(self) -> None:
        registry = InMemoryMetricsRegistry()
        tracker = InMemoryErrorTracker(metrics=registry)
        _record(tracker)

        snapshot = registry.snapshot()
        assert snapshot.total(MetricName.ERRORS_TOTAL) == 1.0
        assert snapshot.total(MetricName.UNHANDLED_EXCEPTIONS_TOTAL) == 1.0

    def test_a_handled_error_is_not_counted_as_unhandled(self) -> None:
        registry = InMemoryMetricsRegistry()
        tracker = InMemoryErrorTracker(metrics=registry)
        _record(tracker, category=ErrorCategory.HANDLED)

        assert registry.snapshot().total(MetricName.UNHANDLED_EXCEPTIONS_TOTAL) == 0.0


class TestNullTracker:
    """Recording code stays a plain call."""

    def test_it_accepts_everything_and_reports_nothing(self) -> None:
        tracker = NullErrorTracker()
        tracker.record(
            category=ErrorCategory.UNHANDLED,
            component=MonitoringComponent.API,
            exception_type="ValueError",
        )

        assert tracker.snapshot().total_errors == 0
