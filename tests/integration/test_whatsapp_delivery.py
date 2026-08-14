"""Integration tests for the WhatsApp Delivery Channel.

Four things end to end, against the real application:

* **the delivery path** — a real business action publishes a real domain event,
  the Notification Service creates a notification from it, and the WhatsApp
  channel queues, renders, and sends one. No step is mocked between the event and
  the composed template message except the provider itself;
* **the boundary** — that this channel delivers *notifications* rather than
  events, and only the ones the spec marks for WhatsApp. A document upload
  notifies and does not message;
* **preferences** — that the WhatsApp channel can be silenced through the API
  **independently of the other two**, which is what the spec's User Preferences
  section is actually about;
* **monitoring and authorization** — that the metrics view reports what the spec
  names, and that it is administrative.

The queue is inline and the provider records rather than sends (see the
``whatsapp_queue`` and ``whatsapp_provider`` fixtures), so "did assigning a case
message the lawyer?" is an assertion rather than a wait. Everything else —
routers, schemas, services, repositories, **the real descriptors** — is the
application's own.

**One arrangement detail carries a real requirement.** The lawyer fixture has a
phone number and the administrator does not, which is not incidental: the
platform's phone column is optional, so an account without one is skipped forever
and correctly, and having both kinds present means every test here is also
asserting that the skip is per recipient rather than per batch.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.config import settings
from core.events import DomainEvent, DomainEventType, case_topic, user_topic
from core.localization import default_language
from core.notifications import NotificationCategory, render_notification
from core.whatsapp import provider_language_code
from models.user import UserRole
from models.whatsapp import WhatsAppDeliveryStatus

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
NOTIFICATIONS_URL = f"{settings.API_V1_PREFIX}/notifications"
PREFERENCES_URL = f"{NOTIFICATIONS_URL}/preferences"
WHATSAPP_METRICS_URL = f"{NOTIFICATIONS_URL}/whatsapp/metrics"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


@pytest.fixture(autouse=True)
def _enable_whatsapp(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """WhatsApp is **off by default** on this platform.

    Like email and unlike everything else here: an unconfigured OCR engine records
    a failure nobody outside the platform sees, while an unconfigured messaging
    channel is an outward-facing side effect that reaches a device somebody
    carries. The tests turn it on explicitly, which is also what a deployment
    does.

    ``api_client`` is requested — and otherwise unused — to force the application
    to start **before** the switch is flipped. The lifespan's
    ``start_whatsapp_workers`` runs the retry sweeper's startup pass, which is a
    query, and letting it fire here would have it reach for the real PostgreSQL
    rather than the test database.
    """
    monkeypatch.setattr(settings, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_BASE_URL", "https://legal.example")
    monkeypatch.setattr(settings, "WHATSAPP_DEFAULT_COUNTRY_CODE", None)


@pytest.fixture
def admin_user(make_user: Any) -> Any:
    """Deliberately **without** a phone number. See the module docstring."""
    return make_user(
        email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR
    )


@pytest.fixture
def lawyer_user(make_user: Any) -> Any:
    return make_user(
        email="lawyer@example.com",
        password=PASSWORD,
        role=UserRole.LAWYER,
        phone="+212612345678",
    )


@pytest.fixture
def admin_headers(api_client: TestClient, admin_user: Any) -> dict[str, str]:
    return bearer(token_for(api_client, admin_user.email))


@pytest.fixture
def lawyer_headers(api_client: TestClient, lawyer_user: Any) -> dict[str, str]:
    return bearer(token_for(api_client, lawyer_user.email))


def deliveries(db_session: Session) -> list[Any]:
    from models.whatsapp import WhatsAppDelivery

    return list(db_session.query(WhatsAppDelivery).all())


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
    def test_an_assignment_event_produces_one_message(
        self,
        notification_subscriber: Any,
        whatsapp_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
    ) -> None:
        """The whole flow the spec draws: domain event → Notification Service →
        notification created → marked for WhatsApp → rendered → provider."""
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
        assert rows[0].status is WhatsAppDeliveryStatus.DELIVERED
        assert rows[0].recipient_phone == "212612345678"
        assert rows[0].provider_message_id is not None
        assert len(whatsapp_provider.sent) == 1

    def test_the_message_carries_the_notification_wording_as_parameters(
        self,
        notification_subscriber: Any,
        whatsapp_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        """Composed from `core/notifications.py` rather than restated — and
        rather than held in an approved template in Meta's console, which is the
        one place this channel's wording could have escaped review."""
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

        message = whatsapp_provider.sent[0]
        assert message.to_number == "212612345678"
        assert message.template_name == "notification"
        assert message.language_code == provider_language_code(default_language())
        assert message.parameters[1] == render_notification(
            rule_key="case.assigned",
            category=NotificationCategory.CASE,
            language=default_language(),
        ).title
        assert "CASE-2026-0001" in message.parameters[2]

    def test_the_link_points_at_the_case(
        self,
        notification_subscriber: Any,
        whatsapp_provider: Any,
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
            in whatsapp_provider.sent[0].parameters[3]
        )

    def test_a_password_reset_is_messaged_without_a_link(
        self,
        notification_subscriber: Any,
        whatsapp_provider: Any,
        lawyer_user: Any,
        db_session: Session,
    ) -> None:
        """One of the spec's Authentication types, and the reason the security
        descriptor exists: a message about a password that asks the reader to tap
        something is the shape of a phishing message — more so here than in an
        email, because the sender is a number they may not recognise."""
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
        assert not any(
            "http" in parameter for parameter in whatsapp_provider.sent[0].parameters
        )

    def test_an_excluded_event_notifies_without_messaging(
        self,
        notification_subscriber: Any,
        whatsapp_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
    ) -> None:
        """The spec's "Events That Must NOT Generate WhatsApp Messages": a
        document upload is an in-app notification and nothing more, and this
        channel cannot override that because it can only narrow."""
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
        assert deliveries(db_session) == []  # and no message was queued
        assert whatsapp_provider.sent == []

    def test_an_account_without_a_number_is_notified_but_not_messaged(
        self,
        notification_subscriber: Any,
        whatsapp_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
    ) -> None:
        """The ordinary case on this channel rather than an edge one — and the
        notification is entirely unaffected, which is what makes an optional phone
        number a non-issue rather than a gap."""
        legal_case = make_case(
            created_by=lawyer_user.id, assigned_lawyer_id=admin_user.id
        )
        created = notification_subscriber.process(
            assignment_event(
                case_id=legal_case.id,
                assignee_id=admin_user.id,
                actor_id=lawyer_user.id,
            )
        )

        assert created == 1
        assert deliveries(db_session) == []
        assert whatsapp_provider.sent == []

    def test_a_disabled_channel_delivers_nothing(
        self,
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing else changes: the notification is created and readable exactly
        as before, which is what makes `WHATSAPP_ENABLED` a configuration rather
        than a degradation."""
        monkeypatch.setattr(settings, "WHATSAPP_ENABLED", False)
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

    def test_the_two_outbound_channels_both_deliver(
        self,
        notification_subscriber: Any,
        whatsapp_provider: Any,
        email_provider: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One notification, two channels, one message each — and neither can
        suppress or duplicate the other, because each writes to its own table
        behind its own unique constraint."""
        monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
        monkeypatch.setattr(settings, "EMAIL_FROM_ADDRESS", "notify@legal.example")

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

        assert len(deliveries(db_session)) == 1
        assert len(whatsapp_provider.sent) == 1
        assert len(email_provider.sent) == 1


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class TestPreferences:
    def test_the_channel_is_offered_on_every_preference(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """A settings page renders from one response, so a channel added later has
        to appear on every row at its default rather than needing a client
        change."""
        response = api_client.get(PREFERENCES_URL, headers=lawyer_headers)

        assert response.status_code == 200, response.text
        body = response.json()["preferences"]
        assert body
        assert all(entry["whatsapp"] is True for entry in body)
        assert all(entry["is_default"] is True for entry in body)

    def test_switching_it_off_silences_the_channel_and_nothing_else(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        db_session: Session,
    ) -> None:
        """The spec's *"users should be able to enable or disable WhatsApp
        delivery independently from In-App notifications, Email notifications"*."""
        response = api_client.put(
            PREFERENCES_URL,
            headers=lawyer_headers,
            json={"preferences": [{"preference_key": "case_updates", "whatsapp": False}]},
        )
        assert response.status_code == 200, response.text

        stored = next(
            entry
            for entry in response.json()["preferences"]
            if entry["preference_key"] == "case_updates"
        )
        assert stored["whatsapp"] is False
        assert stored["in_app"] is True
        assert stored["email"] is True

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

        assert created == 1  # still in the feed
        assert deliveries(db_session) == []  # and not on a phone

    def test_a_change_carrying_one_channel_leaves_the_others_alone(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """Which is what stops a client written before this channel existed from
        switching it off by omission — the protection the email channel introduced
        and this one inherited for free."""
        api_client.put(
            PREFERENCES_URL,
            headers=lawyer_headers,
            json={"preferences": [{"preference_key": "case_updates", "whatsapp": False}]},
        )
        response = api_client.put(
            PREFERENCES_URL,
            headers=lawyer_headers,
            json={"preferences": [{"preference_key": "case_updates", "in_app": False}]},
        )

        stored = next(
            entry
            for entry in response.json()["preferences"]
            if entry["preference_key"] == "case_updates"
        )
        assert stored["whatsapp"] is False
        assert stored["in_app"] is False

    def test_an_empty_change_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """Accepting it would answer 200 while doing nothing, which is the hardest
        kind of failure to notice from the outside."""
        response = api_client.put(
            PREFERENCES_URL,
            headers=lawyer_headers,
            json={"preferences": [{"preference_key": "case_updates"}]},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMonitoring:
    def test_it_reports_the_figures_the_spec_names(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        """Queued messages, delivered messages, failed deliveries, retry count,
        average delivery latency, and provider response time."""
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

        response = api_client.get(WHATSAPP_METRICS_URL, headers=admin_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is True
        assert body["delivered"] == 1
        assert body["failed"] == 0
        assert body["attempts"] == 1
        assert body["delivery_rate"] == 100.0
        assert body["retried"] == 0
        assert "average_delivery_latency_ms" in body
        assert "average_provider_response_ms" in body
        assert body["delivered_by_rule"] == {"case.assigned": 1}

    def test_it_reports_why_nothing_was_sent(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        """Together the skip reasons answer "why did that person not get a
        message?" without anyone reading the delivery table."""
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

        body = api_client.get(WHATSAPP_METRICS_URL, headers=admin_headers).json()
        assert body["skipped_by_reason"]["not_whatsapp_eligible"] == 1

    def test_it_carries_no_number_and_no_message(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        notification_subscriber: Any,
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
    ) -> None:
        """A phone number is more identifying than an address — it is a device
        somebody carries — so the monitoring view carries counts, durations,
        rates, and causes, and nothing that names a person."""
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

        raw = api_client.get(WHATSAPP_METRICS_URL, headers=admin_headers).text
        assert "212612345678" not in raw
        assert "CASE-2026-0001" not in raw
        assert str(lawyer_user.id) not in raw

    def test_there_is_no_endpoint_that_lists_deliveries(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """A list of deliveries would be a live index of who the platform messages
        about what."""
        response = api_client.get(
            f"{NOTIFICATIONS_URL}/whatsapp/deliveries", headers=admin_headers
        )
        assert response.status_code == 404

    def test_it_is_administrative(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """Gated on the existing `notifications:monitor` — no new permission, for
        the reason the email channel added none."""
        response = api_client.get(WHATSAPP_METRICS_URL, headers=lawyer_headers)
        assert response.status_code == 403

    def test_it_needs_authentication(self, api_client: TestClient) -> None:
        assert api_client.get(WHATSAPP_METRICS_URL).status_code == 401
