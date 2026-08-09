"""Unit tests for the Notification Service.

Against the **real repository and the real database** (SQLite in memory), because
the three things this service is actually responsible for — preferences,
duplicate suppression, and read state — are all properties of queries, and a
mocked repository would make every assertion here a test of the mock.

What *is* substituted is the event publisher, so "was this delivered?" is an
assertion about a recorded list rather than about a socket.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from core.config import settings
from core.events import DomainEventType
from core.exceptions import NotificationNotFoundError, NotificationsDisabledError
from core.notifications import (
    EVENT_RULES,
    AnnouncementKind,
    ChannelPreferenceUpdate,
    NotificationCategory,
    NotificationPreferenceKey,
    NotificationPriority,
    NotificationTarget,
    NotificationTargetType,
)
from models.user import UserRole
from repositories.notification import NotificationRepository
from schemas.notification import NotificationListQuery
from services.events import RecordingEventPublisher
from services.notification import NotificationService
from services.notification_metrics import InMemoryNotificationMetrics

CASE_CREATED = EVENT_RULES[DomainEventType.CASE_CREATED]
REPORT_GENERATED = EVENT_RULES[DomainEventType.REPORT_GENERATED]


@pytest.fixture
def publisher() -> RecordingEventPublisher:
    return RecordingEventPublisher()


@pytest.fixture
def metrics() -> InMemoryNotificationMetrics:
    return InMemoryNotificationMetrics()


@pytest.fixture
def service(
    db_session: Session,
    publisher: RecordingEventPublisher,
    metrics: InMemoryNotificationMetrics,
) -> NotificationService:
    return NotificationService(
        NotificationRepository(db_session), events=publisher, metrics=metrics
    )


@pytest.fixture
def lawyer(make_user: Any) -> Any:
    return make_user(email="lawyer@example.com", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: Any) -> Any:
    return make_user(email="other@example.com", role=UserRole.LAWYER)


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


class TestCreation:
    def test_a_notification_is_persisted(
        self, service: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        case_id = uuid.uuid4()
        created = service.create(
            rule=CASE_CREATED,
            recipient_ids=[lawyer.id],
            context={"case_number": "CASE-2026-0001"},
            case_id=case_id,
            target=NotificationTarget(NotificationTargetType.CASE, case_id),
        )

        assert len(created) == 1
        stored = NotificationRepository(db_session).get(
            created[0].id, recipient_id=lawyer.id
        )
        assert stored is not None
        assert stored.rule_key == "case.created"
        assert stored.category == NotificationCategory.CASE.value
        assert stored.context == {"case_number": "CASE-2026-0001"}
        assert stored.read_at is None

    def test_no_prose_is_persisted(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        """The row stores a rule key and a context, and the wording is rendered
        per request — which is what makes 'never log confidential notification
        contents' trivially true."""
        created = service.create(
            rule=CASE_CREATED,
            recipient_ids=[lawyer.id],
            context={"case_number": "CASE-2026-0001"},
        )
        assert not hasattr(created[0], "title")
        assert not hasattr(created[0], "message")

    def test_a_batch_is_one_write_for_every_recipient(
        self, service: NotificationService, lawyer: Any, other_lawyer: Any
    ) -> None:
        created = service.create(
            rule=CASE_CREATED, recipient_ids=[lawyer.id, other_lawyer.id]
        )
        assert {row.recipient_id for row in created} == {lawyer.id, other_lawyer.id}

    def test_an_empty_audience_creates_nothing(self, service: NotificationService) -> None:
        assert service.create(rule=CASE_CREATED, recipient_ids=[]) == []

    def test_creation_is_switched_off_by_configuration(
        self, service: NotificationService, lawyer: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disabling stops creation; reading existing notifications is untouched."""
        monkeypatch.setattr(settings, "NOTIFICATIONS_ENABLED", False)
        assert service.create(rule=CASE_CREATED, recipient_ids=[lawyer.id]) == []


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


class TestDelivery:
    def test_each_notification_is_announced_on_its_own_recipients_topic(
        self, service: NotificationService, lawyer: Any, publisher: RecordingEventPublisher
    ) -> None:
        """Delivery is a publication, never a socket — and a user topic is
        authorized by identity, so it cannot reach anybody else."""
        service.create(rule=CASE_CREATED, recipient_ids=[lawyer.id])

        assert publisher.types() == [DomainEventType.NOTIFICATION_CREATED]
        assert publisher.events[0].topic.resource_id == lawyer.id

    def test_the_announcement_carries_no_wording(
        self, service: NotificationService, lawyer: Any, publisher: RecordingEventPublisher
    ) -> None:
        service.create(
            rule=CASE_CREATED,
            recipient_ids=[lawyer.id],
            context={"case_number": "CASE-2026-0001"},
        )
        payload = publisher.events[0].payload
        assert set(payload) <= {
            "notification_id",
            "category",
            "notification_type",
            "priority",
            "rule_key",
            "target_type",
            "target_id",
        }
        assert "CASE-2026-0001" not in str(payload)

    def test_delivery_is_counted(
        self,
        service: NotificationService,
        lawyer: Any,
        metrics: InMemoryNotificationMetrics,
    ) -> None:
        service.create(rule=CASE_CREATED, recipient_ids=[lawyer.id])
        snapshot = metrics.snapshot()
        assert snapshot.created == 1
        assert snapshot.delivered == 1
        assert snapshot.created_by_rule == {"case.created": 1}
        assert snapshot.average_delivery_latency_ms is not None


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class TestPreferences:
    def test_every_preference_defaults_to_on(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        preferences = service.preferences(actor=lawyer)
        assert set(preferences) == set(NotificationPreferenceKey)
        assert all(entry.in_app for entry in preferences.values())
        # Both channels default to on — see `DEFAULT_PREFERENCES` for why email
        # follows in-app rather than being opt-in.
        assert all(entry.email for entry in preferences.values())
        assert all(entry.is_default for entry in preferences.values())

    def test_a_stored_answer_is_no_longer_a_default(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        updated = service.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(in_app=False)},
            actor=lawyer,
        )
        changed = updated[NotificationPreferenceKey.CASE_UPDATES]
        assert changed.in_app is False
        assert changed.is_default is False
        # The channel the change did not mention keeps the value the user would
        # have seen, rather than being switched off by omission.
        assert changed.email is True
        # Everything else is untouched and still a default.
        untouched = updated[NotificationPreferenceKey.OCR_COMPLETION]
        assert (untouched.in_app, untouched.email, untouched.is_default) == (True, True, True)

    def test_a_channel_can_be_silenced_without_touching_the_other(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        """`17-email-delivery-channel.md`'s "if a user disables email delivery for
        a supported notification type, no email should be sent" — without also
        emptying their in-app feed, which is the setting people actually want."""
        updated = service.update_preferences(
            {NotificationPreferenceKey.HEARING_UPDATES: ChannelPreferenceUpdate(email=False)},
            actor=lawyer,
        )
        changed = updated[NotificationPreferenceKey.HEARING_UPDATES]
        assert changed.email is False
        assert changed.in_app is True

        # And the in-app path is genuinely unaffected: the notification is still
        # created, which is what makes the choice about *delivery* rather than
        # about being told.
        assert service.create(
            rule=EVENT_RULES[DomainEventType.CASE_UPDATED], recipient_ids=[lawyer.id]
        )

    def test_a_switched_off_preference_suppresses_creation(
        self,
        service: NotificationService,
        lawyer: Any,
        metrics: InMemoryNotificationMetrics,
    ) -> None:
        service.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(in_app=False)}, actor=lawyer
        )
        assert service.create(rule=CASE_CREATED, recipient_ids=[lawyer.id]) == []
        assert metrics.snapshot().suppressed_by_preference == 1

    def test_one_persons_preference_does_not_silence_another(
        self, service: NotificationService, lawyer: Any, other_lawyer: Any
    ) -> None:
        service.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(in_app=False)}, actor=lawyer
        )
        created = service.create(
            rule=CASE_CREATED, recipient_ids=[lawyer.id, other_lawyer.id]
        )
        assert [row.recipient_id for row in created] == [other_lawyer.id]

    def test_a_preference_silences_only_its_own_rule(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        """`ocr_completion` is its own axis so extraction can be silenced without
        silencing the case."""
        service.update_preferences(
            {NotificationPreferenceKey.OCR_COMPLETION: ChannelPreferenceUpdate(in_app=False)}, actor=lawyer
        )
        assert service.create(rule=CASE_CREATED, recipient_ids=[lawyer.id])
        assert (
            service.create(
                rule=EVENT_RULES[DomainEventType.OCR_COMPLETED], recipient_ids=[lawyer.id]
            )
            == []
        )

    def test_switching_a_preference_off_keeps_existing_notifications(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        service.create(rule=CASE_CREATED, recipient_ids=[lawyer.id])
        service.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(in_app=False)}, actor=lawyer
        )
        page = service.list_notifications(NotificationListQuery(), actor=lawyer)
        assert page.total == 1

    def test_preferences_survive_a_second_save(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        service.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(in_app=False)}, actor=lawyer
        )
        updated = service.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(in_app=True)},
            actor=lawyer,
        )
        restored = updated[NotificationPreferenceKey.CASE_UPDATES]
        assert (restored.in_app, restored.is_default) == (True, False)


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #


class TestDuplicates:
    def test_the_same_event_notifies_a_person_once(
        self,
        service: NotificationService,
        lawyer: Any,
        metrics: InMemoryNotificationMetrics,
    ) -> None:
        """The exact half: an event's identity is assigned once by the dispatcher
        and never reused, so this cannot suppress a genuine repeat."""
        event_id = uuid.uuid4()
        first = service.create(
            rule=CASE_CREATED, recipient_ids=[lawyer.id], event_id=event_id
        )
        second = service.create(
            rule=CASE_CREATED, recipient_ids=[lawyer.id], event_id=event_id
        )

        assert len(first) == 1
        assert second == []
        assert metrics.snapshot().deduplicated == 1

    def test_the_same_thing_said_twice_in_the_window_is_said_once(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        case_id = uuid.uuid4()
        target = NotificationTarget(NotificationTargetType.CASE, case_id)

        assert service.create(
            rule=CASE_CREATED,
            recipient_ids=[lawyer.id],
            case_id=case_id,
            target=target,
            event_id=uuid.uuid4(),
        )
        assert (
            service.create(
                rule=CASE_CREATED,
                recipient_ids=[lawyer.id],
                case_id=case_id,
                target=target,
                event_id=uuid.uuid4(),
            )
            == []
        )

    def test_the_window_is_a_suppression_rather_than_a_ban(
        self, service: NotificationService, lawyer: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A case genuinely updated twice in a week is two notifications — which
        is why this is a windowed query and not a unique constraint."""
        monkeypatch.setattr(settings, "NOTIFICATION_DEDUPE_WINDOW_SECONDS", 0)
        case_id = uuid.uuid4()

        assert service.create(
            rule=CASE_CREATED, recipient_ids=[lawyer.id], case_id=case_id, event_id=uuid.uuid4()
        )
        assert service.create(
            rule=CASE_CREATED, recipient_ids=[lawyer.id], case_id=case_id, event_id=uuid.uuid4()
        )

    def test_different_cases_are_never_duplicates_of_one_another(
        self, service: NotificationService, lawyer: Any
    ) -> None:
        assert service.create(
            rule=CASE_CREATED, recipient_ids=[lawyer.id], case_id=uuid.uuid4()
        )
        assert service.create(
            rule=CASE_CREATED, recipient_ids=[lawyer.id], case_id=uuid.uuid4()
        )


# --------------------------------------------------------------------------- #
# Reading and read state
# --------------------------------------------------------------------------- #


class TestReading:
    def test_a_feed_is_the_callers_own(
        self, service: NotificationService, lawyer: Any, other_lawyer: Any
    ) -> None:
        service.create(rule=CASE_CREATED, recipient_ids=[lawyer.id, other_lawyer.id])

        page = service.list_notifications(NotificationListQuery(), actor=lawyer)
        assert page.total == 1
        assert page.results[0].recipient_id == lawyer.id

    def test_another_persons_notification_is_not_found(
        self, service: NotificationService, lawyer: Any, other_lawyer: Any
    ) -> None:
        """404 rather than 403: confirming it exists is itself the disclosure."""
        created = service.create(rule=CASE_CREATED, recipient_ids=[other_lawyer.id])

        with pytest.raises(NotificationNotFoundError):
            service.get_notification(created[0].id, actor=lawyer)

    def test_unread_only_filters_in_the_query(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        make_notification(recipient_id=lawyer.id, read_at=datetime.now(UTC))
        make_notification(recipient_id=lawyer.id)

        page = service.list_notifications(
            NotificationListQuery(unread_only=True), actor=lawyer
        )
        assert page.total == 1

    def test_a_category_filter_narrows_the_feed(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        make_notification(recipient_id=lawyer.id, category=NotificationCategory.CASE)
        make_notification(recipient_id=lawyer.id, category=NotificationCategory.REPORT)

        page = service.list_notifications(
            NotificationListQuery(category=NotificationCategory.REPORT), actor=lawyer
        )
        assert page.total == 1
        assert page.results[0].category == NotificationCategory.REPORT.value

    def test_priority_sorts_by_urgency_rather_than_alphabetically(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        """Alphabetically, `critical` sorts below `low`."""
        make_notification(recipient_id=lawyer.id, priority=NotificationPriority.LOW)
        make_notification(recipient_id=lawyer.id, priority=NotificationPriority.CRITICAL)
        make_notification(recipient_id=lawyer.id, priority=NotificationPriority.NORMAL)

        from schemas.notification import NotificationSortField

        page = service.list_notifications(
            NotificationListQuery(sort_by=NotificationSortField.PRIORITY), actor=lawyer
        )
        assert [row.priority for row in page.results] == [
            NotificationPriority.CRITICAL,
            NotificationPriority.NORMAL,
            NotificationPriority.LOW,
        ]

    def test_the_summary_counts_only_the_callers_unread(
        self, service: NotificationService, lawyer: Any, other_lawyer: Any, make_notification: Any
    ) -> None:
        make_notification(recipient_id=lawyer.id)
        make_notification(recipient_id=lawyer.id, read_at=datetime.now(UTC))
        make_notification(recipient_id=other_lawyer.id)

        counts = service.summary(actor=lawyer)
        assert counts.unread == 1
        assert counts.total == 2
        assert counts.unread_capped is False

    def test_the_summary_reports_the_most_urgent_unread(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        make_notification(recipient_id=lawyer.id, priority=NotificationPriority.LOW)
        make_notification(recipient_id=lawyer.id, priority=NotificationPriority.HIGH)

        assert service.summary(actor=lawyer).highest_unread_priority is (
            NotificationPriority.HIGH
        )

    def test_the_unread_count_is_capped(
        self,
        service: NotificationService,
        lawyer: Any,
        make_notification: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Counting thousands exactly is a scan to render '999+'."""
        monkeypatch.setattr(settings, "NOTIFICATION_UNREAD_COUNT_CAP", 2)
        for _ in range(4):
            make_notification(recipient_id=lawyer.id)

        counts = service.summary(actor=lawyer)
        assert counts.unread == 2
        assert counts.unread_capped is True

    def test_an_archived_notification_leaves_every_read(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        """The column has no endpoint yet; the reads already honour it, so the
        future feature is a route rather than a migration."""
        make_notification(recipient_id=lawyer.id, archived_at=datetime.now(UTC))
        make_notification(recipient_id=lawyer.id)

        assert service.list_notifications(NotificationListQuery(), actor=lawyer).total == 1
        assert service.summary(actor=lawyer).total == 1


class TestReadState:
    def test_marking_one_as_read_stamps_it(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        notification = make_notification(recipient_id=lawyer.id)

        assert service.mark_read([notification.id], actor=lawyer) == 1
        assert service.get_notification(notification.id, actor=lawyer).is_read

    def test_re_marking_does_not_move_the_timestamp(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        """'When did I read this' must not move because a list refreshed."""
        notification = make_notification(recipient_id=lawyer.id)
        service.mark_read([notification.id], actor=lawyer)
        first = service.get_notification(notification.id, actor=lawyer).read_at

        assert service.mark_read([notification.id], actor=lawyer) == 0
        assert service.get_notification(notification.id, actor=lawyer).read_at == first

    def test_another_persons_notification_is_ignored_rather_than_refused(
        self, service: NotificationService, lawyer: Any, other_lawyer: Any, make_notification: Any
    ) -> None:
        """Refusing would confirm it exists — the disclosure this feature avoids
        everywhere else."""
        theirs = make_notification(recipient_id=other_lawyer.id)

        assert service.mark_read([theirs.id], actor=lawyer) == 0
        assert not service.get_notification(theirs.id, actor=other_lawyer).is_read

    def test_mark_all_read_clears_the_badge(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        for _ in range(3):
            make_notification(recipient_id=lawyer.id)

        assert service.mark_all_read(actor=lawyer) == 3
        assert service.summary(actor=lawyer).unread == 0

    def test_mark_all_read_can_be_scoped_to_a_category(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        """A feed filtered to one category beside a button that ignored the
        filter is how a notification somebody meant to keep is lost."""
        make_notification(recipient_id=lawyer.id, category=NotificationCategory.CASE)
        make_notification(recipient_id=lawyer.id, category=NotificationCategory.REPORT)

        assert service.mark_all_read(actor=lawyer, category=NotificationCategory.CASE) == 1
        assert service.summary(actor=lawyer).unread == 1

    def test_reading_announces_to_the_readers_other_tabs(
        self, service: NotificationService, lawyer: Any, make_notification: Any,
        publisher: RecordingEventPublisher,
    ) -> None:
        notification = make_notification(recipient_id=lawyer.id)
        publisher.reset()

        service.mark_read([notification.id], actor=lawyer)

        assert publisher.types() == [DomainEventType.NOTIFICATION_READ]
        assert publisher.events[0].topic.resource_id == lawyer.id

    def test_marking_nothing_announces_nothing(
        self, service: NotificationService, lawyer: Any, publisher: RecordingEventPublisher
    ) -> None:
        assert service.mark_all_read(actor=lawyer) == 0
        assert publisher.events == []


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #


class TestAnnouncements:
    def test_an_announcement_reaches_every_active_account(
        self, service: NotificationService, make_user: Any, lawyer: Any, other_lawyer: Any
    ) -> None:
        admin = make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)

        outcome = service.announce(
            kind=AnnouncementKind.ANNOUNCEMENT, message="Read-only on Sunday.", actor=admin
        )
        assert outcome.recipients == 3
        assert outcome.skipped == 0

    def test_an_inactive_account_is_not_written_a_feed_it_cannot_read(
        self, service: NotificationService, make_user: Any, lawyer: Any
    ) -> None:
        admin = make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)
        make_user(email="gone@example.com", role=UserRole.LAWYER, is_active=False)

        outcome = service.announce(
            kind=AnnouncementKind.MAINTENANCE, message="Sunday.", actor=admin
        )
        assert outcome.recipients == 2

    def test_a_switched_off_preference_is_reported_as_skipped(
        self, service: NotificationService, make_user: Any, lawyer: Any
    ) -> None:
        """So an administrator can tell 'nobody was told' from 'nobody wanted to
        be'."""
        admin = make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)
        service.update_preferences(
            {NotificationPreferenceKey.SYSTEM_ANNOUNCEMENTS: ChannelPreferenceUpdate(in_app=False)}, actor=lawyer
        )

        outcome = service.announce(
            kind=AnnouncementKind.ANNOUNCEMENT, message="Sunday.", actor=admin
        )
        assert outcome.recipients == 1
        assert outcome.skipped == 1

    def test_two_announcements_of_the_same_wording_are_two_announcements(
        self, service: NotificationService, make_user: Any, lawyer: Any
    ) -> None:
        """A maintenance window announced twice is two windows; the discriminator
        is what stops the duplicate check silencing the one that mattered."""
        admin = make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)

        service.announce(kind=AnnouncementKind.MAINTENANCE, message="Sunday.", actor=admin)
        second = service.announce(
            kind=AnnouncementKind.MAINTENANCE, message="Sunday.", actor=admin
        )
        assert second.recipients == 2

    def test_announcing_is_refused_when_notifications_are_disabled(
        self, service: NotificationService, make_user: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An administrator pressed a button and is entitled to know it had no
        effect — unlike an event, which nobody is waiting on."""
        admin = make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)
        monkeypatch.setattr(settings, "NOTIFICATIONS_ENABLED", False)

        with pytest.raises(NotificationsDisabledError):
            service.announce(
                kind=AnnouncementKind.ANNOUNCEMENT, message="Sunday.", actor=admin
            )


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_row_counts_come_from_the_database(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        """Exact, surviving a restart, and the same on every instance — unlike
        the in-process counters beside them."""
        make_notification(recipient_id=lawyer.id, category=NotificationCategory.CASE)
        make_notification(
            recipient_id=lawyer.id,
            category=NotificationCategory.REPORT,
            read_at=datetime.now(UTC),
        )

        metrics = service.metrics()
        assert metrics.statistics.total == 2
        assert metrics.statistics.unread == 1
        assert metrics.statistics.read == 1
        assert metrics.statistics.recipients == 1
        assert metrics.statistics.by_category == {"case": 1, "report": 1}

    def test_a_window_narrows_the_row_counts(
        self, service: NotificationService, lawyer: Any, make_notification: Any
    ) -> None:
        make_notification(
            recipient_id=lawyer.id, created_at=datetime.now(UTC) - timedelta(days=30)
        )
        make_notification(recipient_id=lawyer.id)

        assert service.metrics(window_days=7).statistics.total == 1
        assert service.metrics().statistics.total == 2

    def test_the_read_rate_is_zero_when_there_is_nothing(
        self, service: NotificationService
    ) -> None:
        assert service.metrics().statistics.read_rate == 0.0
