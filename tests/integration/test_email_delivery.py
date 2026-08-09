"""Integration tests for the Email Delivery Channel.

Four things end to end, against the real application:

* **the delivery path** — a real business action publishes a real domain event,
  the Notification Service creates a notification from it, and the email channel
  queues, renders, and sends one. No step is mocked between the event and the
  composed message except the provider itself;
* **the boundary** — that this channel delivers *notifications* rather than
  events, and only the ones the spec marks for email. A document upload notifies
  and does not mail;
* **preferences** — that the email channel can be silenced through the API
  without emptying somebody's in-app feed, which is the setting the spec's User
  Preferences section is actually about;
* **monitoring and authorization** — that the metrics view reports what the spec
  names, and that it is administrative.

The queue is inline and the provider records rather than sends (see the
``email_queue`` and ``email_provider`` fixtures), so "did assigning a case mail
the lawyer?" is an assertion rather than a wait. Everything else — routers,
schemas, services, repositories, **the real templates** — is the application's
own.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.config import settings
from core.events import DomainEvent, DomainEventType, case_topic, user_topic
from models.email import EmailDeliveryStatus
from models.user import UserRole

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
NOTIFICATIONS_URL = f"{settings.API_V1_PREFIX}/notifications"
PREFERENCES_URL = f"{NOTIFICATIONS_URL}/preferences"
EMAIL_METRICS_URL = f"{NOTIFICATIONS_URL}/email/metrics"
ANNOUNCEMENTS_URL = f"{NOTIFICATIONS_URL}/announcements"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


@pytest.fixture(autouse=True)
def _enable_email(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Email is **off by default** on this platform.

    Unlike every other feature switch here, and deliberately: an unconfigured OCR
    engine records a failure nobody outside the platform sees, while an
    unconfigured mail relay is the platform's only outward-facing side effect. The
    tests turn it on explicitly, which is also what a deployment does.

    ``api_client`` is requested — and otherwise unused — to force the application
    to start **before** the switch is flipped. The lifespan's
    ``start_email_workers`` runs the retry sweeper's startup pass, which is a
    query, and letting it fire here would have it reach for the real PostgreSQL
    rather than the test database. It is caught and logged either way; this keeps
    it from happening at all.
    """
    monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "EMAIL_FROM_ADDRESS", "notifications@legal.example")
    monkeypatch.setattr(settings, "EMAIL_BASE_URL", "https://legal.example")


@pytest.fixture
def admin_user(make_user: Any) -> Any:
    return make_user(
        email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR
    )


@pytest.fixture
def lawyer_user(make_user: Any) -> Any:
    return make_user(email="lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def admin_headers(api_client: TestClient, admin_user: Any) -> dict[str, str]:
    return bearer(token_for(api_client, admin_user.email))


@pytest.fixture
def lawyer_headers(api_client: TestClient, lawyer_user: Any) -> dict[str, str]:
    return bearer(token_for(api_client, lawyer_user.email))


def deliveries(db_session: Session) -> list[Any]:
    from models.email import EmailDelivery

    return list(db_session.query(EmailDelivery).all())


def assignment_event(*, case_id: uuid.UUID, assignee_id: uuid.UUID, actor_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """The event `services/case.py` publishes when a case is assigned."""
    return DomainEvent.create(
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


# --------------------------------------------------------------------------- #
# The delivery path
# --------------------------------------------------------------------------- #


class TestDeliveryPath:
    def test_an_assignment_event_produces_one_email(
        self,
        notification_subscriber: Any,
        email_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
    ) -> None:
        """The whole flow the spec draws: domain event → Notification Service →
        notification created → marked for email → rendered → provider."""
        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )

        created = notification_subscriber.process(
            assignment_event(
                case_id=legal_case.id,
                assignee_id=lawyer_user.id,
                actor_id=admin_user.id,
            )
        )

        assert created == 1
        rows = deliveries(db_session)
        assert len(rows) == 1
        assert rows[0].status is EmailDeliveryStatus.SENT
        assert rows[0].recipient_email == lawyer_user.email
        assert len(email_provider.sent) == 1

    def test_the_message_carries_the_notification_wording_in_both_bodies(
        self,
        notification_subscriber: Any,
        email_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        """Composed from `core/notifications.py` rather than restated — which is
        `code-standards.md`'s "notification logic must never be duplicated" across
        channels."""
        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        notification_subscriber.process(
            assignment_event(
                case_id=legal_case.id,
                assignee_id=lawyer_user.id,
                actor_id=admin_user.id,
            )
        )

        message = email_provider.sent[0]
        assert message.subject == "Dossier attribué"
        assert "CASE-2026-0001" in message.text_body
        assert "CASE-2026-0001" in message.html_body
        assert message.html_body.startswith("<html")
        assert message.to_address == lawyer_user.email
        assert message.from_address == "notifications@legal.example"

    def test_the_link_points_at_the_case(
        self,
        notification_subscriber: Any,
        email_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        notification_subscriber.process(
            assignment_event(
                case_id=legal_case.id,
                assignee_id=lawyer_user.id,
                actor_id=admin_user.id,
            )
        )
        assert (
            f"https://legal.example/cases/{legal_case.id}"
            in email_provider.sent[0].html_body
        )

    def test_a_password_reset_is_mailed_without_a_link(
        self,
        notification_subscriber: Any,
        email_provider: Any,
        lawyer_user: Any,
        db_session: Session,
    ) -> None:
        """One of the spec's Authentication email types, and the reason the
        security template exists: a message about a password that asks the reader
        to click something is the shape of a phishing email."""
        notification_subscriber.process(
            DomainEvent.create(
                event_type=DomainEventType.USER_PASSWORD_RESET,
                topic=user_topic(lawyer_user.id),
                sequence=1,
            )
        )

        rows = deliveries(db_session)
        assert len(rows) == 1
        assert rows[0].template == "security"
        assert "<a " not in email_provider.sent[0].html_body

    def test_an_excluded_event_notifies_without_mailing(
        self,
        notification_subscriber: Any,
        email_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
    ) -> None:
        """The spec's "Events That Must NOT Generate Emails": a document upload is
        an in-app notification and nothing more, and this channel cannot override
        that because it can only narrow."""
        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        created = notification_subscriber.process(
            DomainEvent.create(
                event_type=DomainEventType.DOCUMENT_UPLOADED,
                topic=case_topic(legal_case.id),
                sequence=1,
                case_id=legal_case.id,
                actor_id=admin_user.id,
                payload={"case_number": "CASE-2026-0001"},
            )
        )

        assert created == 1  # the notification exists
        assert deliveries(db_session) == []  # and no email was queued
        assert email_provider.sent == []

    def test_a_disabled_channel_delivers_nothing(
        self,
        notification_subscriber: Any,
        email_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing else changes: the notification is created and readable exactly
        as before, which is what makes `EMAIL_ENABLED` a configuration rather than
        a degradation."""
        monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )

        created = notification_subscriber.process(
            assignment_event(
                case_id=legal_case.id,
                assignee_id=lawyer_user.id,
                actor_id=admin_user.id,
            )
        )

        assert created == 1
        assert deliveries(db_session) == []


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #


class TestAnnouncements:
    def test_a_maintenance_notice_is_mailed(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        email_provider: Any,
        db_session: Session,
    ) -> None:
        """The spec's "Critical System Announcement", through the one endpoint
        that creates a notification from a person rather than from an event."""
        response = api_client.post(
            ANNOUNCEMENTS_URL,
            headers=admin_headers,
            json={"kind": "maintenance", "message": "Indisponible dimanche."},
        )

        assert response.status_code == 201, response.text
        rows = deliveries(db_session)
        assert {row.rule_key for row in rows} == {"system.maintenance"}
        assert any("Indisponible dimanche." in m.text_body for m in email_provider.sent)

    def test_a_routine_announcement_is_not(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        db_session: Session,
    ) -> None:
        """The spec asks for a *critical* system announcement; mailing every
        routine one to every account is how a platform's mail starts being
        filtered."""
        response = api_client.post(
            ANNOUNCEMENTS_URL,
            headers=admin_headers,
            json={"kind": "announcement", "message": "Bonne semaine."},
        )

        assert response.status_code == 201, response.text
        assert deliveries(db_session) == []

    def test_an_announcement_is_escaped_in_the_html_body(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        email_provider: Any,
    ) -> None:
        """The one place a human's words become an email, so the one place the
        escaping environment actually earns its keep."""
        api_client.post(
            ANNOUNCEMENTS_URL,
            headers=admin_headers,
            json={"kind": "maintenance", "message": "<script>alert(1)</script>"},
        )

        message = next(m for m in email_provider.sent if "alert(1)" in m.html_body)
        assert "<script>" not in message.html_body
        assert "&lt;script&gt;" in message.html_body
        # And the plain-text part is not entity-encoded, because it is text.
        assert "<script>alert(1)</script>" in message.text_body


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class TestPreferences:
    def test_the_email_channel_is_reported(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        body = api_client.get(PREFERENCES_URL, headers=lawyer_headers).json()
        entry = next(
            item
            for item in body["preferences"]
            if item["preference_key"] == "case_updates"
        )
        assert entry["email"] is True
        assert entry["in_app"] is True
        assert entry["is_default"] is True

    def test_email_can_be_silenced_without_touching_the_feed(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
    ) -> None:
        """The spec's "if a user disables email delivery for a supported
        notification type, no email should be sent" — and the in-app notification
        still arrives, which is the setting people actually want."""
        response = api_client.put(
            PREFERENCES_URL,
            headers=lawyer_headers,
            json={"preferences": [{"preference_key": "case_updates", "email": False}]},
        )
        assert response.status_code == 200, response.text
        entry = next(
            item
            for item in response.json()["preferences"]
            if item["preference_key"] == "case_updates"
        )
        assert (entry["email"], entry["in_app"]) == (False, True)

        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        created = notification_subscriber.process(
            assignment_event(
                case_id=legal_case.id,
                assignee_id=lawyer_user.id,
                actor_id=admin_user.id,
            )
        )

        assert created == 1
        assert deliveries(db_session) == []

    def test_an_old_client_payload_does_not_silence_email_by_omission(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """Every client written before this channel existed sends `in_app` alone."""
        response = api_client.put(
            PREFERENCES_URL,
            headers=lawyer_headers,
            json={"preferences": [{"preference_key": "case_updates", "in_app": False}]},
        )
        entry = next(
            item
            for item in response.json()["preferences"]
            if item["preference_key"] == "case_updates"
        )
        assert (entry["in_app"], entry["email"]) == (False, True)

    def test_a_change_naming_no_channel_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """Almost certainly a client bug, and answering 200 while doing nothing is
        the hardest kind of failure to notice from the outside."""
        response = api_client.put(
            PREFERENCES_URL,
            headers=lawyer_headers,
            json={"preferences": [{"preference_key": "case_updates"}]},
        )
        assert response.status_code == 422

    def test_preferences_are_the_callers_own(
        self, api_client: TestClient, lawyer_headers: dict[str, str], admin_user: Any
    ) -> None:
        """There is no endpoint for reading or setting anybody else's, and
        `notifications:manage` does not grant one."""
        response = api_client.put(
            PREFERENCES_URL,
            headers=lawyer_headers,
            json={
                "preferences": [
                    {
                        "preference_key": "case_updates",
                        "email": False,
                        "user_id": str(admin_user.id),
                    }
                ]
            },
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_the_view_reports_what_the_spec_names(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        notification_subscriber.process(
            assignment_event(
                case_id=legal_case.id,
                assignee_id=lawyer_user.id,
                actor_id=admin_user.id,
            )
        )

        response = api_client.get(EMAIL_METRICS_URL, headers=admin_headers)
        assert response.status_code == 200, response.text
        body = response.json()

        # Queued, sent, failed, retries, and latency — the spec's five.
        assert body["queued"] == 0  # nothing still waiting
        assert body["sent"] == 1
        assert body["failed"] == 0
        assert body["retried"] == 0
        assert body["average_delivery_latency_ms"] is not None
        # Plus what explains them.
        assert body["sent_by_rule"] == {"case.assigned": 1}
        assert body["provider_available"] is True
        assert body["templates_available"] is True
        assert body["enabled"] is True

    def test_skips_are_reported_as_skips_rather_than_failures(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        """Most of the platform's notifications are in-app only by design, and
        counting that as a failure would make a healthy deployment look broken."""
        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        notification_subscriber.process(
            DomainEvent.create(
                event_type=DomainEventType.DOCUMENT_UPLOADED,
                topic=case_topic(legal_case.id),
                sequence=1,
                case_id=legal_case.id,
                actor_id=admin_user.id,
                payload={"case_number": "CASE-2026-0001"},
            )
        )

        body = api_client.get(EMAIL_METRICS_URL, headers=admin_headers).json()
        assert body["failed"] == 0
        assert body["skipped_by_reason"]["not_email_eligible"] == 1

    def test_the_view_is_administrative(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """`notifications:monitor`, and **no new permission**: email is a delivery
        channel for notifications rather than a feature of its own."""
        assert api_client.get(EMAIL_METRICS_URL, headers=lawyer_headers).status_code == 403

    def test_the_view_requires_authentication(self, api_client: TestClient) -> None:
        assert api_client.get(EMAIL_METRICS_URL).status_code == 401

    def test_no_address_reaches_the_response(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        """Counts, durations, rules, and codes only. A per-recipient breakdown
        would be a live index of whose mailbox the platform writes to."""
        legal_case = make_case(
            created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id
        )
        notification_subscriber.process(
            assignment_event(
                case_id=legal_case.id,
                assignee_id=lawyer_user.id,
                actor_id=admin_user.id,
            )
        )

        raw = api_client.get(EMAIL_METRICS_URL, headers=admin_headers).text
        assert lawyer_user.email not in raw
        assert "CASE-2026-0001" not in raw

    def test_there_is_no_endpoint_listing_deliveries(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """Deliberate: a list of deliveries names people, rules, and moments.
        Troubleshooting a specific complaint is a query against the table under
        the database's own access controls.

        The status is a **422** rather than a 404, and that is itself informative:
        ``/notifications/email`` falls through to ``/notifications/{notification_id}``
        and fails to parse as a UUID, which is exactly what "there is no route
        here" looks like on this router.
        """
        response = api_client.get(f"{NOTIFICATIONS_URL}/email", headers=admin_headers)
        assert response.status_code == 422
