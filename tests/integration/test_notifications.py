"""Integration tests for notifications.

Three things end to end, against the real application:

* **the feed** — listing, filtering, rendering, paging, and the badge, through
  the real router, the real schemas, and the real repository;
* **authorization** — that a notification is its recipient's and nobody else's,
  and that the two administrative surfaces are administrative;
* **the event path** — that a real business action (an upload, an assignment, a
  password reset) produces the notification the spec says it should, for the
  people it says it should.

The dispatcher is replaced per test by a recording publisher (see the
``event_publisher`` fixture), so the subscriber is driven explicitly through
``process`` rather than racing a worker thread — which is also what makes "did
uploading a document notify the lawyer?" an assertion rather than a wait.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.events import DomainEventType
from core.localization import default_language
from models.user import UserRole
from tests.helpers import PDF_BYTES

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
NOTIFICATIONS_URL = f"{settings.API_V1_PREFIX}/notifications"
SUMMARY_URL = f"{NOTIFICATIONS_URL}/summary"
PREFERENCES_URL = f"{NOTIFICATIONS_URL}/preferences"
READ_URL = f"{NOTIFICATIONS_URL}/read"
READ_ALL_URL = f"{NOTIFICATIONS_URL}/read-all"
METRICS_URL = f"{NOTIFICATIONS_URL}/metrics"
ANNOUNCEMENTS_URL = f"{NOTIFICATIONS_URL}/announcements"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


@pytest.fixture
def admin_user(make_user: Any) -> Any:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer_user(make_user: Any) -> Any:
    return make_user(email="lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)


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
# The feed
# --------------------------------------------------------------------------- #


class TestFeed:
    def test_a_new_account_has_an_empty_feed(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(NOTIFICATIONS_URL, headers=lawyer_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["total_records"] == 0
        assert body["unread_count"] == 0
        # An empty result still reports one page, so a client never renders
        # "page 1 of 0".
        assert body["total_pages"] == 1

    def test_a_feed_carries_rendered_wording(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        make_notification(
            recipient_id=lawyer_user.id,
            rule_key="case.created",
            context={"case_number": "CASE-2026-0042"},
        )

        body = api_client.get(NOTIFICATIONS_URL, headers=lawyer_headers).json()
        item = body["items"][0]
        assert "CASE-2026-0042" in item["message"]
        assert item["title"]
        # No `?language=` was sent, so the feed renders in the language this
        # reader is addressed in — their stored preference, then the platform's
        # default, then the application's. This account has chosen nothing.
        assert item["language"] == default_language()

    def test_the_same_row_is_rendered_in_the_language_asked_for(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        """No prose is stored, so switching to Arabic re-renders the **whole
        history** rather than only what arrives afterwards."""
        make_notification(recipient_id=lawyer_user.id, rule_key="case.created")

        french = api_client.get(
            f"{NOTIFICATIONS_URL}?language=fr", headers=lawyer_headers
        ).json()["items"][0]
        arabic = api_client.get(
            f"{NOTIFICATIONS_URL}?language=ar", headers=lawyer_headers
        ).json()["items"][0]

        assert french["id"] == arabic["id"]
        assert french["title"] != arabic["title"]
        assert arabic["language"] == "ar"

    def test_a_feed_is_the_callers_own(
        self,
        api_client: TestClient,
        lawyer_user: Any,
        admin_user: Any,
        lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        make_notification(recipient_id=admin_user.id)
        make_notification(recipient_id=lawyer_user.id)

        body = api_client.get(NOTIFICATIONS_URL, headers=lawyer_headers).json()
        assert body["total_records"] == 1

    def test_filters_execute_in_the_query(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        from core.notifications import NotificationCategory, NotificationPriority

        make_notification(recipient_id=lawyer_user.id, category=NotificationCategory.CASE)
        make_notification(
            recipient_id=lawyer_user.id,
            category=NotificationCategory.REPORT,
            priority=NotificationPriority.HIGH,
        )

        body = api_client.get(
            f"{NOTIFICATIONS_URL}?category=report", headers=lawyer_headers
        ).json()
        assert body["total_records"] == 1
        assert body["items"][0]["category"] == "report"

        body = api_client.get(
            f"{NOTIFICATIONS_URL}?priority=high", headers=lawyer_headers
        ).json()
        assert body["total_records"] == 1

    def test_an_unknown_filter_value_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            f"{NOTIFICATIONS_URL}?category=deposition", headers=lawyer_headers
        )
        assert response.status_code == 422

    def test_one_notification_can_be_read(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        notification = make_notification(recipient_id=lawyer_user.id)

        response = api_client.get(
            f"{NOTIFICATIONS_URL}/{notification.id}", headers=lawyer_headers
        )
        assert response.status_code == 200
        assert response.json()["id"] == str(notification.id)

    def test_reading_one_does_not_mark_it_read(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        """Only the client knows whether a notification was *seen*; a GET with
        that side effect would quietly empty somebody's badge on a prefetch."""
        notification = make_notification(recipient_id=lawyer_user.id)

        api_client.get(f"{NOTIFICATIONS_URL}/{notification.id}", headers=lawyer_headers)

        assert api_client.get(SUMMARY_URL, headers=lawyer_headers).json()["unread_count"] == 1

    def test_another_persons_notification_is_a_404(
        self,
        api_client: TestClient,
        admin_user: Any,
        lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        """404 rather than 403: confirming it exists is itself the disclosure."""
        theirs = make_notification(recipient_id=admin_user.id)

        response = api_client.get(f"{NOTIFICATIONS_URL}/{theirs.id}", headers=lawyer_headers)
        assert response.status_code == 404

    def test_an_unknown_notification_is_a_404(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            f"{NOTIFICATIONS_URL}/{uuid.uuid4()}", headers=lawyer_headers
        )
        assert response.status_code == 404

    def test_the_feed_requires_authentication(self, api_client: TestClient) -> None:
        assert api_client.get(NOTIFICATIONS_URL).status_code == 401


# --------------------------------------------------------------------------- #
# The badge and read state
# --------------------------------------------------------------------------- #


class TestReadState:
    def test_the_summary_reports_unread_state(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        from core.notifications import NotificationCategory

        make_notification(recipient_id=lawyer_user.id, category=NotificationCategory.CASE)
        make_notification(recipient_id=lawyer_user.id, category=NotificationCategory.REPORT)

        body = api_client.get(SUMMARY_URL, headers=lawyer_headers).json()
        assert body["unread_count"] == 2
        assert body["total_count"] == 2
        assert body["unread_by_category"] == {"case": 1, "report": 1}
        assert body["unread_count_capped"] is False

    def test_marking_one_as_read_returns_the_new_badge(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        first = make_notification(recipient_id=lawyer_user.id)
        make_notification(recipient_id=lawyer_user.id)

        response = api_client.patch(
            READ_URL, json={"notification_ids": [str(first.id)]}, headers=lawyer_headers
        )
        assert response.status_code == 200
        assert response.json()["unread_count"] == 1

    def test_marking_somebody_elses_is_ignored_rather_than_refused(
        self,
        api_client: TestClient,
        admin_user: Any,
        admin_headers: dict[str, str],
        lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        theirs = make_notification(recipient_id=admin_user.id)

        response = api_client.patch(
            READ_URL, json={"notification_ids": [str(theirs.id)]}, headers=lawyer_headers
        )
        assert response.status_code == 200
        # Untouched from its owner's point of view.
        assert api_client.get(SUMMARY_URL, headers=admin_headers).json()["unread_count"] == 1

    def test_mark_all_read_clears_the_badge(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        for _ in range(3):
            make_notification(recipient_id=lawyer_user.id)

        response = api_client.patch(READ_ALL_URL, json={}, headers=lawyer_headers)
        assert response.status_code == 200
        assert response.json()["unread_count"] == 0

    def test_mark_all_read_honours_a_category(
        self, api_client: TestClient, lawyer_user: Any, lawyer_headers: dict[str, str],
        make_notification: Any,
    ) -> None:
        from core.notifications import NotificationCategory

        make_notification(recipient_id=lawyer_user.id, category=NotificationCategory.CASE)
        make_notification(recipient_id=lawyer_user.id, category=NotificationCategory.REPORT)

        response = api_client.patch(
            READ_ALL_URL, json={"category": "case"}, headers=lawyer_headers
        )
        assert response.json()["unread_count"] == 1

    def test_a_bulk_read_beyond_the_ceiling_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.patch(
            READ_URL,
            json={
                "notification_ids": [
                    str(uuid.uuid4())
                    for _ in range(settings.NOTIFICATION_MAX_BULK_READ + 1)
                ]
            },
            headers=lawyer_headers,
        )
        assert response.status_code == 422

    def test_an_empty_bulk_read_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.patch(
            READ_URL, json={"notification_ids": []}, headers=lawyer_headers
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class TestPreferences:
    def test_every_preference_is_offered_at_its_default(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        from core.notifications import NotificationPreferenceKey

        body = api_client.get(PREFERENCES_URL, headers=lawyer_headers).json()

        assert len(body["preferences"]) == len(NotificationPreferenceKey)
        assert all(entry["in_app"] for entry in body["preferences"])
        assert all(entry["is_default"] for entry in body["preferences"])

    def test_a_preference_can_be_switched_off(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.put(
            PREFERENCES_URL,
            json={"preferences": [{"preference_key": "ocr_completion", "in_app": False}]},
            headers=lawyer_headers,
        )
        assert response.status_code == 200

        entries = {
            entry["preference_key"]: entry for entry in response.json()["preferences"]
        }
        assert entries["ocr_completion"]["in_app"] is False
        assert entries["ocr_completion"]["is_default"] is False
        # Everything else is untouched, which is what makes a partial save safe.
        assert entries["case_updates"]["in_app"] is True

    def test_preferences_are_per_account(
        self, api_client: TestClient, lawyer_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        api_client.put(
            PREFERENCES_URL,
            json={"preferences": [{"preference_key": "case_updates", "in_app": False}]},
            headers=lawyer_headers,
        )

        entries = {
            entry["preference_key"]: entry
            for entry in api_client.get(PREFERENCES_URL, headers=admin_headers).json()[
                "preferences"
            ]
        }
        assert entries["case_updates"]["in_app"] is True

    def test_an_unknown_preference_is_refused(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.put(
            PREFERENCES_URL,
            json={"preferences": [{"preference_key": "carrier_pigeon", "in_app": False}]},
            headers=lawyer_headers,
        )
        assert response.status_code == 422

    def test_every_role_may_manage_its_own_preferences(
        self, api_client: TestClient, court_headers: dict[str, str]
    ) -> None:
        """`notifications:view` is in `BASE_PERMISSIONS`: a role that could not
        see its own alerts would watch a badge it cannot explain."""
        assert api_client.get(PREFERENCES_URL, headers=court_headers).status_code == 200


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #


class TestAnnouncements:
    def test_an_administrator_can_address_the_platform(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
    ) -> None:
        response = api_client.post(
            ANNOUNCEMENTS_URL,
            json={"kind": "maintenance", "message": "Read-only on Sunday 08:00-10:00."},
            headers=admin_headers,
        )
        assert response.status_code == 201
        assert response.json()["recipients"] >= 2

        feed = api_client.get(NOTIFICATIONS_URL, headers=lawyer_headers).json()
        assert feed["items"][0]["category"] == "system"
        assert "Read-only on Sunday" in feed["items"][0]["message"]

    def test_a_lawyer_may_not_address_the_platform(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            ANNOUNCEMENTS_URL,
            json={"message": "Everybody go home."},
            headers=lawyer_headers,
        )
        assert response.status_code == 403

    def test_a_court_representative_may_not_address_the_platform(
        self, api_client: TestClient, court_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            ANNOUNCEMENTS_URL,
            json={"message": "Everybody go home."},
            headers=court_headers,
        )
        assert response.status_code == 403

    def test_a_blank_announcement_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            ANNOUNCEMENTS_URL, json={"message": "   "}, headers=admin_headers
        )
        assert response.status_code == 422

    def test_an_announcement_respects_the_preference(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_headers: dict[str, str],
    ) -> None:
        api_client.put(
            PREFERENCES_URL,
            json={
                "preferences": [
                    {"preference_key": "system_announcements", "in_app": False}
                ]
            },
            headers=lawyer_headers,
        )

        response = api_client.post(
            ANNOUNCEMENTS_URL, json={"message": "Sunday."}, headers=admin_headers
        )
        assert response.json()["skipped"] == 1
        assert api_client.get(NOTIFICATIONS_URL, headers=lawyer_headers).json()[
            "total_records"
        ] == 0


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMonitoring:
    def test_an_administrator_reads_platform_health(
        self, api_client: TestClient, admin_headers: dict[str, str], admin_user: Any,
        make_notification: Any,
    ) -> None:
        make_notification(recipient_id=admin_user.id)

        response = api_client.get(METRICS_URL, headers=admin_headers)
        assert response.status_code == 200

        body = response.json()
        assert body["total_notifications"] == 1
        assert body["unread_notifications"] == 1
        assert body["enabled"] is True
        assert "since" in body
        assert body["pending"] == 0

    def test_a_lawyer_may_not_read_platform_health(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        """`notifications:monitor` is administrative, like every other
        `*:monitor` permission."""
        assert api_client.get(METRICS_URL, headers=lawyer_headers).status_code == 403

    def test_the_metrics_view_names_no_recipient(
        self, api_client: TestClient, admin_headers: dict[str, str], admin_user: Any,
        make_notification: Any,
    ) -> None:
        """An operational view reports counts, categories, and rule keys — never
        whose feed anything was in."""
        make_notification(recipient_id=admin_user.id)

        body = api_client.get(METRICS_URL, headers=admin_headers).text
        assert str(admin_user.id) not in body
        assert admin_user.email not in body


# --------------------------------------------------------------------------- #
# The event path, end to end
# --------------------------------------------------------------------------- #


class TestEventDrivenCreation:
    """A real business action, through the real API, becoming a notification.

    The dispatcher is the recording publisher (see the ``event_publisher``
    fixture), so these tests take what the business module *actually published*
    and hand it to the subscriber — which is the same object the lifespan
    registers, exercised without a thread.
    """

    def test_uploading_a_document_notifies_the_case(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        admin_user: Any,
        make_case: Any,
        event_publisher: Any,
        notification_subscriber: Any,
    ) -> None:
        legal_case = make_case(created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id)

        response = api_client.post(
            f"{settings.API_V1_PREFIX}/documents/upload",
            data={"case_id": str(legal_case.id), "category": "evidence"},
            files={"file": ("filing.pdf", PDF_BYTES, "application/pdf")},
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text

        uploaded = [
            event
            for event in event_publisher.events
            if event.event_type is DomainEventType.DOCUMENT_UPLOADED
        ]
        assert uploaded, "the document service must publish an upload"
        assert notification_subscriber.process(uploaded[0]) == 1

        feed = api_client.get(NOTIFICATIONS_URL, headers=lawyer_headers).json()
        assert feed["total_records"] == 1
        assert feed["items"][0]["category"] == "document"
        assert feed["items"][0]["target"]["target_type"] == "document"

    def test_uploading_a_document_never_notifies_an_unrelated_lawyer(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        admin_user: Any,
        lawyer_user: Any,
        make_user: Any,
        make_case: Any,
        event_publisher: Any,
        notification_subscriber: Any,
    ) -> None:
        """The spec's example, exactly: *"a document upload notification must
        never be created for a user who cannot access the document."*"""
        outsider = make_user(
            email="outsider@example.com", password=PASSWORD, role=UserRole.LAWYER
        )
        legal_case = make_case(created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id)

        api_client.post(
            f"{settings.API_V1_PREFIX}/documents/upload",
            data={"case_id": str(legal_case.id), "category": "evidence"},
            files={"file": ("filing.pdf", PDF_BYTES, "application/pdf")},
            headers=admin_headers,
        )
        for event in list(event_publisher.events):
            notification_subscriber.process(event)

        outsider_feed = api_client.get(
            NOTIFICATIONS_URL, headers=bearer(token_for(api_client, outsider.email))
        ).json()
        assert outsider_feed["total_records"] == 0

    def test_assigning_a_case_notifies_the_lawyer_it_was_assigned_to(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        admin_user: Any,
        lawyer_user: Any,
        lawyer_headers: dict[str, str],
        make_case: Any,
        event_publisher: Any,
        notification_subscriber: Any,
    ) -> None:
        legal_case = make_case(created_by=admin_user.id)

        response = api_client.patch(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": str(lawyer_user.id)},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text

        assignments = [
            event
            for event in event_publisher.events
            if event.event_type is DomainEventType.CASE_ASSIGNMENT_CHANGED
        ]
        assert assignments
        for event in assignments:
            notification_subscriber.process(event)

        feed = api_client.get(NOTIFICATIONS_URL, headers=lawyer_headers).json()
        assert feed["total_records"] == 1
        assert feed["items"][0]["rule_key"] == "case.assigned"
        assert feed["items"][0]["priority"] == "high"

    def test_resetting_a_password_notifies_the_account_holder(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        event_publisher: Any,
        notification_subscriber: Any,
    ) -> None:
        response = api_client.post(
            f"{settings.API_V1_PREFIX}/users/{lawyer_user.id}/reset-password",
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text

        resets = [
            event
            for event in event_publisher.events
            if event.event_type is DomainEventType.USER_PASSWORD_RESET
        ]
        assert resets, "the user service must publish a password reset"
        assert notification_subscriber.process(resets[0]) == 1

        # A reset revokes every session, so the fixture's token is no longer
        # usable — signing in with the temporary password is how the account
        # holder actually reaches their own feed.
        temporary = response.json()["temporary_password"]
        headers = bearer(token_for(api_client, lawyer_user.email, temporary))

        feed = api_client.get(NOTIFICATIONS_URL, headers=headers).json()
        assert feed["items"][0]["priority"] == "critical"
        assert feed["items"][0]["category"] == "user"

    def test_no_credential_travels_on_a_password_reset_event(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer_user: Any,
        event_publisher: Any,
    ) -> None:
        response = api_client.post(
            f"{settings.API_V1_PREFIX}/users/{lawyer_user.id}/reset-password",
            headers=admin_headers,
        )
        temporary = response.json()["temporary_password"]

        for event in event_publisher.events:
            assert temporary not in str(event.payload)

    def test_a_created_notification_is_announced_on_the_recipients_own_topic(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        admin_user: Any,
        lawyer_user: Any,
        make_case: Any,
        event_publisher: Any,
        notification_subscriber: Any,
    ) -> None:
        """Delivery reuses the existing channel: the service publishes, and the
        connection manager routes — it never touches a socket."""
        legal_case = make_case(created_by=admin_user.id, assigned_lawyer_id=lawyer_user.id)

        api_client.post(
            f"{settings.API_V1_PREFIX}/documents/upload",
            data={"case_id": str(legal_case.id), "category": "evidence"},
            files={"file": ("filing.pdf", PDF_BYTES, "application/pdf")},
            headers=admin_headers,
        )
        uploaded = next(
            event
            for event in event_publisher.events
            if event.event_type is DomainEventType.DOCUMENT_UPLOADED
        )
        notification_subscriber.process(uploaded)

        announcements = [
            event
            for event in event_publisher.events
            if event.event_type is DomainEventType.NOTIFICATION_CREATED
        ]
        assert len(announcements) == 1
        assert announcements[0].topic.key == f"user:{lawyer_user.id}"
        assert "message" not in announcements[0].payload
        assert "title" not in announcements[0].payload
