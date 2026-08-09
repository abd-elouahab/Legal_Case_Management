"""Unit tests for the notification event subscriber.

The half of this feature that makes it *event-driven*: a domain event goes in,
notifications for the right people come out, and no business module is involved
at either end.

Driven through :meth:`~services.notification_events.NotificationEventSubscriber.process`
rather than through the queue, deliberately — the method is public and
synchronous precisely so these assertions are about rows rather than about
whether a worker thread got there first.

The recipient resolution runs against the **real** case policy and the real
repositories on the test database, because "a document upload notification must
never be created for a user who cannot access the document" is the spec's
sharpest requirement and a mocked policy would make every assertion here a test
of the mock.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from core.config import settings
from core.events import (
    DomainEvent,
    DomainEventType,
    case_topic,
    document_topic,
    report_topic,
    user_topic,
)
from core.notifications import NotificationCategory, NotificationPriority
from models.report import Report, ReportStatus, ReportType
from models.user import UserRole
from repositories.notification import NotificationRepository
from schemas.notification import NotificationListQuery
from services.notification_events import NotificationEventSubscriber


@pytest.fixture
def admin(make_user: Any) -> Any:
    return make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer(make_user: Any) -> Any:
    return make_user(email="lawyer@example.com", role=UserRole.LAWYER)


@pytest.fixture
def court(make_user: Any) -> Any:
    return make_user(email="court@example.com", role=UserRole.COURT_REPRESENTATIVE)


@pytest.fixture
def outsider(make_user: Any) -> Any:
    """A lawyer assigned to nothing. The person who must never be notified."""
    return make_user(email="outsider@example.com", role=UserRole.LAWYER)


@pytest.fixture
def legal_case(make_case: Any, admin: Any, lawyer: Any, court: Any) -> Any:
    return make_case(
        created_by=admin.id,
        assigned_lawyer_id=lawyer.id,
        assigned_court_representative_id=court.id,
    )


def _recipients(db_session: Session, user_id: uuid.UUID) -> list[Any]:
    rows, _ = NotificationRepository(db_session).list_notifications(
        NotificationListQuery(), recipient_id=user_id
    )
    return rows


def _event(
    event_type: DomainEventType,
    *,
    topic: Any,
    case_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    **payload: Any,
) -> DomainEvent:
    return DomainEvent.create(
        event_type=event_type,
        topic=topic,
        sequence=1,
        case_id=case_id,
        actor_id=actor_id,
        payload=payload,
    )


# --------------------------------------------------------------------------- #
# The subscription contract
# --------------------------------------------------------------------------- #


class TestSubscriberContract:
    def test_it_implements_the_dispatcher_contract(
        self, notification_subscriber: NotificationEventSubscriber
    ) -> None:
        """`EventSubscriber` is a protocol: a name and a `handle` that never
        raises and never blocks."""
        assert notification_subscriber.name == "notifications"
        assert callable(notification_subscriber.handle)

    def test_handle_never_raises(
        self, notification_subscriber: NotificationEventSubscriber
    ) -> None:
        """A subscriber that raised would fail a request that already committed."""
        notification_subscriber.handle(
            _event(DomainEventType.PRESENCE_CHANGED, topic=user_topic(uuid.uuid4()))
        )

    def test_events_with_no_rule_never_reach_the_queue(
        self, notification_subscriber: NotificationEventSubscriber
    ) -> None:
        """Timeline updates and presence changes are most of the platform's
        traffic; queueing them to discard them would spend a bounded resource on
        events that were never going to produce anything."""
        for event_type, topic in (
            (DomainEventType.TIMELINE_UPDATED, case_topic(uuid.uuid4())),
            (DomainEventType.PRESENCE_CHANGED, user_topic(uuid.uuid4())),
            (DomainEventType.NOTIFICATION_CREATED, user_topic(uuid.uuid4())),
        ):
            notification_subscriber.handle(_event(event_type, topic=topic))

        assert notification_subscriber.pending == 0

    def test_a_relevant_event_is_queued(
        self, notification_subscriber: NotificationEventSubscriber
    ) -> None:
        notification_subscriber.handle(
            _event(
                DomainEventType.CASE_CREATED,
                topic=case_topic(uuid.uuid4()),
                case_id=uuid.uuid4(),
            )
        )
        assert notification_subscriber.pending == 1

    def test_a_full_queue_drops_rather_than_blocking_the_publisher(
        self,
        notification_subscriber: NotificationEventSubscriber,
        notification_metrics: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Blocking would trade a lost notification for a stalled operation."""
        case_id = uuid.uuid4()
        for _ in range(settings.NOTIFICATION_QUEUE_SIZE + 5):
            notification_subscriber.handle(
                _event(
                    DomainEventType.CASE_CREATED,
                    topic=case_topic(case_id),
                    case_id=case_id,
                )
            )

        assert notification_metrics.snapshot().dropped >= 1

    def test_it_accepts_nothing_when_notifications_are_disabled(
        self,
        notification_subscriber: NotificationEventSubscriber,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "NOTIFICATIONS_ENABLED", False)
        notification_subscriber.handle(
            _event(
                DomainEventType.CASE_CREATED,
                topic=case_topic(uuid.uuid4()),
                case_id=uuid.uuid4(),
            )
        )
        assert notification_subscriber.pending == 0


# --------------------------------------------------------------------------- #
# Case events
# --------------------------------------------------------------------------- #


class TestCaseEvents:
    def test_a_case_event_notifies_everyone_party_to_the_case(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        admin: Any,
        lawyer: Any,
        court: Any,
    ) -> None:
        created = notification_subscriber.process(
            _event(
                DomainEventType.CASE_CREATED,
                topic=case_topic(legal_case.id),
                case_id=legal_case.id,
                case_number=legal_case.case_number,
            )
        )

        assert created == 3
        for user in (admin, lawyer, court):
            assert len(_recipients(db_session, user.id)) == 1

    def test_a_case_event_never_reaches_somebody_not_on_the_case(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        outsider: Any,
    ) -> None:
        """The spec's sharpest requirement, stated for a case rather than a
        document — and the same rule, since document access *is* case access."""
        notification_subscriber.process(
            _event(
                DomainEventType.CASE_CREATED,
                topic=case_topic(legal_case.id),
                case_id=legal_case.id,
                case_number=legal_case.case_number,
            )
        )
        assert _recipients(db_session, outsider.id) == []

    def test_the_actor_is_not_told_what_they_just_did(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        admin: Any,
        lawyer: Any,
    ) -> None:
        notification_subscriber.process(
            _event(
                DomainEventType.CASE_UPDATED,
                topic=case_topic(legal_case.id),
                case_id=legal_case.id,
                actor_id=admin.id,
                case_number=legal_case.case_number,
                fields=["title"],
            )
        )

        assert _recipients(db_session, admin.id) == []
        assert len(_recipients(db_session, lawyer.id)) == 1

    def test_a_court_field_update_arrives_as_hearing_news(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        lawyer: Any,
    ) -> None:
        notification_subscriber.process(
            _event(
                DomainEventType.CASE_UPDATED,
                topic=case_topic(legal_case.id),
                case_id=legal_case.id,
                case_number=legal_case.case_number,
                fields=["next hearing date"],
            )
        )

        notification = _recipients(db_session, lawyer.id)[0]
        assert notification.category == NotificationCategory.HEARING.value
        assert notification.rule_key == "hearing.updated"

    def test_an_assignment_notifies_only_the_person_assigned(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        admin: Any,
        lawyer: Any,
        court: Any,
    ) -> None:
        """Being handed a matter is news for the person handed it, not for
        everybody already on it."""
        created = notification_subscriber.process(
            _event(
                DomainEventType.CASE_ASSIGNMENT_CHANGED,
                topic=case_topic(legal_case.id),
                case_id=legal_case.id,
                actor_id=admin.id,
                case_number=legal_case.case_number,
                assignee_id=str(lawyer.id),
                assigned=True,
                position="lawyer",
            )
        )

        assert created == 1
        assigned = _recipients(db_session, lawyer.id)[0]
        assert assigned.rule_key == "case.assigned"
        assert assigned.priority is NotificationPriority.HIGH
        assert _recipients(db_session, court.id) == []

    def test_an_unassignment_says_so_and_offers_nothing_to_open(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        admin: Any,
        lawyer: Any,
    ) -> None:
        notification_subscriber.process(
            _event(
                DomainEventType.CASE_ASSIGNMENT_CHANGED,
                topic=case_topic(legal_case.id),
                case_id=legal_case.id,
                actor_id=admin.id,
                case_number=legal_case.case_number,
                assignee_id=str(lawyer.id),
                assigned=False,
                position="lawyer",
            )
        )

        notification = _recipients(db_session, lawyer.id)[0]
        assert notification.rule_key == "case.unassigned"
        assert notification.target_type is None

    def test_a_case_that_no_longer_exists_produces_nothing(
        self,
        notification_subscriber: NotificationEventSubscriber,
        notification_metrics: Any,
    ) -> None:
        """Fails closed: an unresolvable resource notifies nobody rather than
        everybody, and is counted so an operator sees it."""
        missing = uuid.uuid4()
        assert (
            notification_subscriber.process(
                _event(
                    DomainEventType.CASE_CREATED,
                    topic=case_topic(missing),
                    case_id=missing,
                )
            )
            == 0
        )
        assert notification_metrics.snapshot().failed == 1


# --------------------------------------------------------------------------- #
# Document and OCR events
# --------------------------------------------------------------------------- #


class TestDocumentEvents:
    def test_a_document_upload_notifies_the_case(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        admin: Any,
        lawyer: Any,
    ) -> None:
        document_id = uuid.uuid4()
        notification_subscriber.process(
            _event(
                DomainEventType.DOCUMENT_UPLOADED,
                topic=document_topic(document_id),
                case_id=legal_case.id,
                actor_id=admin.id,
                document_id=str(document_id),
                category="evidence",
                version=1,
                file_extension="pdf",
            )
        )

        notification = _recipients(db_session, lawyer.id)[0]
        assert notification.category == NotificationCategory.DOCUMENT.value
        assert notification.target_type == "document"
        assert notification.target_id == document_id

    def test_a_document_notification_carries_no_filename(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        lawyer: Any,
    ) -> None:
        """A notification's context is built from the rule's own key list, so
        nothing a publisher invents can reach it — and the event does not carry a
        filename in the first place."""
        document_id = uuid.uuid4()
        notification_subscriber.process(
            _event(
                DomainEventType.DOCUMENT_UPLOADED,
                topic=document_topic(document_id),
                case_id=legal_case.id,
                document_id=str(document_id),
                category="evidence",
                file_extension="pdf",
            )
        )

        context = _recipients(db_session, lawyer.id)[0].context
        assert set(context) <= {"case_number", "category", "file_extension"}

    def test_a_failed_extraction_is_an_error_at_high_priority(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        lawyer: Any,
    ) -> None:
        document_id = uuid.uuid4()
        notification_subscriber.process(
            _event(
                DomainEventType.OCR_FAILED,
                topic=document_topic(document_id),
                case_id=legal_case.id,
                document_id=str(document_id),
                ocr_status="failed",
                error_code="engine_failure",
            )
        )

        notification = _recipients(db_session, lawyer.id)[0]
        assert notification.category == NotificationCategory.OCR.value
        assert notification.priority is NotificationPriority.HIGH
        assert notification.context["error_code"] == "engine_failure"

    def test_a_successful_extraction_is_low_priority(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        legal_case: Any,
        lawyer: Any,
    ) -> None:
        """The pipeline working as designed is worth seeing and not worth
        interrupting anyone for."""
        document_id = uuid.uuid4()
        notification_subscriber.process(
            _event(
                DomainEventType.OCR_COMPLETED,
                topic=document_topic(document_id),
                case_id=legal_case.id,
                document_id=str(document_id),
                ocr_status="completed",
                page_count=4,
            )
        )

        assert _recipients(db_session, lawyer.id)[0].priority is NotificationPriority.LOW


# --------------------------------------------------------------------------- #
# Report events
# --------------------------------------------------------------------------- #


class TestReportEvents:
    @pytest.fixture
    def report(self, db_session: Session, legal_case: Any, lawyer: Any) -> Report:
        report = Report(
            id=uuid.uuid4(),
            case_id=legal_case.id,
            report_type=ReportType.CASE_SUMMARY,
            title="Case summary",
            language="fr",
            status=ReportStatus.COMPLETED,
            requested_by=lawyer.id,
        )
        db_session.add(report)
        db_session.commit()
        return report

    def test_a_finished_report_notifies_only_its_author(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        report: Report,
        legal_case: Any,
        lawyer: Any,
        admin: Any,
        court: Any,
    ) -> None:
        """A report is private work product: the case's participants learn from
        the timeline that one exists, and only its author is told it is ready."""
        created = notification_subscriber.process(
            _event(
                DomainEventType.REPORT_GENERATED,
                topic=report_topic(report.id),
                case_id=legal_case.id,
                actor_id=lawyer.id,
                report_id=str(report.id),
                report_type="case_summary",
                report_status="completed",
            )
        )

        assert created == 1
        assert len(_recipients(db_session, lawyer.id)) == 1
        assert _recipients(db_session, admin.id) == []
        assert _recipients(db_session, court.id) == []

    def test_a_report_notification_opens_the_report(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        report: Report,
        legal_case: Any,
        lawyer: Any,
    ) -> None:
        notification_subscriber.process(
            _event(
                DomainEventType.REPORT_GENERATED,
                topic=report_topic(report.id),
                case_id=legal_case.id,
                report_id=str(report.id),
                report_type="case_summary",
            )
        )

        notification = _recipients(db_session, lawyer.id)[0]
        assert notification.target_type == "report"
        assert notification.target_id == report.id

    def test_an_author_unassigned_from_the_case_still_owns_their_report(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        make_case: Any,
        admin: Any,
        outsider: Any,
    ) -> None:
        """Ownership *is* the authorization here — the same rule
        `services/realtime_access.py` applies to a report topic."""
        unrelated_case = make_case(created_by=admin.id)
        report = Report(
            id=uuid.uuid4(),
            case_id=unrelated_case.id,
            report_type=ReportType.CASE_SUMMARY,
            title="Case summary",
            language="fr",
            status=ReportStatus.COMPLETED,
            requested_by=outsider.id,
        )
        db_session.add(report)
        db_session.commit()

        created = notification_subscriber.process(
            _event(
                DomainEventType.REPORT_GENERATED,
                topic=report_topic(report.id),
                case_id=unrelated_case.id,
                report_id=str(report.id),
                report_type="case_summary",
            )
        )
        assert created == 1


# --------------------------------------------------------------------------- #
# Account events
# --------------------------------------------------------------------------- #


class TestAccountEvents:
    def test_a_password_reset_notifies_the_account_it_was_reset_for(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        admin: Any,
        lawyer: Any,
    ) -> None:
        created = notification_subscriber.process(
            _event(
                DomainEventType.USER_PASSWORD_RESET,
                topic=user_topic(lawyer.id),
                actor_id=admin.id,
                user_id=str(lawyer.id),
                role=lawyer.role.value,
                sessions_revoked=True,
            )
        )

        assert created == 1
        notification = _recipients(db_session, lawyer.id)[0]
        assert notification.priority is NotificationPriority.CRITICAL
        assert notification.category == NotificationCategory.USER.value
        assert _recipients(db_session, admin.id) == []

    def test_a_role_change_names_the_new_role(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        admin: Any,
        lawyer: Any,
    ) -> None:
        notification_subscriber.process(
            _event(
                DomainEventType.USER_ROLE_CHANGED,
                topic=user_topic(lawyer.id),
                actor_id=admin.id,
                user_id=str(lawyer.id),
                role="administrator",
                previous_role="lawyer",
            )
        )

        assert _recipients(db_session, lawyer.id)[0].context["role"] == "administrator"

    def test_a_deactivated_account_is_not_written_a_notification(
        self,
        notification_subscriber: NotificationEventSubscriber,
        db_session: Session,
        make_user: Any,
        admin: Any,
    ) -> None:
        """Two reasons, and either alone would be enough: there is no rule for
        `user.deactivated`, and a disabled account is dropped by the authorization
        pass anyway."""
        disabled = make_user(email="gone@example.com", role=UserRole.LAWYER, is_active=False)

        assert (
            notification_subscriber.process(
                _event(
                    DomainEventType.USER_DEACTIVATED,
                    topic=user_topic(disabled.id),
                    actor_id=admin.id,
                    user_id=str(disabled.id),
                    role=disabled.role.value,
                )
            )
            == 0
        )
        assert _recipients(db_session, disabled.id) == []


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


class TestIsolation:
    def test_a_resolver_failure_is_counted_rather_than_raised(
        self,
        session_factory: Any,
        event_publisher: Any,
        notification_metrics: Any,
    ) -> None:
        """*"Failures should never affect business operations."* The business
        change committed before this ran; raising would answer a successful
        request with a 500 and invite a duplicating retry."""

        class BrokenResolver:
            def resolve(self, event: Any, rule: Any) -> Any:
                raise RuntimeError("database is on fire")

        subscriber = NotificationEventSubscriber(
            session_factory,
            recipients=BrokenResolver(),  # type: ignore[arg-type]
            publisher=event_publisher,
            metrics=notification_metrics,
        )

        with pytest.raises(RuntimeError):
            # `process` is the *inner* call and is allowed to propagate; the
            # worker loop is what swallows. Asserted so the boundary is explicit
            # rather than assumed.
            subscriber.process(
                _event(
                    DomainEventType.CASE_CREATED,
                    topic=case_topic(uuid.uuid4()),
                    case_id=uuid.uuid4(),
                )
            )

    def test_the_worker_loop_swallows_what_process_raises(
        self,
        session_factory: Any,
        event_publisher: Any,
        notification_metrics: Any,
    ) -> None:
        """A fault must not take the worker down, because that would silently
        stop every notification while the API kept serving requests normally."""

        class BrokenResolver:
            def resolve(self, event: Any, rule: Any) -> Any:
                raise RuntimeError("database is on fire")

        subscriber = NotificationEventSubscriber(
            session_factory,
            recipients=BrokenResolver(),  # type: ignore[arg-type]
            publisher=event_publisher,
            metrics=notification_metrics,
        )
        subscriber.start()
        try:
            subscriber.handle(
                _event(
                    DomainEventType.CASE_CREATED,
                    topic=case_topic(uuid.uuid4()),
                    case_id=uuid.uuid4(),
                )
            )
            subscriber.stop(timeout=2.0)
        finally:
            subscriber.stop(timeout=0.0)

        assert subscriber.pending == 0
        assert notification_metrics.snapshot().failed >= 1
