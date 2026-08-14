"""Unit tests for the dashboard metrics recorder.

Two things are asserted, and the second is the reason this module needed a design
decision rather than a copy of the recorder beside it:

* the **five figures** ``19-dashboard-analytics.md``'s Monitoring section names,
  counted correctly and reported as one consistent snapshot;
* the **distinct-user estimate**, which is the only counter on this platform that
  is handed an identity — and which must be incapable of giving one back.
"""

from __future__ import annotations

import uuid

from services.dashboard_metrics import (
    InMemoryDashboardMetrics,
    NullDashboardMetrics,
    WidgetFailureReason,
)


class TestCounting:
    """The five figures the spec names."""

    def test_a_fresh_recorder_reports_an_empty_window(self) -> None:
        snapshot = InMemoryDashboardMetrics().snapshot()

        assert snapshot.loads == 0
        assert snapshot.refreshes == 0
        assert snapshot.widgets_loaded == 0
        assert snapshot.widgets_failed == 0
        assert snapshot.active_users == 0

    def test_averages_are_none_rather_than_zero_before_anything_is_measured(self) -> None:
        """Zero would read as "instantaneous", which is a very different claim."""
        snapshot = InMemoryDashboardMetrics().snapshot()

        assert snapshot.average_load_ms is None
        assert snapshot.average_widget_ms is None

    def test_it_counts_loads_and_their_duration(self) -> None:
        metrics = InMemoryDashboardMetrics()
        metrics.record_load(duration_ms=100.0, user_id=uuid.uuid4())
        metrics.record_load(duration_ms=200.0, user_id=uuid.uuid4())

        snapshot = metrics.snapshot()
        assert snapshot.loads == 2
        assert snapshot.average_load_ms == 150.0

    def test_a_refresh_is_not_a_load(self) -> None:
        """Averaging a one-widget refresh into the page average would flatter it."""
        metrics = InMemoryDashboardMetrics()
        metrics.record_load(duration_ms=400.0, user_id=uuid.uuid4())
        metrics.record_refresh(user_id=uuid.uuid4())

        snapshot = metrics.snapshot()
        assert snapshot.loads == 1
        assert snapshot.refreshes == 1
        assert snapshot.average_load_ms == 400.0

    def test_it_counts_widgets_per_key(self) -> None:
        """What turns "the dashboard is slow" into a sentence somebody can act on."""
        metrics = InMemoryDashboardMetrics()
        metrics.record_widget("my_cases", duration_ms=10.0)
        metrics.record_widget("my_cases", duration_ms=30.0)
        metrics.record_widget("storage_usage", duration_ms=900.0)

        snapshot = metrics.snapshot()
        assert snapshot.widgets_loaded == 3
        assert snapshot.average_ms_by_widget["my_cases"] == 20.0
        assert snapshot.average_ms_by_widget["storage_usage"] == 900.0

    def test_a_failure_does_not_move_the_duration_average(self) -> None:
        """A widget that failed fast says nothing about how fast the platform is."""
        metrics = InMemoryDashboardMetrics()
        metrics.record_widget("my_cases", duration_ms=100.0)
        metrics.record_widget_failure("storage_usage", WidgetFailureReason.QUERY_FAILED)

        snapshot = metrics.snapshot()
        assert snapshot.average_widget_ms == 100.0
        assert "storage_usage" not in snapshot.average_ms_by_widget

    def test_it_breaks_failures_down_by_widget_and_by_cause(self) -> None:
        metrics = InMemoryDashboardMetrics()
        metrics.record_widget_failure("my_cases", WidgetFailureReason.QUERY_FAILED)
        metrics.record_widget_failure("ocr_status", WidgetFailureReason.BUDGET_EXHAUSTED)
        metrics.record_widget_failure("ocr_status", WidgetFailureReason.BUDGET_EXHAUSTED)

        snapshot = metrics.snapshot()
        assert snapshot.failures_by_widget == {"my_cases": 1, "ocr_status": 2}
        assert snapshot.failures_by_reason == {"query_failed": 1, "budget_exhausted": 2}

    def test_the_success_rate_is_zero_before_anything_is_attempted(self) -> None:
        assert InMemoryDashboardMetrics().snapshot().widget_success_rate == 0.0

    def test_the_success_rate_counts_both_outcomes(self) -> None:
        metrics = InMemoryDashboardMetrics()
        for _ in range(3):
            metrics.record_widget("my_cases", duration_ms=1.0)
        metrics.record_widget_failure("my_cases", WidgetFailureReason.QUERY_FAILED)

        assert metrics.snapshot().widget_success_rate == 75.0

    def test_a_negative_duration_cannot_lower_an_average(self) -> None:
        metrics = InMemoryDashboardMetrics()
        metrics.record_widget("my_cases", duration_ms=-50.0)
        assert metrics.snapshot().average_widget_ms == 0.0

    def test_reset_clears_everything(self) -> None:
        metrics = InMemoryDashboardMetrics()
        metrics.record_load(duration_ms=10.0, user_id=uuid.uuid4())
        metrics.record_widget("my_cases", duration_ms=10.0)

        before = metrics.snapshot().since
        metrics.reset()
        snapshot = metrics.snapshot()

        assert snapshot.loads == 0
        assert snapshot.widgets_loaded == 0
        assert snapshot.active_users == 0
        assert snapshot.since >= before


class TestActiveUsers:
    """The one counter handed an identity, and the one that must not keep it."""

    def test_it_counts_distinct_people(self) -> None:
        metrics = InMemoryDashboardMetrics()
        amina, youssef = uuid.uuid4(), uuid.uuid4()

        metrics.record_load(duration_ms=1.0, user_id=amina)
        metrics.record_load(duration_ms=1.0, user_id=amina)
        metrics.record_load(duration_ms=1.0, user_id=youssef)

        snapshot = metrics.snapshot()
        assert snapshot.loads == 3
        assert snapshot.active_users == 2

    def test_a_refresh_counts_towards_the_same_estimate(self) -> None:
        metrics = InMemoryDashboardMetrics()
        metrics.record_refresh(user_id=uuid.uuid4())
        assert metrics.snapshot().active_users == 1

    def test_no_identifier_survives_the_recorder(self) -> None:
        """**The property that made a salted digest the right answer.**

        The recorder is given a user id and keeps only an unrecoverable digest, so
        the process cannot be asked who was seen — the snapshot exposes a
        cardinality and nothing else.
        """
        metrics = InMemoryDashboardMetrics()
        amina = uuid.uuid4()
        metrics.record_load(duration_ms=1.0, user_id=amina)

        held = metrics._users
        assert amina.bytes not in held
        assert str(amina).encode() not in held
        assert all(len(digest) == 16 for digest in held)

    def test_two_processes_produce_different_digests_for_one_person(self) -> None:
        """A per-process salt is what stops two recorders' sets being joined."""
        amina = uuid.uuid4()
        first, second = InMemoryDashboardMetrics(), InMemoryDashboardMetrics()

        first.record_load(duration_ms=1.0, user_id=amina)
        second.record_load(duration_ms=1.0, user_id=amina)

        assert first._users != second._users

    def test_the_estimate_is_capped_and_says_so(self, monkeypatch: object) -> None:
        """A figure that silently stopped growing would be a lie."""
        import services.dashboard_metrics as module

        original = module.MAX_TRACKED_USERS
        try:
            module.MAX_TRACKED_USERS = 3  # type: ignore[misc]
            metrics = InMemoryDashboardMetrics()
            for _ in range(10):
                metrics.record_load(duration_ms=1.0, user_id=uuid.uuid4())

            snapshot = metrics.snapshot()
            assert snapshot.active_users == 3
            assert snapshot.active_users_capped is True
        finally:
            module.MAX_TRACKED_USERS = original  # type: ignore[misc]


class TestNullRecorder:
    """The default for a service built without observability."""

    def test_it_counts_nothing_and_still_answers(self) -> None:
        metrics = NullDashboardMetrics()
        metrics.record_load(duration_ms=1.0, user_id=uuid.uuid4())
        metrics.record_refresh(user_id=uuid.uuid4())
        metrics.record_widget("my_cases", duration_ms=1.0)
        metrics.record_widget_failure("my_cases", WidgetFailureReason.UNKNOWN)

        snapshot = metrics.snapshot()
        assert snapshot.loads == 0
        assert snapshot.widgets_loaded == 0
        assert snapshot.average_load_ms is None
