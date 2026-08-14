"""Integration tests for Localization.

The end-to-end claims ``21-localization.md`` makes that no unit test can check,
because each of them crosses a module boundary the feature exists to connect:

* a language chosen in **Settings** reaches the **notification feed**, an
  **email**, and a **WhatsApp message** — three surfaces owned by three different
  modules, none of which imports the others;
* the **catalogue** endpoint tells a client which languages exist and which one it
  is being addressed in;
* a client's **report** of a missing translation is counted and never stored;
* the **metrics** view is administrative, reports counts by language, and names
  nobody;
* and — the requirement that would be invisible if it broke —
  **authorization is unaffected by language**.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.events import DomainEvent, DomainEventType, case_topic
from core.localization import default_language
from core.notifications import NotificationCategory, render_notification
from core.whatsapp import provider_language_code
from models.user import UserRole

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
LOCALIZATION_URL = f"{settings.API_V1_PREFIX}/localization"
LANGUAGES_URL = f"{LOCALIZATION_URL}/languages"
REPORT_URL = f"{LOCALIZATION_URL}/report"
METRICS_URL = f"{LOCALIZATION_URL}/metrics"
SETTINGS_URL = f"{settings.API_V1_PREFIX}/settings/preferences"
NOTIFICATIONS_URL = f"{settings.API_V1_PREFIX}/notifications"
CASES_URL = f"{settings.API_V1_PREFIX}/cases"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


@pytest.fixture
def admin_user(make_user: Any) -> Any:
    return make_user(
        email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR
    )


@pytest.fixture
def lawyer_user(make_user: Any) -> Any:
    # With a phone number, so the WhatsApp channel has somewhere to deliver: the
    # column is optional and `no_phone_number` is the ordinary outcome on that
    # channel rather than a fault, which would make this fixture silently skip.
    return make_user(
        email="lawyer@example.com",
        password=PASSWORD,
        role=UserRole.LAWYER,
        phone="+212612345678",
    )


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


def choose_language(client: TestClient, headers: dict[str, str], language: str) -> None:
    """Store a language preference the way a person actually does: through the
    Settings API, which is the platform's **only** write path for one."""
    response = client.put(
        SETTINGS_URL,
        headers=headers,
        json={"settings": [{"setting_key": "language", "value": language}]},
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


class TestCatalogue:
    def test_it_lists_the_three_languages_with_direction_and_locale(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        body = api_client.get(LANGUAGES_URL, headers=lawyer_headers).json()

        codes = [entry["code"] for entry in body["languages"]]
        assert codes == ["en", "fr", "ar"]
        directions = {entry["code"]: entry["direction"] for entry in body["languages"]}
        assert directions["ar"] == "rtl"
        assert directions["fr"] == "ltr"

    def test_it_carries_no_language_names(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """*"An API response is a place a translation cannot live."* The client
        names each language, in its own language — which is the one string on the
        platform that must never be translated."""
        body = api_client.get(LANGUAGES_URL, headers=lawyer_headers).json()

        serialized = str(body)
        for name in ("English", "Français", "العربية", "Arabic", "French"):
            assert name not in serialized

    def test_it_reports_the_language_this_caller_is_addressed_in(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        choose_language(api_client, lawyer_headers, "ar")

        body = api_client.get(LANGUAGES_URL, headers=lawyer_headers).json()
        assert body["resolved"] == "ar"
        assert body["direction"] == "rtl"
        assert body["locale"] == "ar-MA"
        assert body["default"] == default_language()

    def test_every_role_may_read_it(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        court_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        """No capability of its own: a language selector that could not list its
        options would be a selector nobody could use, and the spec requires that
        language switching cannot affect permissions."""
        for headers in (lawyer_headers, court_headers, admin_headers):
            assert api_client.get(LANGUAGES_URL, headers=headers).status_code == 200

    def test_it_requires_a_session(self, api_client: TestClient) -> None:
        assert api_client.get(LANGUAGES_URL).status_code == 401


# --------------------------------------------------------------------------- #
# A preference reaching every surface
# --------------------------------------------------------------------------- #


class TestTheNotificationFeed:
    def test_the_feed_is_rendered_in_the_readers_own_language(
        self,
        api_client: TestClient,
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        """A notification stores **no prose**, so a stored preference re-renders the
        *whole history* rather than only what arrives afterwards."""
        make_notification(
            recipient_id=lawyer_user.id,
            rule_key="case.created",
            context={"case_number": "CASE-2026-0042"},
        )
        choose_language(api_client, lawyer_headers, "ar")

        item = api_client.get(NOTIFICATIONS_URL, headers=lawyer_headers).json()["items"][0]
        assert item["language"] == "ar"
        assert item["title"] == render_notification(
            rule_key="case.created",
            category=NotificationCategory.CASE,
            context={"case_number": "CASE-2026-0042"},
            language="ar",
        ).title

    def test_an_explicit_query_still_wins(
        self,
        api_client: TestClient,
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        """A client that has *just* switched needs the feed to follow before the
        setting has round-tripped."""
        make_notification(recipient_id=lawyer_user.id)
        choose_language(api_client, lawyer_headers, "ar")

        body = api_client.get(
            NOTIFICATIONS_URL, headers=lawyer_headers, params={"language": "fr"}
        ).json()
        assert body["items"][0]["language"] == "fr"

    def test_an_unsupported_language_is_counted_rather_than_refused(
        self,
        api_client: TestClient,
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        make_notification: Any,
        localization_metrics: Any,
    ) -> None:
        """Refusing would take somebody's whole notification feed away over a
        presentation detail."""
        make_notification(recipient_id=lawyer_user.id)

        response = api_client.get(
            NOTIFICATIONS_URL, headers=lawyer_headers, params={"language": "de"}
        )

        assert response.status_code == 200
        assert response.json()["items"][0]["language"] == default_language()
        assert localization_metrics.snapshot().unsupported_locale_requests == 1


@pytest.mark.usefixtures("email_provider")
class TestOutboundChannels:
    """The two channels the spec names, and the open question this feature closed.

    ``progress-tracker.md`` recorded, when the email channel shipped, that
    *"an email is written in the deployment's language, not the reader's"* and that
    `resolve_email_language` was one injection away from the answer. These assert
    that the injection landed.
    """

    @staticmethod
    def _assign(
        notification_subscriber: Any,
        *,
        case_id: Any,
        assignee_id: Any,
        actor_id: Any,
    ) -> None:
        """The event `services/case.py` publishes when a case is assigned."""
        notification_subscriber.process(
            DomainEvent.create(
                event_type=DomainEventType.CASE_ASSIGNMENT_CHANGED,
                topic=case_topic(case_id),
                sequence=1,
                case_id=case_id,
                actor_id=actor_id,
                payload={
                    "case_number": "CASE-2026-0001",
                    "assignee_id": assignee_id,
                    "assigned": True,
                },
            )
        )

    def test_an_email_is_written_in_the_recipients_language(
        self,
        api_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        admin_user: Any,
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        make_case: Any,
        notification_subscriber: Any,
        email_provider: Any,
    ) -> None:
        monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
        choose_language(api_client, lawyer_headers, "ar")

        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        self._assign(
            notification_subscriber,
            case_id=legal_case.id,
            assignee_id=lawyer_user.id,
            actor_id=admin_user.id,
        )

        message = email_provider.sent[0]
        assert message.subject == render_notification(
            rule_key="case.assigned",
            category=NotificationCategory.CASE,
            language="ar",
        ).title
        # The direction travels *in* the document: a mail client has no access to
        # the application's stylesheet.
        assert 'dir="rtl"' in message.html_body

    def test_a_whatsapp_message_asks_for_the_recipients_approved_template(
        self,
        api_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        admin_user: Any,
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        make_case: Any,
        notification_subscriber: Any,
        whatsapp_provider: Any,
    ) -> None:
        """The language decides which *approved template* Meta is asked for, which
        is why it is resolved before the delivery row is queued rather than at send
        time."""
        monkeypatch.setattr(settings, "WHATSAPP_ENABLED", True)
        choose_language(api_client, lawyer_headers, "ar")

        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        self._assign(
            notification_subscriber,
            case_id=legal_case.id,
            assignee_id=lawyer_user.id,
            actor_id=admin_user.id,
        )

        message = whatsapp_provider.sent[0]
        assert message.language_code == provider_language_code("ar")
        assert message.parameters[1] == render_notification(
            rule_key="case.assigned",
            category=NotificationCategory.CASE,
            language="ar",
        ).title


# --------------------------------------------------------------------------- #
# Reporting and monitoring
# --------------------------------------------------------------------------- #


class TestClientReports:
    def test_a_missing_key_is_counted(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        localization_metrics: Any,
    ) -> None:
        response = api_client.post(
            REPORT_URL,
            headers=lawyer_headers,
            json={"missing_keys": ["cases.filters.status"], "language": "ar"},
        )

        assert response.status_code == 204
        snapshot = localization_metrics.snapshot()
        assert snapshot.missing_translations == 1
        assert snapshot.missing_keys == ("cases.filters.status",)

    def test_a_load_failure_is_counted_by_cause(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        localization_metrics: Any,
    ) -> None:
        api_client.post(
            REPORT_URL,
            headers=lawyer_headers,
            json={"failures": ["load_failed"], "catalogue": "ar"},
        )

        snapshot = localization_metrics.snapshot()
        assert snapshot.translation_failures == 1
        assert snapshot.failures_by_reason == {"load_failed": 1}
        assert snapshot.failing_catalogues == ("ar",)

    def test_a_sentence_is_discarded_rather_than_recorded(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        localization_metrics: Any,
    ) -> None:
        """The one thing this endpoint must never accept: the *text* a key would
        have rendered to, which may name a case, a court, or a person."""
        api_client.post(
            REPORT_URL,
            headers=lawyer_headers,
            json={"missing_keys": ["Hearing for CASE-2026-0001 was moved"]},
        )

        assert localization_metrics.snapshot().missing_translations == 0

    def test_an_empty_report_is_accepted_and_counts_nothing(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        localization_metrics: Any,
    ) -> None:
        """So a client can send one shape unconditionally at the end of a render
        pass without branching."""
        assert (
            api_client.post(REPORT_URL, headers=lawyer_headers, json={}).status_code
            == 204
        )
        assert localization_metrics.snapshot().missing_translations == 0

    def test_reporting_can_be_switched_off_without_becoming_an_error(
        self,
        api_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        lawyer_headers: dict[str, str],
        localization_metrics: Any,
    ) -> None:
        """A deployment that does not want browser reports gets a 204; refusing
        would put an error in every console on a working platform."""
        monkeypatch.setattr(settings, "LOCALIZATION_REPORTING_ENABLED", False)

        response = api_client.post(
            REPORT_URL, headers=lawyer_headers, json={"missing_keys": ["a.b"]}
        )

        assert response.status_code == 204
        assert localization_metrics.snapshot().missing_translations == 0


class TestMonitoring:
    def test_it_is_administrative(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        court_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        assert api_client.get(METRICS_URL, headers=lawyer_headers).status_code == 403
        assert api_client.get(METRICS_URL, headers=court_headers).status_code == 403
        assert api_client.get(METRICS_URL, headers=admin_headers).status_code == 200

    def test_the_distribution_reports_every_language_including_the_empty_ones(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        """A breakdown that omitted Arabic until somebody switched to it would hide
        exactly the figure a deployment deciding whether to invest in Arabic
        needs."""
        choose_language(api_client, lawyer_headers, "ar")

        body = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert body["distribution"]["ar"] == 1
        assert body["distribution"]["fr"] == 0
        assert body["distribution"]["en"] == 0
        assert body["accounts_following_default"] >= 1
        assert body["supported_languages"] == ["en", "fr", "ar"]

    def test_it_names_nobody(
        self,
        api_client: TestClient,
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        """A per-account breakdown of a language preference would be a live index of
        who is who, which is the same thing `notifications:monitor` refuses to
        build."""
        choose_language(api_client, lawyer_headers, "ar")

        serialized = api_client.get(METRICS_URL, headers=admin_headers).text

        assert str(lawyer_user.id) not in serialized
        assert lawyer_user.email not in serialized


# --------------------------------------------------------------------------- #
# The invariant that would be invisible if it broke
# --------------------------------------------------------------------------- #


class TestLocalizationChangesNothingButWords:
    def test_switching_language_does_not_change_what_a_caller_may_reach(
        self,
        api_client: TestClient,
        admin_user: Any,
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        make_case: Any,
    ) -> None:
        """*"Localization must never affect authorization, RBAC, routing, database
        schema, business rules, or workflow execution."* A lawyer sees the cases
        they are assigned to, in every language."""
        make_case(created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id)
        make_case(created_by=admin_user.id)

        before = api_client.get(CASES_URL, headers=lawyer_headers).json()
        choose_language(api_client, lawyer_headers, "ar")
        after = api_client.get(CASES_URL, headers=lawyer_headers).json()

        assert len(before["items"]) == len(after["items"]) == 1
        assert [item["id"] for item in before["items"]] == [
            item["id"] for item in after["items"]
        ]

    def test_a_language_preference_grants_no_permission(
        self, api_client: TestClient, court_headers: dict[str, str]
    ) -> None:
        """A court representative holds no AI capability, and choosing Arabic must
        not be a way to acquire one."""
        choose_language(api_client, court_headers, "ar")

        response = api_client.post(
            f"{settings.API_V1_PREFIX}/rag/answer",
            headers=court_headers,
            json={"question": "ما هو مبلغ الكراء الشهري؟"},
        )
        assert response.status_code == 403

    def test_the_feature_adds_exactly_one_permission_and_it_is_a_monitor(self) -> None:
        """`21-localization.md` adds no capability anybody needs a grant for:
        reading the interface in Arabic is not a thing to be permitted, and
        choosing a language is `settings:update`."""
        from core.permissions import Permission, PermissionGroup, permissions_in_group

        assert permissions_in_group(PermissionGroup.LOCALIZATION) == frozenset(
            {Permission.LOCALIZATION_MONITOR}
        )

    def test_no_role_but_the_administrator_holds_it(self) -> None:
        from core.permissions import Permission
        from core.roles import permissions_for_role
        from models.user import UserRole as Role

        assert Permission.LOCALIZATION_MONITOR in permissions_for_role(
            Role.ADMINISTRATOR
        )
        assert Permission.LOCALIZATION_MONITOR not in permissions_for_role(Role.LAWYER)
        assert Permission.LOCALIZATION_MONITOR not in permissions_for_role(
            Role.COURT_REPRESENTATIVE
        )

    def test_there_is_no_endpoint_that_sets_a_language(self) -> None:
        """A language preference is a *setting*: a second write path for one stored
        value is how two answers to one question start to disagree."""
        from main import app

        paths = [path for path in app.openapi()["paths"] if "/localization" in path]
        assert sorted(paths) == sorted(
            [
                f"{settings.API_V1_PREFIX}/localization/languages",
                f"{settings.API_V1_PREFIX}/localization/report",
                f"{settings.API_V1_PREFIX}/localization/metrics",
            ]
        )

    def test_there_is_no_endpoint_that_serves_translations(self) -> None:
        """A catalogue is a static asset the web application ships: serving it from
        an authenticated API would put the login screen's own copy behind a
        login."""
        from main import app

        assert (
            f"{settings.API_V1_PREFIX}/localization/messages"
            not in app.openapi()["paths"]
        )

