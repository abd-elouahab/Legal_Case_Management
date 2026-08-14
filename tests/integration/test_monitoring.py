"""Integration tests for Monitoring & Observability.

The end-to-end claims ``22-monitoring.md`` makes that no unit test can check,
because each of them crosses a boundary this feature exists to attach to without
coupling:

* **authorization** — every monitoring endpoint is administrator-only, which is
  the spec's *"regular users must never access monitoring endpoints or operational
  metrics"*;
* **the middleware** — a request produces a correlation id, a trace, a log
  context, and metric series, and returns the trace identity to its caller;
* **the exception handlers** — a failed sign-in, an invalid token, and a
  permission denial become **security events** without ``AuthService`` or any
  access policy containing a line of monitoring code;
* **health and readiness** — the probes answer, report external services from
  configuration alone, and never make an optional integration the reason a
  deployment is unready;
* **graceful degradation** — the platform keeps serving with monitoring switched
  off, which is the requirement that would be invisible if it broke.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.tracing import TRACEPARENT_HEADER, parse_traceparent
from models.user import UserRole
from services.error_tracker import reset_error_tracker
from services.metrics_registry import reset_metrics_registry
from services.security_monitor import reset_security_monitor
from services.tracer import reset_tracer

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
MONITORING_URL = f"{settings.API_V1_PREFIX}/monitoring"
OVERVIEW_URL = f"{MONITORING_URL}/overview"
HEALTH_URL = f"{MONITORING_URL}/health"
METRICS_URL = f"{MONITORING_URL}/metrics"
EXPORT_URL = f"{MONITORING_URL}/export"
PERFORMANCE_URL = f"{MONITORING_URL}/performance"
JOBS_URL = f"{MONITORING_URL}/jobs"
ERRORS_URL = f"{MONITORING_URL}/errors"
SECURITY_URL = f"{MONITORING_URL}/security"
TRACES_URL = f"{MONITORING_URL}/traces"
ALERTS_URL = f"{MONITORING_URL}/alerts"

EVERY_ENDPOINT = (
    OVERVIEW_URL,
    HEALTH_URL,
    METRICS_URL,
    PERFORMANCE_URL,
    JOBS_URL,
    ERRORS_URL,
    SECURITY_URL,
    TRACES_URL,
    ALERTS_URL,
)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


@pytest.fixture(autouse=True)
def _clean_recorders() -> Any:
    """Start every test from an empty window.

    The recorders are process-wide by design — they measure the *process* — so a
    test that asserted on a count would otherwise be reading whatever the tests
    before it happened to do.
    """
    reset_metrics_registry()
    reset_tracer()
    reset_error_tracker()
    reset_security_monitor()
    yield
    reset_metrics_registry()
    reset_tracer()
    reset_error_tracker()
    reset_security_monitor()


@pytest.fixture
def admin_user(make_user: Any) -> Any:
    return make_user(email="monitoring-admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer_user(make_user: Any) -> Any:
    return make_user(email="monitoring-lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def court_user(make_user: Any) -> Any:
    return make_user(email="monitoring-court@example.com", password=PASSWORD, role=UserRole.COURT_REPRESENTATIVE)


class TestAuthorization:
    """*"Regular users must never access monitoring endpoints."*"""

    @pytest.mark.parametrize("url", EVERY_ENDPOINT)
    def test_an_anonymous_caller_is_refused(self, api_client: TestClient, url: str) -> None:
        assert api_client.get(url).status_code == 401

    @pytest.mark.parametrize("url", EVERY_ENDPOINT)
    def test_a_lawyer_is_refused(self, api_client: TestClient, lawyer_user: Any, url: str) -> None:
        response = api_client.get(url, headers=bearer(token_for(api_client, lawyer_user.email)))
        assert response.status_code == 403

    @pytest.mark.parametrize("url", EVERY_ENDPOINT)
    def test_a_court_representative_is_refused(
        self, api_client: TestClient, court_user: Any, url: str
    ) -> None:
        response = api_client.get(url, headers=bearer(token_for(api_client, court_user.email)))
        assert response.status_code == 403

    @pytest.mark.parametrize("url", EVERY_ENDPOINT)
    def test_an_administrator_may_read(self, api_client: TestClient, admin_user: Any, url: str) -> None:
        response = api_client.get(url, headers=bearer(token_for(api_client, admin_user.email)))
        assert response.status_code == 200, response.text

    def test_a_denial_does_not_name_the_required_permission(
        self, api_client: TestClient, lawyer_user: Any
    ) -> None:
        """A 403 body never hands out a map of the platform's capability model."""
        response = api_client.get(OVERVIEW_URL, headers=bearer(token_for(api_client, lawyer_user.email)))

        assert "monitoring" not in response.text.lower()


class TestRequestObservation:
    """What the middleware adds to every request."""

    def test_every_response_carries_a_correlation_id(self, api_client: TestClient) -> None:
        response = api_client.get("/health")

        assert response.headers.get("X-Request-ID")

    def test_a_supplied_correlation_id_is_echoed_back(self, api_client: TestClient) -> None:
        response = api_client.get("/health", headers={"X-Request-ID": "abc-123"})

        assert response.headers["X-Request-ID"] == "abc-123"

    def test_a_hostile_correlation_id_is_sanitised_before_it_is_echoed(
        self, api_client: TestClient
    ) -> None:
        """It reaches a response header and every log line for the request."""
        response = api_client.get("/health", headers={"X-Request-ID": "a" * 500 + " evil\r\nX: y"})

        echoed = response.headers["X-Request-ID"]
        assert len(echoed) <= 64
        assert "\n" not in echoed and " " not in echoed

    def test_the_response_returns_the_trace_identity(self, api_client: TestClient) -> None:
        response = api_client.get("/health")

        traceparent = response.headers.get(TRACEPARENT_HEADER)
        assert traceparent is not None
        assert parse_traceparent(traceparent) is not None

    def test_an_inbound_traceparent_is_joined_rather_than_replaced(
        self, api_client: TestClient
    ) -> None:
        """This is what "prepare for distributed deployments" means in practice."""
        inbound = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        response = api_client.get("/health", headers={TRACEPARENT_HEADER: inbound})

        returned = parse_traceparent(response.headers[TRACEPARENT_HEADER])
        assert returned is not None
        assert returned.trace_id == "0af7651916cd43dd8448eb211c80319c"

    def test_a_malformed_traceparent_does_not_fail_the_request(self, api_client: TestClient) -> None:
        response = api_client.get("/health", headers={TRACEPARENT_HEADER: "nonsense"})

        assert response.status_code == 200

    def test_requests_are_counted_by_route_template_never_by_path(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        """One series per route, not one per case."""
        token = token_for(api_client, admin_user.email)
        api_client.get(JOBS_URL, headers=bearer(token))

        response = api_client.get(METRICS_URL, headers=bearer(token))
        routes = {
            series["labels"].get("route")
            for series in response.json()["series"]
            if series["name"] == "http_requests_total"
        }
        assert f"{settings.API_V1_PREFIX}/monitoring/jobs" in routes

    def test_latency_is_recorded(self, api_client: TestClient, admin_user: Any) -> None:
        token = token_for(api_client, admin_user.email)
        api_client.get(JOBS_URL, headers=bearer(token))

        response = api_client.get(PERFORMANCE_URL, headers=bearer(token))
        body = response.json()
        assert body["requests_total"] >= 1
        assert any(item["name"] == "api_response" for item in body["latencies"])


class TestSecurityMonitoring:
    """Classified in the exception handlers, so no business module changed."""

    def test_a_failed_sign_in_becomes_a_security_event(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        api_client.post(LOGIN_URL, json={"email": admin_user.email, "password": "wrong-password"})

        response = api_client.get(
            SECURITY_URL, headers=bearer(token_for(api_client, admin_user.email))
        )
        assert response.json()["events_by_type"].get("login_failed", 0) >= 1

    def test_a_successful_sign_in_is_recorded_as_the_denominator(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        token = token_for(api_client, admin_user.email)

        body = api_client.get(SECURITY_URL, headers=bearer(token)).json()
        assert body["login_attempts"] >= 1
        assert body["events_by_type"].get("login_succeeded", 0) >= 1

    def test_a_permission_denial_becomes_a_security_event(
        self, api_client: TestClient, admin_user: Any, lawyer_user: Any
    ) -> None:
        api_client.get(OVERVIEW_URL, headers=bearer(token_for(api_client, lawyer_user.email)))

        body = api_client.get(
            SECURITY_URL, headers=bearer(token_for(api_client, admin_user.email))
        ).json()
        assert body["events_by_type"].get("permission_denied", 0) >= 1

    def test_an_invalid_token_becomes_a_security_event(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        api_client.get(JOBS_URL, headers=bearer("not-a-real-token"))

        body = api_client.get(
            SECURITY_URL, headers=bearer(token_for(api_client, admin_user.email))
        ).json()
        assert body["events_by_type"].get("token_invalid", 0) >= 1

    def test_the_feed_names_nobody(self, api_client: TestClient, admin_user: Any) -> None:
        """No account, no address, no credential — see `services/security_monitor.py`."""
        api_client.post(LOGIN_URL, json={"email": admin_user.email, "password": "wrong-password"})

        body = api_client.get(
            SECURITY_URL, headers=bearer(token_for(api_client, admin_user.email))
        ).json()
        rendered = str(body["recent"])
        assert admin_user.email not in rendered
        assert "wrong-password" not in rendered


class TestHealth:
    """Two probes for an orchestrator, one report for a person."""

    def test_liveness_answers_without_touching_a_dependency(self, api_client: TestClient) -> None:
        assert api_client.get("/health").json() == {"status": "ok"}

    def test_readiness_reports_external_services_from_configuration(
        self, api_client: TestClient
    ) -> None:
        body = api_client.get("/ready").json()

        assert set(body["external_services"]) >= {"llm", "email", "whatsapp"}
        for entry in body["external_services"].values():
            assert set(entry) == {"enabled", "configured"}

    def test_an_unconfigured_integration_never_makes_a_deployment_unready(
        self, api_client: TestClient
    ) -> None:
        """Answering 503 for it would take a working deployment out of rotation."""
        response = api_client.get("/ready")

        assert response.status_code in (200, 503)
        if response.status_code == 200:
            assert response.json()["status"] == "ok"

    def test_the_operator_report_carries_detail_the_probe_does_not(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        body = api_client.get(HEALTH_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        assert body["system"]["version"] == settings.VERSION
        assert body["system"]["uptime_seconds"] >= 0
        assert {check["name"] for check in body["dependencies"]} >= {"postgres", "redis"}
        assert any(check["required"] for check in body["dependencies"])
        assert [pool["name"] for pool in body["workers"]]

    def test_it_answers_200_even_when_something_is_unhealthy(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        """A page that goes blank when the thing it describes breaks is useless."""
        response = api_client.get(HEALTH_URL, headers=bearer(token_for(api_client, admin_user.email)))

        assert response.status_code == 200
        assert response.json()["state"] in {"healthy", "degraded", "unhealthy", "unknown"}


class TestBackgroundJobs:
    """Depth from persisted rows, liveness from the process."""

    def test_it_reports_every_queue_and_pool(self, api_client: TestClient, admin_user: Any) -> None:
        body = api_client.get(JOBS_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        assert {queue["name"] for queue in body["queues"]} >= {"ocr", "indexing", "reports"}
        assert {pool["name"] for pool in body["workers"]} >= {"ocr", "indexing", "notifications"}

    def test_a_switched_off_channel_is_disabled_rather_than_unhealthy(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        """A deployment without WhatsApp must not look broken."""
        body = api_client.get(JOBS_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        for pool in body["workers"]:
            if not pool["running"]:
                assert pool["state"] in {"disabled", "unhealthy", "unknown"}


class TestMetricsExport:
    """A renderer over the same snapshot the JSON endpoint serves."""

    def test_it_serves_the_prometheus_text_format(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        response = api_client.get(EXPORT_URL, headers=bearer(token_for(api_client, admin_user.email)))

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "# TYPE" in response.text
        assert response.text.endswith("\n")

    def test_every_series_carries_the_configured_prefix(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        response = api_client.get(EXPORT_URL, headers=bearer(token_for(api_client, admin_user.email)))

        for line in response.text.splitlines():
            if line and not line.startswith("#"):
                assert line.startswith(settings.MONITORING_PROMETHEUS_PREFIX)

    def test_the_eleven_feature_recorders_are_bridged_rather_than_duplicated(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        """Those recorders stay the source of truth; this reads them."""
        body = api_client.get(METRICS_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        assert "features" in body
        assert isinstance(body["features"], dict)


class TestTracing:
    """A request end to end."""

    def test_a_request_produces_a_trace_with_its_route_on_it(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        token = token_for(api_client, admin_user.email)
        api_client.get(JOBS_URL, headers=bearer(token))

        body = api_client.get(TRACES_URL, headers=bearer(token)).json()
        assert body["traces_recorded"] >= 1
        assert any("/monitoring/jobs" in trace["name"] for trace in body["traces"])


class TestAlerts:
    """Evaluated, never delivered."""

    def test_every_declared_rule_is_reported(self, api_client: TestClient, admin_user: Any) -> None:
        body = api_client.get(ALERTS_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        keys = {alert["key"] for alert in body["alerts"]}
        assert {"error_rate_high", "latency_high", "queue_backlog"} <= keys

    def test_a_rate_alert_does_not_fire_on_a_handful_of_requests(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        """Three requests of which one failed is a 33 % error rate and means nothing."""
        body = api_client.get(ALERTS_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        error_rate = next(alert for alert in body["alerts"] if alert["key"] == "error_rate_high")
        assert error_rate["firing"] is False
        assert error_rate["threshold"] == settings.MONITORING_ERROR_RATE_THRESHOLD

    def test_a_firing_rule_reports_the_measurement_that_decided_it(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        body = api_client.get(ALERTS_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        for alert in body["alerts"]:
            assert alert["summary"]
            assert "severity" in alert


class TestGracefulDegradation:
    """*"Monitoring must never become a dependency of the application."*"""

    def test_the_platform_serves_with_monitoring_switched_off(
        self, api_client: TestClient, admin_user: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "MONITORING_ENABLED", False)

        assert api_client.get("/health").status_code == 200
        assert api_client.post(
            LOGIN_URL, json={"email": admin_user.email, "password": PASSWORD}
        ).status_code == 200

    def test_the_endpoints_still_answer_with_the_recorders_switched_off(
        self, api_client: TestClient, admin_user: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """They report an empty window rather than failing."""
        token = token_for(api_client, admin_user.email)
        monkeypatch.setattr(settings, "MONITORING_METRICS_ENABLED", False)
        monkeypatch.setattr(settings, "MONITORING_TRACING_ENABLED", False)

        assert api_client.get(METRICS_URL, headers=bearer(token)).status_code == 200
        assert api_client.get(TRACES_URL, headers=bearer(token)).status_code == 200

    def test_the_exporter_is_absent_rather_than_refused_when_disabled(
        self, api_client: TestClient, admin_user: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """403 would tell a scraper to retry with better credentials."""
        token = token_for(api_client, admin_user.email)
        monkeypatch.setattr(settings, "MONITORING_PROMETHEUS_ENABLED", False)

        assert api_client.get(EXPORT_URL, headers=bearer(token)).status_code == 404


class TestOverview:
    """One page, assembled from every section, each failing alone."""

    def test_it_carries_every_section(self, api_client: TestClient, admin_user: Any) -> None:
        body = api_client.get(OVERVIEW_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        assert set(body) >= {
            "generated_at",
            "state",
            "health",
            "performance",
            "jobs",
            "errors",
            "security",
            "traces",
            "alerts",
            "unavailable",
        }

    def test_a_healthy_read_reports_nothing_unavailable(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        body = api_client.get(OVERVIEW_URL, headers=bearer(token_for(api_client, admin_user.email))).json()

        assert body["unavailable"] == []

    def test_it_agrees_with_the_narrow_endpoints(
        self, api_client: TestClient, admin_user: Any
    ) -> None:
        """The aggregate loops over the very loaders they call."""
        token = token_for(api_client, admin_user.email)
        overview = api_client.get(OVERVIEW_URL, headers=bearer(token)).json()
        jobs = api_client.get(JOBS_URL, headers=bearer(token)).json()

        assert {queue["name"] for queue in overview["jobs"]["queues"]} == {
            queue["name"] for queue in jobs["queues"]
        }
