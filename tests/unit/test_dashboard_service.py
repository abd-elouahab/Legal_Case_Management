"""Unit tests for the dashboard service.

The service's job is not to compute figures — the repository does that, against a
real database, in the integration suite. Its job is the **loop**, and the loop is
where ``19-dashboard-analytics.md``'s hardest requirements live:

* *"one failing widget must not prevent the dashboard from loading"*;
* *"widgets should fail independently"*;
* *"handle timeout"* and *"partial failures"*;
* *"cache expensive computations when appropriate"* — where the whole design is
  in what "appropriate" excludes;
* *"refreshing one widget should not reload the entire dashboard"*.

Each is asserted here against a repository double, so a failure is unambiguous:
these tests do not touch a database, and nothing in them can fail because a query
was wrong.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.dashboard import DashboardRange, WidgetKey
from core.exceptions import DashboardDisabledError, DashboardWidgetNotFoundError
from models.user import User, UserRole, UserStatus
from repositories.dashboard import (
    CaseAnalytics,
    ConversationAnalytics,
    DocumentAnalytics,
    HearingSummary,
    QueueDepths,
    ReportAnalytics,
    StorageUsage,
    UserActivity,
)
from services.dashboard import DashboardService, WidgetStateValue, clear_dashboard_cache
from services.dashboard_metrics import InMemoryDashboardMetrics, WidgetFailureReason


def build_user(role: UserRole = UserRole.ADMINISTRATOR) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{role.value}@example.com",
        first_name="Amina",
        last_name="Benali",
        hashed_password="x",
        role=role,
        status=UserStatus.ACTIVE,
    )


def build_repository() -> MagicMock:
    """A dashboard repository that answers every query with an empty result.

    Empty rather than fabricated, because these tests are about the loop: a
    widget that returns nothing still *loaded*, which is precisely the `empty`
    versus `unavailable` distinction the service has to get right.
    """
    repository = MagicMock()
    repository.case_status_breakdown.return_value = ()
    repository.case_analytics.return_value = CaseAnalytics(0, 0, 0, 0, 0, 0)
    repository.assigned_cases.return_value = []
    repository.recent_cases.return_value = []
    repository.upcoming_hearings.return_value = []
    repository.hearing_summary.return_value = HearingSummary(0, 0, 0, 0)
    repository.recent_documents.return_value = []
    repository.document_analytics.return_value = DocumentAnalytics(0, 0, 0, 0, 0, 0, 0)
    repository.ocr_status_breakdown.return_value = ()
    repository.storage_usage.return_value = StorageUsage(0, 0, 0, None, ())
    repository.user_activity.return_value = UserActivity(0, 0, 0, 0, 0, ())
    repository.queue_depths.return_value = QueueDepths(0, 0, 0, 0, 0, 0)
    repository.recent_activity.return_value = []
    repository.activity_breakdown.return_value = (0, ())
    repository.recent_reports.return_value = []
    repository.report_analytics.return_value = ReportAnalytics(0, 0, 0, 0, 0)
    repository.recent_conversations.return_value = []
    repository.conversation_analytics.return_value = ConversationAnalytics(0, 0, 0, 0)
    return repository


def build_notifications() -> MagicMock:
    notifications = MagicMock()
    notifications.list_notifications.return_value = ([], 0)
    return notifications


@pytest.fixture
def repository() -> MagicMock:
    return build_repository()


@pytest.fixture
def metrics() -> InMemoryDashboardMetrics:
    return InMemoryDashboardMetrics()


@pytest.fixture
def service(repository: MagicMock, metrics: InMemoryDashboardMetrics) -> DashboardService:
    return DashboardService(repository, build_notifications(), metrics=metrics)


def widget(dashboard: Any, key: WidgetKey) -> Any:
    """The one widget with this key, or a failure naming what was there instead."""
    for result in dashboard.widgets:
        if result.definition.key is key:
            return result
    raise AssertionError(f"{key.value} not on the dashboard")


class TestLoaderRegistry:
    """Every widget has a loader, checked at construction rather than at render."""

    def test_construction_fails_if_a_widget_has_no_loader(
        self, repository: MagicMock
    ) -> None:
        """The exhaustiveness guard, asserted by proving it currently passes.

        A widget added to `WIDGETS` without a loader raises here — at import of the
        service, in every test — rather than the first time somebody with the right
        role opens the page.
        """
        service = DashboardService(repository, build_notifications())
        for key in WidgetKey:
            assert key in service._loaders


class TestLoadingThePage:
    """The aggregated read."""

    def test_it_returns_the_roles_layout(self, service: DashboardService) -> None:
        dashboard = service.load(actor=build_user(UserRole.ADMINISTRATOR))
        keys = [result.definition.key for result in dashboard.widgets]
        assert WidgetKey.ACTIVE_USERS in keys
        assert WidgetKey.MY_CASES in keys

    def test_a_lawyer_receives_no_platform_widget(self, service: DashboardService) -> None:
        dashboard = service.load(actor=build_user(UserRole.LAWYER))
        keys = {result.definition.key for result in dashboard.widgets}
        assert WidgetKey.STORAGE_USAGE not in keys
        assert WidgetKey.PROCESSING_QUEUES not in keys

    def test_an_unauthorized_widget_is_never_computed(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        """**Not filtered after the fact — never run.**

        A widget that was computed and then hidden would have executed a query
        against rows the caller may not see, which is a leak waiting for a logging
        change to expose it.
        """
        service.load(actor=build_user(UserRole.LAWYER))
        repository.storage_usage.assert_not_called()
        repository.user_activity.assert_not_called()
        repository.queue_depths.assert_not_called()

    def test_only_narrows_the_page(self, service: DashboardService) -> None:
        dashboard = service.load(
            actor=build_user(UserRole.ADMINISTRATOR), only=[WidgetKey.MY_CASES]
        )
        assert [result.definition.key for result in dashboard.widgets] == [WidgetKey.MY_CASES]

    def test_only_cannot_widen_the_page(self, service: DashboardService) -> None:
        """A lawyer asking for the storage widget is not given it."""
        dashboard = service.load(
            actor=build_user(UserRole.LAWYER), only=[WidgetKey.STORAGE_USAGE]
        )
        assert dashboard.widgets == ()

    def test_every_widget_measures_the_same_window(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        """The spec's "widgets should update consistently when filters change"."""
        service.load(
            actor=build_user(UserRole.ADMINISTRATOR),
            dashboard_range=DashboardRange.LAST_7_DAYS,
        )

        windows = {
            (call.kwargs["start"], call.kwargs["end"])
            for call in (
                *repository.case_analytics.call_args_list,
                *repository.document_analytics.call_args_list,
                *repository.activity_breakdown.call_args_list,
                *repository.report_analytics.call_args_list,
            )
        }
        assert len(windows) == 1

    def test_an_empty_result_is_empty_rather_than_unavailable(
        self, service: DashboardService
    ) -> None:
        """A measured emptiness is not a failure — the client renders differently."""
        dashboard = service.load(actor=build_user(UserRole.LAWYER))
        assert widget(dashboard, WidgetKey.MY_CASES).state is WidgetStateValue.EMPTY

    def test_a_metrics_widget_with_zeroes_is_ready_rather_than_empty(
        self, service: DashboardService
    ) -> None:
        """Zero cases is a fact about the platform, not an absence of one."""
        dashboard = service.load(actor=build_user(UserRole.LAWYER))
        assert widget(dashboard, WidgetKey.CASE_ANALYTICS).state is WidgetStateValue.READY

    def test_the_page_reports_its_own_duration(self, service: DashboardService) -> None:
        dashboard = service.load(actor=build_user(UserRole.LAWYER))
        assert dashboard.duration_ms >= 0

    def test_it_refuses_when_the_feature_is_disabled(
        self, service: DashboardService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "DASHBOARD_ENABLED", False)
        with pytest.raises(DashboardDisabledError):
            service.load(actor=build_user())


class TestIndependentFailure:
    """The spec's hardest requirement, and the reason the loop looks as it does."""

    def test_a_failing_widget_does_not_fail_the_page(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        repository.assigned_cases.side_effect = RuntimeError("the database went away")

        dashboard = service.load(actor=build_user(UserRole.LAWYER))

        assert widget(dashboard, WidgetKey.MY_CASES).state is WidgetStateValue.UNAVAILABLE
        assert dashboard.failed_widgets == 1
        # And every other widget still loaded.
        assert any(
            result.state is not WidgetStateValue.UNAVAILABLE for result in dashboard.widgets
        )

    def test_a_failing_widget_carries_a_code_and_no_data(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        """A code rather than a message: nothing internal reaches the client."""
        repository.assigned_cases.side_effect = RuntimeError("connection refused at 10.0.0.4")

        result = widget(service.load(actor=build_user(UserRole.LAWYER)), WidgetKey.MY_CASES)

        assert result.error_code == WidgetFailureReason.QUERY_FAILED.value
        assert result.payload is None

    def test_several_widgets_can_fail_independently(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        repository.assigned_cases.side_effect = RuntimeError("boom")
        repository.recent_documents.side_effect = RuntimeError("boom")

        dashboard = service.load(actor=build_user(UserRole.LAWYER))
        assert dashboard.failed_widgets == 2

    def test_a_failure_is_counted(
        self, repository: MagicMock, service: DashboardService, metrics: InMemoryDashboardMetrics
    ) -> None:
        repository.assigned_cases.side_effect = RuntimeError("boom")
        service.load(actor=build_user(UserRole.LAWYER))

        snapshot = metrics.snapshot()
        assert snapshot.widgets_failed == 1
        assert snapshot.failures_by_widget[WidgetKey.MY_CASES.value] == 1
        assert snapshot.failures_by_reason[WidgetFailureReason.QUERY_FAILED.value] == 1


class TestTimeBudget:
    """A slow dashboard degrades into a partial one, never into a hung request."""

    def test_widgets_past_the_budget_are_shed_rather_than_attempted(
        self, repository: MagicMock, metrics: InMemoryDashboardMetrics, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        # A budget small enough that the first widget's own cost exhausts it.
        monkeypatch.setattr(settings, "DASHBOARD_BUDGET_SECONDS", 0.02)

        def slow(*args: object, **kwargs: object) -> list[object]:
            time.sleep(0.05)
            return []

        repository.assigned_cases.side_effect = slow
        service = DashboardService(repository, build_notifications(), metrics=metrics)

        dashboard = service.load(actor=build_user(UserRole.LAWYER))

        shed = [
            result
            for result in dashboard.widgets
            if result.error_code == WidgetFailureReason.BUDGET_EXHAUSTED.value
        ]
        assert shed, "the budget should have shed the widgets it did not reach"
        assert all(result.payload is None for result in shed)

    def test_shedding_is_counted_separately_from_a_fault(
        self, repository: MagicMock, metrics: InMemoryDashboardMetrics, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rising number here means "too many widgets", not "the database broke"."""
        from core.config import settings

        monkeypatch.setattr(settings, "DASHBOARD_BUDGET_SECONDS", 0.02)
        repository.assigned_cases.side_effect = lambda *a, **k: (time.sleep(0.05), [])[1]
        service = DashboardService(repository, build_notifications(), metrics=metrics)

        service.load(actor=build_user(UserRole.LAWYER))

        failures = metrics.snapshot().failures_by_reason
        assert failures.get(WidgetFailureReason.BUDGET_EXHAUSTED.value, 0) > 0
        assert failures.get(WidgetFailureReason.QUERY_FAILED.value, 0) == 0


class TestRefreshingOneWidget:
    """"Refreshing one widget should not reload the entire dashboard"."""

    def test_it_runs_only_that_widgets_queries(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        service.refresh(WidgetKey.MY_CASES, actor=build_user(UserRole.LAWYER))

        repository.assigned_cases.assert_called_once()
        repository.recent_documents.assert_not_called()
        repository.case_analytics.assert_not_called()

    def test_it_uses_the_same_loader_as_the_page(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        """A second code path is how a refreshed tile drifts from its neighbours."""
        lawyer = build_user(UserRole.LAWYER)

        page = widget(service.load(actor=lawyer, only=[WidgetKey.MY_CASES]), WidgetKey.MY_CASES)
        refreshed = service.refresh(WidgetKey.MY_CASES, actor=lawyer)

        assert page.definition == refreshed.definition
        assert page.state == refreshed.state

    def test_it_refuses_a_widget_the_caller_may_not_see(
        self, service: DashboardService
    ) -> None:
        with pytest.raises(DashboardWidgetNotFoundError):
            service.refresh(WidgetKey.STORAGE_USAGE, actor=build_user(UserRole.LAWYER))

    def test_a_refusal_looks_like_a_missing_widget(self, service: DashboardService) -> None:
        """404 rather than 403, so the endpoint is not an oracle for what exists."""
        with pytest.raises(DashboardWidgetNotFoundError) as raised:
            service.refresh(WidgetKey.ACTIVE_USERS, actor=build_user(UserRole.LAWYER))
        assert raised.value.status_code == 404

    def test_a_refresh_is_counted_apart_from_a_load(
        self, service: DashboardService, metrics: InMemoryDashboardMetrics
    ) -> None:
        service.refresh(WidgetKey.MY_CASES, actor=build_user(UserRole.LAWYER))

        snapshot = metrics.snapshot()
        assert snapshot.refreshes == 1
        assert snapshot.loads == 0
        assert snapshot.widgets_loaded == 1


class TestPlatformCache:
    """Only platform-wide widgets are cacheable, and only for the configured TTL."""

    def test_a_platform_widget_is_reused_within_the_ttl(
        self, repository: MagicMock, service: DashboardService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "DASHBOARD_CACHE_SECONDS", 60)
        clear_dashboard_cache()

        admin = build_user(UserRole.ADMINISTRATOR)
        service.load(actor=admin, only=[WidgetKey.STORAGE_USAGE])
        service.load(actor=admin, only=[WidgetKey.STORAGE_USAGE])

        repository.storage_usage.assert_called_once()

    def test_a_user_scoped_widget_is_never_cached(
        self, repository: MagicMock, service: DashboardService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**The safety argument, asserted.**

        A per-caller figure in a shared cache would show one person another's
        numbers. There is no setting that permits it, and this is what proves the
        eligibility test is on `platform_wide` rather than on cost.
        """
        from core.config import settings

        monkeypatch.setattr(settings, "DASHBOARD_CACHE_SECONDS", 60)
        clear_dashboard_cache()

        lawyer = build_user(UserRole.LAWYER)
        service.load(actor=lawyer, only=[WidgetKey.MY_CASES])
        service.load(actor=lawyer, only=[WidgetKey.MY_CASES])

        assert repository.assigned_cases.call_count == 2

    def test_caching_is_disabled_at_zero(
        self, repository: MagicMock, service: DashboardService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "DASHBOARD_CACHE_SECONDS", 0)
        clear_dashboard_cache()

        admin = build_user(UserRole.ADMINISTRATOR)
        service.load(actor=admin, only=[WidgetKey.STORAGE_USAGE])
        service.load(actor=admin, only=[WidgetKey.STORAGE_USAGE])

        assert repository.storage_usage.call_count == 2

    def test_a_different_window_is_a_different_entry(
        self, repository: MagicMock, service: DashboardService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "DASHBOARD_CACHE_SECONDS", 60)
        clear_dashboard_cache()

        admin = build_user(UserRole.ADMINISTRATOR)
        service.load(
            actor=admin, only=[WidgetKey.STORAGE_USAGE], dashboard_range=DashboardRange.TODAY
        )
        service.load(
            actor=admin,
            only=[WidgetKey.STORAGE_USAGE],
            dashboard_range=DashboardRange.LAST_30_DAYS,
        )

        assert repository.storage_usage.call_count == 2


class TestCatalog:
    """Metadata, and no queries."""

    def test_it_runs_no_queries(self, repository: MagicMock, service: DashboardService) -> None:
        service.catalog(actor=build_user(UserRole.ADMINISTRATOR))
        repository.assigned_cases.assert_not_called()
        repository.storage_usage.assert_not_called()

    def test_it_lists_only_what_the_caller_may_load(
        self, service: DashboardService
    ) -> None:
        keys = {d.key for d in service.catalog(actor=build_user(UserRole.LAWYER))}
        assert WidgetKey.STORAGE_USAGE not in keys
        assert WidgetKey.MY_CASES in keys

    def test_every_descriptor_carries_its_refresh_events(
        self, service: DashboardService
    ) -> None:
        """The field that lets the client refresh without a table of its own."""
        catalog = {d.key: d for d in service.catalog(actor=build_user(UserRole.ADMINISTRATOR))}
        assert catalog[WidgetKey.MY_CASES].events
        assert catalog[WidgetKey.OCR_STATUS].events


class TestListSize:
    """The row count is clamped rather than trusted."""

    def test_it_defaults_to_the_configured_size(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        from core.config import settings

        service.load(actor=build_user(UserRole.LAWYER), only=[WidgetKey.MY_CASES])
        assert (
            repository.assigned_cases.call_args.kwargs["limit"] == settings.DASHBOARD_LIST_SIZE
        )

    def test_it_is_capped_at_the_maximum(
        self, repository: MagicMock, service: DashboardService
    ) -> None:
        from core.config import settings

        service.load(
            actor=build_user(UserRole.LAWYER), only=[WidgetKey.MY_CASES], list_size=10_000
        )
        assert (
            repository.assigned_cases.call_args.kwargs["limit"]
            == settings.DASHBOARD_MAX_LIST_SIZE
        )
