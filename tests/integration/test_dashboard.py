"""Integration tests for the dashboard.

Four things end to end, against the real application:

* **the aggregated endpoint** — one request returning a whole page, through the
  real router, the real schemas, the real access policy, and the real SQL;
* **authorization** — that a widget counts only what its owner may read, that a
  role is never offered a widget it does not hold, and that the numbers a lawyer
  sees are their own caseload rather than the platform's;
* **the filters** — that a time window narrows every analytic in the response
  consistently;
* **independent refresh and independent failure** — that one widget can be
  re-read on its own, and that one broken widget leaves the page standing.

Nothing here is doubled except the metrics recorder: the dashboard has no
provider, no queue, and no worker, so the queries these assertions rest on are the
queries production runs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from models.case import CaseStatus
from models.user import UserRole

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
DASHBOARD_URL = f"{settings.API_V1_PREFIX}/dashboard"
WIDGETS_URL = f"{DASHBOARD_URL}/widgets"
METRICS_URL = f"{DASHBOARD_URL}/metrics"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def widget_of(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """The one widget with this key, or a failure naming what was there instead."""
    for widget in payload["widgets"]:
        if widget["widget"]["key"] == key:
            return widget
    raise AssertionError(f"{key} not on the dashboard; got {[w['widget']['key'] for w in payload['widgets']]}")


def widget_keys(payload: dict[str, Any]) -> set[str]:
    return {widget["widget"]["key"] for widget in payload["widgets"]}


def metric(widget: dict[str, Any], key: str) -> float | None:
    for entry in widget["data"]["metrics"]:
        if entry["key"] == key:
            value: float | None = entry["value"]
            return value
    raise AssertionError(f"{key} not among {[m['key'] for m in widget['data']['metrics']]}")


def bucket(widget: dict[str, Any], key: str) -> int:
    for entry in widget["data"]["buckets"]:
        if entry["key"] == key:
            count: int = entry["count"]
            return count
    raise AssertionError(f"{key} not among {[b['key'] for b in widget['data']['buckets']]}")


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin_user(make_user: Any) -> Any:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer_user(make_user: Any) -> Any:
    return make_user(email="lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: Any) -> Any:
    return make_user(email="other@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def court_user(make_user: Any) -> Any:
    return make_user(
        email="court@example.com", password=PASSWORD, role=UserRole.COURT_REPRESENTATIVE
    )


@pytest.fixture
def admin_headers(api_client: TestClient, admin_user: Any) -> dict[str, str]:
    return bearer(token_for(api_client, admin_user.email))


@pytest.fixture
def lawyer_headers(api_client: TestClient, lawyer_user: Any) -> dict[str, str]:
    return bearer(token_for(api_client, lawyer_user.email))


@pytest.fixture
def court_headers(api_client: TestClient, court_user: Any) -> dict[str, str]:
    return bearer(token_for(api_client, court_user.email))


# --------------------------------------------------------------------------- #
# The aggregated endpoint
# --------------------------------------------------------------------------- #


class TestDashboardLoads:
    """One request, one page."""

    def test_it_requires_authentication(self, api_client: TestClient) -> None:
        assert api_client.get(DASHBOARD_URL).status_code == 401

    def test_it_returns_a_page_for_every_role(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_headers: dict[str, str],
        court_headers: dict[str, str],
    ) -> None:
        """No capability gates the dashboard itself — every role has one."""
        for headers in (admin_headers, lawyer_headers, court_headers):
            response = api_client.get(DASHBOARD_URL, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["widgets"]

    def test_the_response_carries_the_resolved_window(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert payload["range"] == "last_30_days"
        assert payload["window_days"] == 30
        assert payload["window_start"] < payload["window_end"]

    def test_every_widget_carries_its_own_metadata(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """A client renders placeholders and wires live refresh from one response."""
        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        for widget in payload["widgets"]:
            descriptor = widget["widget"]
            assert descriptor["key"]
            assert descriptor["group"]
            assert descriptor["kind"]
            assert isinstance(descriptor["refresh_events"], list)
            assert widget["state"] in {"ready", "empty", "unavailable"}

    def test_case_widgets_declare_the_case_events(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """**The field that removes the client's need for a table of its own.**"""
        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        events = widget_of(payload, "my_cases")["widget"]["refresh_events"]

        assert "case.created" in events
        assert "case.assignment_changed" in events

    def test_an_empty_platform_answers_with_zeroes_rather_than_nothing(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """The spec's "Analytics Data Integrity": measured zeroes, never estimates."""
        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        analytics = widget_of(payload, "case_analytics")
        assert analytics["state"] == "ready"
        assert metric(analytics, "total_cases") == 0
        assert metric(analytics, "active_cases") == 0

    def test_an_empty_list_widget_is_empty_rather_than_unavailable(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert widget_of(payload, "my_cases")["state"] == "empty"
        assert payload["failed_widgets"] == 0

    def test_the_quick_actions_are_on_the_envelope(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        assert "upload_document" in payload["quick_actions"]
        assert "create_case" not in payload["quick_actions"]

    def test_widgets_narrows_the_page(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            DASHBOARD_URL, headers=lawyer_headers, params={"widgets": ["my_cases"]}
        )
        assert widget_keys(response.json()) == {"my_cases"}

    def test_widgets_cannot_widen_the_page(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """A lawyer asking for the storage widget receives an empty page, not a 403."""
        response = api_client.get(
            DASHBOARD_URL, headers=lawyer_headers, params={"widgets": ["storage_usage"]}
        )
        assert response.status_code == 200
        assert response.json()["widgets"] == []

    def test_an_unknown_widget_name_is_refused_by_validation(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            DASHBOARD_URL, headers=lawyer_headers, params={"widgets": ["crystal_ball"]}
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Role-based dashboards
# --------------------------------------------------------------------------- #


class TestRoleDashboards:
    """Content depends on the role, and never exceeds what the role may read."""

    def test_an_administrator_gets_the_platform_widgets(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        keys = widget_keys(api_client.get(DASHBOARD_URL, headers=admin_headers).json())

        assert {"active_users", "storage_usage", "processing_queues"} <= keys

    def test_a_lawyer_does_not(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        keys = widget_keys(api_client.get(DASHBOARD_URL, headers=lawyer_headers).json())

        assert "active_users" not in keys
        assert "storage_usage" not in keys
        assert "processing_queues" not in keys

    def test_a_court_representative_gets_no_ai_widget(
        self, api_client: TestClient, court_headers: dict[str, str]
    ) -> None:
        """They hold no AI capability, so no AI widget survives the filter."""
        keys = widget_keys(api_client.get(DASHBOARD_URL, headers=court_headers).json())

        assert "ai_reports" not in keys
        assert "recent_conversations" not in keys
        assert "ai_analytics" not in keys

    def test_a_court_representative_leads_with_hearings(
        self, api_client: TestClient, court_headers: dict[str, str]
    ) -> None:
        """The layout is the role's, and it is an ordering rather than a filter."""
        payload = api_client.get(DASHBOARD_URL, headers=court_headers).json()
        keys = [widget["widget"]["key"] for widget in payload["widgets"]]

        # `quick_actions` is first in every layout — it is the header's shortcuts
        # rather than a section of content — so the *content* order starts after it.
        assert keys[0] == "quick_actions"
        assert keys[1] == "upcoming_hearings"

    def test_the_role_is_reported(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        assert payload["role"] == UserRole.LAWYER.value


# --------------------------------------------------------------------------- #
# Authorization of the figures themselves
# --------------------------------------------------------------------------- #


class TestScopedAnalytics:
    """**Aggregated metrics must never leak unauthorized information.**"""

    def test_a_lawyer_counts_only_their_own_cases(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        other_lawyer: Any,
        make_case: Any,
    ) -> None:
        make_case(title="Mine", assigned_lawyer_id=lawyer_user.id)
        make_case(title="Also mine", assigned_lawyer_id=lawyer_user.id)
        make_case(title="Somebody else's", assigned_lawyer_id=other_lawyer.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert metric(widget_of(payload, "case_analytics"), "total_cases") == 2

    def test_an_administrator_counts_every_case(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        other_lawyer: Any,
        make_case: Any,
    ) -> None:
        """`cases:view-all` is what lifts the restriction, and it is a capability."""
        make_case(assigned_lawyer_id=lawyer_user.id)
        make_case(assigned_lawyer_id=other_lawyer.id)
        make_case()

        payload = api_client.get(DASHBOARD_URL, headers=admin_headers).json()

        assert metric(widget_of(payload, "case_analytics"), "total_cases") == 3

    def test_the_status_breakdown_is_scoped_too(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        other_lawyer: Any,
        make_case: Any,
    ) -> None:
        make_case(status=CaseStatus.OPEN, assigned_lawyer_id=lawyer_user.id)
        make_case(status=CaseStatus.CLOSED, assigned_lawyer_id=other_lawyer.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        overview = widget_of(payload, "case_status_overview")

        assert bucket(overview, "open") == 1
        assert bucket(overview, "closed") == 0

    def test_my_cases_means_assigned_to_me_even_for_an_administrator(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        """Otherwise an administrator's "what needs my attention" is the whole platform."""
        make_case(title="Assigned to the admin", assigned_court_representative_id=admin_user.id)
        make_case(title="Somebody else's", assigned_lawyer_id=lawyer_user.id)

        payload = api_client.get(DASHBOARD_URL, headers=admin_headers).json()
        cases = widget_of(payload, "my_cases")["data"]["cases"]

        assert [item["title"] for item in cases] == ["Assigned to the admin"]

    def test_the_status_breakdown_returns_every_status_including_empty_ones(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        """A measured zero, so the chart's shape does not change as work moves."""
        make_case(status=CaseStatus.OPEN, assigned_lawyer_id=lawyer_user.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        overview = widget_of(payload, "case_status_overview")
        keys = {entry["key"] for entry in overview["data"]["buckets"]}

        assert {status.value for status in CaseStatus} <= keys

    def test_a_case_summary_carries_no_description(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        """A dashboard is an index; client-confidential prose is read where it lives."""
        make_case(
            assigned_lawyer_id=lawyer_user.id,
            description="Confidential settlement terms.",
        )

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        case = widget_of(payload, "my_cases")["data"]["cases"][0]

        assert "description" not in case
        assert "Confidential" not in str(case)


# --------------------------------------------------------------------------- #
# Court and hearings
# --------------------------------------------------------------------------- #


class TestHearings:
    """The dashboard's first question: what requires my attention?"""

    def test_upcoming_hearings_are_ordered_soonest_first(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        today = datetime.now(UTC).date()
        make_case(title="Later", next_hearing_date=today + timedelta(days=10), assigned_lawyer_id=lawyer_user.id)
        make_case(title="Sooner", next_hearing_date=today + timedelta(days=2), assigned_lawyer_id=lawyer_user.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        cases = widget_of(payload, "upcoming_hearings")["data"]["cases"]

        assert [item["title"] for item in cases] == ["Sooner", "Later"]

    def test_a_hearing_today_is_upcoming(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        """A diary that dropped it at midnight would be wrong on the day it mattered."""
        make_case(next_hearing_date=datetime.now(UTC).date(), assigned_lawyer_id=lawyer_user.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert len(widget_of(payload, "upcoming_hearings")["data"]["cases"]) == 1
        assert metric(widget_of(payload, "hearing_calendar"), "hearings_today") == 1

    def test_a_past_hearing_on_an_open_case_is_overdue(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        make_case(
            next_hearing_date=datetime.now(UTC).date() - timedelta(days=3),
            status=CaseStatus.WAITING_FOR_HEARING,
            assigned_lawyer_id=lawyer_user.id,
        )

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert metric(widget_of(payload, "hearing_calendar"), "hearings_overdue") == 1

    def test_a_past_hearing_on_a_closed_case_is_not(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        """Nothing to attend to: the matter is finished."""
        make_case(
            next_hearing_date=datetime.now(UTC).date() - timedelta(days=3),
            status=CaseStatus.CLOSED,
            assigned_lawyer_id=lawyer_user.id,
        )

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert metric(widget_of(payload, "hearing_calendar"), "hearings_overdue") == 0


# --------------------------------------------------------------------------- #
# Time filters
# --------------------------------------------------------------------------- #


class TestTimeFilters:
    """One window, applied to every analytic in the response."""

    def test_today_excludes_last_month(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        make_case(
            assigned_lawyer_id=lawyer_user.id,
            created_at=datetime.now(UTC) - timedelta(days=40),
        )
        make_case(assigned_lawyer_id=lawyer_user.id)

        payload = api_client.get(
            DASHBOARD_URL, headers=lawyer_headers, params={"range": "today"}
        ).json()

        analytics = widget_of(payload, "case_analytics")
        # The standing totals are unaffected by the window; only the windowed
        # figures move, which is the distinction the widget draws on purpose.
        assert metric(analytics, "total_cases") == 2
        assert metric(analytics, "created_in_window") == 1

    def test_a_wider_window_includes_more(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        make_case(
            assigned_lawyer_id=lawyer_user.id,
            created_at=datetime.now(UTC) - timedelta(days=10),
        )
        make_case(assigned_lawyer_id=lawyer_user.id)

        payload = api_client.get(
            DASHBOARD_URL, headers=lawyer_headers, params={"range": "last_30_days"}
        ).json()

        assert metric(widget_of(payload, "case_analytics"), "created_in_window") == 2

    def test_a_custom_range_is_honoured(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        today = date.today()
        response = api_client.get(
            DASHBOARD_URL,
            headers=lawyer_headers,
            params={
                "range": "custom",
                "start_date": (today - timedelta(days=3)).isoformat(),
                "end_date": today.isoformat(),
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["window_days"] == 4

    def test_a_custom_range_without_bounds_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            DASHBOARD_URL, headers=lawyer_headers, params={"range": "custom"}
        )
        assert response.status_code == 422

    def test_an_inverted_custom_range_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            DASHBOARD_URL,
            headers=lawyer_headers,
            params={
                "range": "custom",
                "start_date": "2026-08-10",
                "end_date": "2026-08-01",
            },
        )
        assert response.status_code == 422

    def test_an_unbounded_custom_range_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            DASHBOARD_URL,
            headers=lawyer_headers,
            params={
                "range": "custom",
                "start_date": "2000-01-01",
                "end_date": "2026-01-01",
            },
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Documents and pipelines
# --------------------------------------------------------------------------- #


class TestDocumentWidgets:
    """Document figures follow document access, which follows case access."""

    def test_recent_documents_are_scoped_to_the_callers_cases(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        other_lawyer: Any,
        make_case: Any,
        make_document: Any,
    ) -> None:
        mine = make_case(assigned_lawyer_id=lawyer_user.id)
        theirs = make_case(assigned_lawyer_id=other_lawyer.id)
        make_document(case_id=mine.id, original_filename="mine.pdf")
        make_document(case_id=theirs.id, original_filename="theirs.pdf")

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        documents = widget_of(payload, "recent_documents")["data"]["documents"]

        assert [item["original_filename"] for item in documents] == ["mine.pdf"]

    def test_the_ocr_breakdown_reports_documents_with_no_run(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        make_case: Any,
        make_document: Any,
    ) -> None:
        """`not_started` is exactly what somebody looking at this wants to find."""
        legal_case = make_case(assigned_lawyer_id=lawyer_user.id)
        make_document(case_id=legal_case.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert bucket(widget_of(payload, "ocr_status"), "not_started") == 1

    def test_the_ocr_breakdown_counts_a_completed_run(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        make_case: Any,
        make_document: Any,
        make_ocr_result: Any,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer_user.id)
        document = make_document(case_id=legal_case.id)
        make_ocr_result(document_id=document.id, document_version=document.version)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        overview = widget_of(payload, "ocr_status")

        assert bucket(overview, "completed") == 1
        assert bucket(overview, "not_started") == 0

    def test_storage_bytes_are_summed_from_real_files(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        make_case: Any,
        make_document: Any,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer_user.id)
        make_document(case_id=legal_case.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert (metric(widget_of(payload, "document_analytics"), "storage_bytes") or 0) > 0


# --------------------------------------------------------------------------- #
# Private histories
# --------------------------------------------------------------------------- #


class TestPrivateHistories:
    """Reports and conversations belong to a person, not to a case."""

    def test_the_reports_widget_shows_only_the_callers_own(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        other_lawyer: Any,
        make_case: Any,
        make_report: Any,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer_user.id)
        make_report(case=legal_case, requested_by=lawyer_user, title="Mine")
        make_report(case=legal_case, requested_by=other_lawyer, title="Theirs")

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        reports = widget_of(payload, "ai_reports")["data"]["reports"]

        assert [item["title"] for item in reports] == ["Mine"]

    def test_an_administrator_does_not_see_other_peoples_reports(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        make_case: Any,
        make_report: Any,
    ) -> None:
        """There is no `reports:view-all`, and a dashboard does not invent one."""
        legal_case = make_case(assigned_lawyer_id=lawyer_user.id)
        make_report(case=legal_case, requested_by=lawyer_user, title="Not the admin's")

        payload = api_client.get(DASHBOARD_URL, headers=admin_headers).json()

        assert widget_of(payload, "ai_reports")["data"]["reports"] == []

    def test_the_notifications_widget_shows_only_the_callers_own(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        other_lawyer: Any,
        make_notification: Any,
    ) -> None:
        make_notification(recipient_id=lawyer_user.id)
        make_notification(recipient_id=other_lawyer.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert len(widget_of(payload, "notifications")["data"]["notifications"]) == 1

    def test_notification_titles_are_rendered(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
        make_notification: Any,
    ) -> None:
        """A notification stores no prose; the widget shows it rendered, like the feed."""
        make_notification(recipient_id=lawyer_user.id)

        payload = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()
        notification = widget_of(payload, "notifications")["data"]["notifications"][0]

        assert notification["title"]
        assert notification["message"]


# --------------------------------------------------------------------------- #
# The catalog and per-widget refresh
# --------------------------------------------------------------------------- #


class TestWidgetCatalog:
    """Metadata, and exactly what the caller may refresh."""

    def test_it_lists_the_callers_widgets(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        payload = api_client.get(WIDGETS_URL, headers=lawyer_headers).json()
        keys = {widget["key"] for widget in payload["widgets"]}

        assert "my_cases" in keys
        assert "storage_usage" not in keys

    def test_it_agrees_with_the_dashboard(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """The catalog *is* the authorization answer, so it cannot differ from the page."""
        catalog = api_client.get(WIDGETS_URL, headers=lawyer_headers).json()
        page = api_client.get(DASHBOARD_URL, headers=lawyer_headers).json()

        assert {widget["key"] for widget in catalog["widgets"]} == widget_keys(page)

    def test_it_carries_the_quick_actions(
        self, api_client: TestClient, court_headers: dict[str, str]
    ) -> None:
        payload = api_client.get(WIDGETS_URL, headers=court_headers).json()
        assert set(payload["quick_actions"]) == {"upload_document", "view_calendar"}


class TestWidgetRefresh:
    """One widget, on its own."""

    def test_it_returns_one_widget(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        make_case(assigned_lawyer_id=lawyer_user.id, title="Benali v. Atlas")

        response = api_client.get(f"{WIDGETS_URL}/my_cases", headers=lawyer_headers)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["widget"]["key"] == "my_cases"
        assert payload["data"]["cases"][0]["title"] == "Benali v. Atlas"

    def test_it_honours_the_time_filter(
        self, api_client: TestClient, lawyer_headers: dict[str, str], lawyer_user: Any, make_case: Any
    ) -> None:
        """A refresh that reverted to the default window would disagree with its neighbours."""
        make_case(
            assigned_lawyer_id=lawyer_user.id,
            created_at=datetime.now(UTC) - timedelta(days=10),
        )

        response = api_client.get(
            f"{WIDGETS_URL}/case_analytics", headers=lawyer_headers, params={"range": "today"}
        )

        assert metric(response.json(), "created_in_window") == 0

    def test_an_unauthorized_widget_is_a_404(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """403 would make this endpoint an oracle for what this deployment has."""
        response = api_client.get(f"{WIDGETS_URL}/storage_usage", headers=lawyer_headers)

        assert response.status_code == 404
        assert response.json()["error"] == "dashboard_widget_not_found"

    def test_an_unknown_widget_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{WIDGETS_URL}/crystal_ball", headers=lawyer_headers)
        assert response.status_code == 422

    def test_it_requires_authentication(self, api_client: TestClient) -> None:
        assert api_client.get(f"{WIDGETS_URL}/my_cases").status_code == 401


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMonitoring:
    """An administrative view, gated like every other `*:monitor`."""

    def test_a_lawyer_may_not_read_it(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        assert api_client.get(METRICS_URL, headers=lawyer_headers).status_code == 403

    def test_a_court_representative_may_not_read_it(
        self, api_client: TestClient, court_headers: dict[str, str]
    ) -> None:
        assert api_client.get(METRICS_URL, headers=court_headers).status_code == 403

    def test_an_administrator_may(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(METRICS_URL, headers=admin_headers)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["enabled"] is True
        assert "loads" in payload
        assert "active_users" in payload

    def test_it_counts_loads_and_widgets(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        api_client.get(DASHBOARD_URL, headers=admin_headers)

        payload = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert payload["loads"] == 1
        assert payload["widgets_loaded"] > 0
        assert payload["average_load_ms"] is not None

    def test_it_counts_a_refresh_apart_from_a_load(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        api_client.get(f"{WIDGETS_URL}/my_cases", headers=admin_headers)

        payload = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert payload["refreshes"] == 1
        assert payload["loads"] == 0

    def test_it_reports_distinct_users_and_never_names_one(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_headers: dict[str, str],
        lawyer_user: Any,
    ) -> None:
        api_client.get(DASHBOARD_URL, headers=admin_headers)
        api_client.get(DASHBOARD_URL, headers=lawyer_headers)
        api_client.get(DASHBOARD_URL, headers=lawyer_headers)

        payload = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert payload["active_users"] == 2
        assert str(lawyer_user.id) not in str(payload)
        assert lawyer_user.email not in str(payload)


# --------------------------------------------------------------------------- #
# Failure and disablement
# --------------------------------------------------------------------------- #


class TestDegradation:
    """The page survives a broken widget, and the feature can be switched off."""

    def test_a_failing_widget_leaves_the_page_standing(
        self, api_client: TestClient, lawyer_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**The spec's central error-handling requirement, through the real API.**"""
        from repositories.dashboard import DashboardRepository

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(DashboardRepository, "assigned_cases", explode)

        response = api_client.get(DASHBOARD_URL, headers=lawyer_headers)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["failed_widgets"] == 1

        broken = widget_of(payload, "my_cases")
        assert broken["state"] == "unavailable"
        assert broken["error_code"] == "query_failed"
        assert broken["data"] is None

        # And the rest of the page is intact.
        assert widget_of(payload, "case_analytics")["state"] == "ready"

    def test_a_failure_never_leaks_the_internal_error(
        self, api_client: TestClient, lawyer_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from repositories.dashboard import DashboardRepository

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("connection refused to postgres at 10.0.0.4:5432")

        monkeypatch.setattr(DashboardRepository, "assigned_cases", explode)

        body = api_client.get(DASHBOARD_URL, headers=lawyer_headers).text

        assert "10.0.0.4" not in body
        assert "postgres" not in body

    def test_a_disabled_dashboard_answers_503(
        self, api_client: TestClient, lawyer_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DASHBOARD_ENABLED", False)

        response = api_client.get(DASHBOARD_URL, headers=lawyer_headers)

        assert response.status_code == 503
        assert response.json()["error"] == "dashboard_disabled"

    def test_disabling_the_dashboard_leaves_every_other_module_alone(
        self, api_client: TestClient, lawyer_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dashboard owns no data, so switching it off can only remove a page."""
        monkeypatch.setattr(settings, "DASHBOARD_ENABLED", False)

        cases = api_client.get(f"{settings.API_V1_PREFIX}/cases", headers=lawyer_headers)
        assert cases.status_code == 200
