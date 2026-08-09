"""Unit tests for ``services/email_delivery.py`` — the Email Delivery Service.

A real database session and a real template renderer, with the **provider**
replaced: a fake that records what it was handed and can be told to fail with any
:class:`~core.email.EmailFailureCode`. That is the only substitution, and it is
the right one — every decision this feature makes (what is eligible, whose
preference silences it, when to retry, when to give up) is on this side of the
provider boundary, and a fake mail server would only re-test the standard library.

The queue is a recorder rather than a thread, for the reason every other worker
test on this platform uses one: ``process`` is public and synchronous precisely so
a test can assert about a row instead of waiting to see.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from core.config import settings
from core.email import EmailFailureCode
from core.events import DomainEventType
from core.notifications import (
    EVENT_RULES,
    RULE_CASE_ASSIGNED,
    ChannelPreferenceUpdate,
    NotificationPreferenceKey,
    NotificationTarget,
    NotificationTargetType,
)
from models.email import EmailDeliveryStatus
from models.user import UserRole
from repositories.email import EmailDeliveryRepository
from repositories.notification import NotificationRepository
from services.email_delivery import EmailDeliveryService, EmailJob
from services.email_metrics import EmailSkipReason, InMemoryEmailMetrics
from services.email_provider import EmailSendResult, OutgoingEmail
from services.email_templates import get_email_template_renderer
from services.notification import NotificationService

CASE_CREATED = EVENT_RULES[DomainEventType.CASE_CREATED]
DOCUMENT_UPLOADED = EVENT_RULES[DomainEventType.DOCUMENT_UPLOADED]
REPORT_GENERATED = EVENT_RULES[DomainEventType.REPORT_GENERATED]
PASSWORD_RESET = EVENT_RULES[DomainEventType.USER_PASSWORD_RESET]


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


@dataclass
class FakeEmailProvider:
    """A provider that records what it was handed and fails on demand.

    Keeps the real contract — :meth:`send` never raises, and a refusal is a
    *result* rather than an exception — which is what makes a test that passes
    against it exercise the same error handling production uses.
    """

    name: str = "fake"
    available: bool = True
    failure: EmailFailureCode | None = None
    sent: list[OutgoingEmail] = field(default_factory=list)

    def is_available(self) -> bool:
        return self.available

    def send(self, message: OutgoingEmail) -> EmailSendResult:
        if self.failure is not None:
            return EmailSendResult.refused(provider=self.name, failure=self.failure)
        self.sent.append(message)
        return EmailSendResult.success(provider=self.name, duration_ms=4.0)


@dataclass
class RecordingQueue:
    """A queue that records rather than schedules.

    ``process`` is public and synchronous, so a test drives the work itself and
    never races a thread.
    """

    jobs: list[EmailJob] = field(default_factory=list)

    def enqueue(self, job: EmailJob) -> None:
        self.jobs.append(job)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _enable_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """Email is off by default on this platform — see ``EMAIL_ENABLED``."""
    monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "EMAIL_FROM_ADDRESS", "notifications@legal.example")
    monkeypatch.setattr(settings, "EMAIL_BASE_URL", "https://legal.example")


@pytest.fixture
def provider() -> FakeEmailProvider:
    return FakeEmailProvider()


@pytest.fixture
def queue() -> RecordingQueue:
    return RecordingQueue()


@pytest.fixture
def email_metrics() -> InMemoryEmailMetrics:
    return InMemoryEmailMetrics()


@pytest.fixture
def deliveries(
    db_session: Session,
    provider: FakeEmailProvider,
    queue: RecordingQueue,
    email_metrics: InMemoryEmailMetrics,
) -> EmailDeliveryService:
    return EmailDeliveryService(
        EmailDeliveryRepository(db_session),
        NotificationRepository(db_session),
        provider,
        get_email_template_renderer(),
        queue,
        metrics=email_metrics,
    )


@pytest.fixture
def notifications(
    db_session: Session, deliveries: EmailDeliveryService
) -> NotificationService:
    """The Notification Service with the email channel wired in, which is how the
    two actually meet in production."""
    return NotificationService(NotificationRepository(db_session), channels=[deliveries])


@pytest.fixture
def lawyer(make_user: Any) -> Any:
    return make_user(email="amina@firm.example", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: Any) -> Any:
    return make_user(email="karim@firm.example", role=UserRole.LAWYER)


def _stored(db_session: Session) -> list[Any]:
    from models.email import EmailDelivery

    return list(db_session.query(EmailDelivery).all())


def _aware(value: datetime) -> datetime:
    """Read a stored timestamp as UTC.

    SQLite returns naive datetimes for a ``TIMESTAMP WITH TIME ZONE`` column, so
    a comparison against an aware one raises rather than being slightly wrong —
    the same quirk :mod:`services.notification` and :mod:`services.email_delivery`
    each carry a helper for.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# What gets queued
# --------------------------------------------------------------------------- #


class TestEligibility:
    def test_a_supported_notification_is_queued(
        self,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        queue: RecordingQueue,
    ) -> None:
        case_id = uuid.uuid4()
        notifications.create(
            rule=RULE_CASE_ASSIGNED,
            recipient_ids=[lawyer.id],
            context={"case_number": "CASE-2026-0001"},
            case_id=case_id,
            target=NotificationTarget(NotificationTargetType.CASE, case_id),
        )

        rows = _stored(db_session)
        assert len(rows) == 1
        assert rows[0].status is EmailDeliveryStatus.PENDING
        assert rows[0].recipient_email == "amina@firm.example"
        assert rows[0].rule_key == "case.assigned"
        assert len(queue.jobs) == 1

    def test_an_excluded_notification_is_not(
        self,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        email_metrics: InMemoryEmailMetrics,
    ) -> None:
        """A document upload is an in-app notification and nothing else — the
        spec's "Events That Must NOT Generate Emails"."""
        notifications.create(rule=DOCUMENT_UPLOADED, recipient_ids=[lawyer.id])

        assert _stored(db_session) == []
        snapshot = email_metrics.snapshot()
        assert snapshot.skipped_by_reason == {
            EmailSkipReason.NOT_EMAIL_ELIGIBLE.value: 1
        }

    def test_the_address_is_snapshotted_onto_the_row(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        """A join would render the address the account has *today*, so a user who
        changed their email would rewrite the record of where mail was sent."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]

        lawyer.email = "moved@firm.example"
        db_session.commit()

        db_session.refresh(row)
        assert row.recipient_email == "amina@firm.example"

    def test_the_batch_costs_one_insert(
        self,
        notifications: NotificationService,
        lawyer: Any,
        other_lawyer: Any,
        db_session: Session,
    ) -> None:
        """The spec's "support batch delivery" at the place it pays: an event
        about a case queues for everyone party to it."""
        notifications.create(
            rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id, other_lawyer.id]
        )
        assert len(_stored(db_session)) == 2


class TestPreferences:
    def test_a_switched_off_email_channel_queues_nothing(
        self,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        email_metrics: InMemoryEmailMetrics,
    ) -> None:
        """The spec's "if a user disables email delivery for a supported
        notification type, no email should be sent"."""
        notifications.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(email=False)},
            actor=lawyer,
        )
        created = notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])

        assert created  # the in-app notification is untouched
        assert _stored(db_session) == []
        assert (
            email_metrics.snapshot().skipped_by_reason[
                EmailSkipReason.SUPPRESSED_BY_PREFERENCE.value
            ]
            == 1
        )

    def test_silencing_email_leaves_the_feed_alone(
        self, notifications: NotificationService, lawyer: Any
    ) -> None:
        """Which is the setting people actually want, and the whole reason the
        preference is a column per channel rather than one boolean."""
        notifications.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(email=False)},
            actor=lawyer,
        )
        assert notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])

    def test_silencing_in_app_stops_the_email_too(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        """Because there is no notification to deliver: this channel only ever
        narrows what the Notification Service already created."""
        notifications.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(in_app=False)},
            actor=lawyer,
        )
        assert notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id]) == []
        assert _stored(db_session) == []

    def test_one_persons_preference_does_not_silence_another(
        self,
        notifications: NotificationService,
        lawyer: Any,
        other_lawyer: Any,
        db_session: Session,
    ) -> None:
        notifications.update_preferences(
            {NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(email=False)},
            actor=lawyer,
        )
        notifications.create(
            rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id, other_lawyer.id]
        )
        rows = _stored(db_session)
        assert [row.recipient_id for row in rows] == [other_lawyer.id]

    def test_a_preference_silences_only_its_own_rule(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        notifications.update_preferences(
            {
                NotificationPreferenceKey.AI_REPORT_COMPLETION: ChannelPreferenceUpdate(
                    email=False
                )
            },
            actor=lawyer,
        )
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        notifications.create(
            rule=REPORT_GENERATED,
            recipient_ids=[lawyer.id],
            target=NotificationTarget(NotificationTargetType.REPORT, uuid.uuid4()),
        )
        assert [row.rule_key for row in _stored(db_session)] == ["case.assigned"]


class TestRecipients:
    def test_an_account_with_no_usable_address_is_skipped(
        self,
        notifications: NotificationService,
        make_user: Any,
        db_session: Session,
        email_metrics: InMemoryEmailMetrics,
    ) -> None:
        broken = make_user(email="nobody@firm.example", role=UserRole.LAWYER)
        broken.email = "   "
        db_session.commit()

        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[broken.id])
        assert _stored(db_session) == []
        assert (
            email_metrics.snapshot().skipped_by_reason[EmailSkipReason.NO_ADDRESS.value]
            == 1
        )

    def test_a_deactivated_account_is_never_written_to(
        self, notifications: NotificationService, make_user: Any, db_session: Session
    ) -> None:
        """Mailing a link into a platform somebody can no longer sign in to."""
        gone = make_user(email="gone@firm.example", role=UserRole.LAWYER, is_active=False)
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[gone.id])
        assert _stored(db_session) == []


class TestDuplicates:
    def test_one_notification_is_one_email(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """Whatever re-dispatches it — a retried worker, a second process, a
        restart."""
        created = notifications.create(
            rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id]
        )
        deliveries.dispatch(created)
        deliveries.dispatch(created)

        assert len(_stored(db_session)) == 1

    def test_a_genuine_repeat_is_a_second_email(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        """A case assigned twice is two notifications with two identities, so the
        guard cannot suppress it."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        notifications.create(
            rule=RULE_CASE_ASSIGNED,
            recipient_ids=[lawyer.id],
            case_id=uuid.uuid4(),
            discriminator=str(uuid.uuid4()),
        )
        assert len(_stored(db_session)) == 2


class TestSwitches:
    def test_nothing_is_queued_when_the_channel_is_off(
        self,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
        assert notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        assert _stored(db_session) == []

    def test_nothing_is_queued_when_no_provider_is_configured(
        self,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
        email_metrics: InMemoryEmailMetrics,
    ) -> None:
        """Rather than building a backlog whose only outcome is a burst of very
        old mail the day somebody configures a relay."""
        provider.available = False
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])

        assert _stored(db_session) == []
        assert (
            email_metrics.snapshot().skipped_by_reason[
                EmailSkipReason.PROVIDER_UNAVAILABLE.value
            ]
            == 1
        )


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


class TestSending:
    def _queue_one(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> Any:
        case_id = uuid.uuid4()
        notifications.create(
            rule=RULE_CASE_ASSIGNED,
            recipient_ids=[lawyer.id],
            context={"case_number": "CASE-2026-0001"},
            case_id=case_id,
            target=NotificationTarget(NotificationTargetType.CASE, case_id),
        )
        return _stored(db_session)[0]

    def test_a_delivery_reaches_sent(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        row = self._queue_one(notifications, lawyer, db_session)
        assert deliveries.process(EmailJob(delivery_id=row.id)) is True

        db_session.refresh(row)
        assert row.status is EmailDeliveryStatus.SENT
        assert row.sent_at is not None
        assert row.attempts == 1
        assert row.error_code is None
        assert row.provider == "fake"

    def test_the_message_carries_both_bodies_and_the_notification_wording(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        row = self._queue_one(notifications, lawyer, db_session)
        deliveries.process(EmailJob(delivery_id=row.id))

        message = provider.sent[0]
        assert message.to_address == "amina@firm.example"
        assert message.subject == "Dossier attribué"
        assert "CASE-2026-0001" in message.text_body
        assert "CASE-2026-0001" in message.html_body
        assert message.html_body.startswith("<html")

    def test_a_link_points_at_the_case(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        row = self._queue_one(notifications, lawyer, db_session)
        deliveries.process(EmailJob(delivery_id=row.id))
        assert "https://legal.example/cases/" in provider.sent[0].html_body

    def test_a_security_email_is_rendered_by_its_own_template(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        notifications.create(
            rule=PASSWORD_RESET,
            recipient_ids=[lawyer.id],
            target=NotificationTarget(NotificationTargetType.ACCOUNT),
        )
        row = _stored(db_session)[0]
        assert row.template == "security"

        deliveries.process(EmailJob(delivery_id=row.id))
        assert "<a " not in provider.sent[0].html_body

    def test_a_claim_is_won_once(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """A sweeper re-queueing beside a live dispatch genuinely produces two
        jobs for one row; the conditional UPDATE means one of them sends."""
        row = self._queue_one(notifications, lawyer, db_session)
        assert deliveries.process(EmailJob(delivery_id=row.id)) is True
        assert deliveries.process(EmailJob(delivery_id=row.id)) is False

    def test_a_deleted_notification_leaves_nothing_to_send(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """The delivery cascades away with the notification it carries, so what is
        asserted is that a job for a row that no longer exists is a quiet no-op
        rather than an exception on a worker thread."""
        from models.notification import Notification

        row = self._queue_one(notifications, lawyer, db_session)
        db_session.query(Notification).filter(
            Notification.id == row.notification_id
        ).delete()
        db_session.commit()

        assert deliveries.process(EmailJob(delivery_id=row.id)) is False
        assert provider.sent == []

    def test_an_archived_notification_fails_permanently(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """Every read in `repositories/notification.py` excludes archived rows, so
        the delivery survives and its notification becomes unreachable. Permanent
        rather than retried: it is not going to come back, and four more attempts
        would be four more renders of something that is gone."""
        from models.notification import Notification

        row = self._queue_one(notifications, lawyer, db_session)
        db_session.query(Notification).filter(
            Notification.id == row.notification_id
        ).update({Notification.archived_at: datetime.now(UTC)})
        db_session.commit()

        assert deliveries.process(EmailJob(delivery_id=row.id)) is False
        db_session.refresh(row)
        assert row.status is EmailDeliveryStatus.FAILED
        assert provider.sent == []


class TestFailuresAndRetries:
    def _queue_one(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> Any:
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        return _stored(db_session)[0]

    def test_a_transient_failure_is_rescheduled(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
        email_metrics: InMemoryEmailMetrics,
    ) -> None:
        provider.failure = EmailFailureCode.TIMEOUT
        row = self._queue_one(notifications, lawyer, db_session)

        assert deliveries.process(EmailJob(delivery_id=row.id)) is False
        db_session.refresh(row)

        assert row.status is EmailDeliveryStatus.PENDING
        assert row.attempts == 1
        assert row.error_code == "timeout"
        assert row.next_attempt_at is not None
        # Read as UTC: SQLite returns naive datetimes for a `TIMESTAMP WITH TIME
        # ZONE` column, which is the same quirk `services/notification.py` and
        # `services/email_delivery.py` both carry a helper for.
        assert _aware(row.next_attempt_at) > datetime.now(UTC)
        assert email_metrics.snapshot().retried == 1

    def test_a_permanent_failure_is_not(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
        email_metrics: InMemoryEmailMetrics,
    ) -> None:
        """Retrying a rejected credential is how an account gets locked."""
        provider.failure = EmailFailureCode.AUTHENTICATION_FAILED
        row = self._queue_one(notifications, lawyer, db_session)

        deliveries.process(EmailJob(delivery_id=row.id))
        db_session.refresh(row)

        assert row.status is EmailDeliveryStatus.FAILED
        assert row.error_code == "authentication_failed"
        assert email_metrics.snapshot().retried == 0
        assert email_metrics.snapshot().failed == 1

    def test_retries_are_exhausted_and_then_it_fails(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "EMAIL_MAX_ATTEMPTS", 3)
        provider.failure = EmailFailureCode.THROTTLED
        row = self._queue_one(notifications, lawyer, db_session)

        for _ in range(3):
            # The sweeper's job, done by hand: clear the schedule so the next
            # attempt is due.
            row.next_attempt_at = None
            db_session.commit()
            deliveries.process(EmailJob(delivery_id=row.id))
            db_session.refresh(row)

        assert row.attempts == 3
        assert row.status is EmailDeliveryStatus.FAILED

    def test_the_notification_is_untouched_by_a_failure(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """"Failures should never interrupt application functionality": this
        service writes to one table, so there is nothing else it could damage."""
        provider.failure = EmailFailureCode.RECIPIENT_REFUSED
        row = self._queue_one(notifications, lawyer, db_session)
        deliveries.process(EmailJob(delivery_id=row.id))

        stored = NotificationRepository(db_session).get(
            row.notification_id, recipient_id=lawyer.id
        )
        assert stored is not None
        assert stored.read_at is None

    def test_a_dispatch_failure_never_reaches_the_caller(
        self,
        notifications: NotificationService,
        deliveries: EmailDeliveryService,
        lawyer: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole of "failures should be isolated": the notification was
        already committed by the time a channel is offered it."""

        def explode(_batch: Any) -> None:
            raise RuntimeError("the mail module is on fire")

        monkeypatch.setattr(deliveries, "_queue_batch", explode)
        assert notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])


# --------------------------------------------------------------------------- #
# The sweeper
# --------------------------------------------------------------------------- #


class TestSweep:
    def test_a_due_retry_is_requeued(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        queue: RecordingQueue,
    ) -> None:
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        row.next_attempt_at = datetime.now(UTC) - timedelta(minutes=1)
        db_session.commit()
        queue.jobs.clear()

        assert deliveries.sweep() == 1
        assert queue.jobs[0].delivery_id == row.id

    def test_a_future_retry_is_left_alone(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        queue: RecordingQueue,
    ) -> None:
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        row.next_attempt_at = datetime.now(UTC) + timedelta(hours=1)
        db_session.commit()
        queue.jobs.clear()

        assert deliveries.sweep() == 0
        assert queue.jobs == []

    def test_a_first_attempt_with_no_schedule_is_due_immediately(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        queue: RecordingQueue,
    ) -> None:
        """Which is also the startup recovery: a process stopped with deliveries
        queued leaves rows whose schedule lived only in its memory."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        queue.jobs.clear()
        assert deliveries.sweep() == 1

    def test_a_stranded_send_is_reclaimed(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        queue: RecordingQueue,
    ) -> None:
        """`sending` is the one state no other worker will claim, so a process
        that died mid-send would strand it forever."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        row.status = EmailDeliveryStatus.SENDING
        row.started_at = datetime.now(UTC) - timedelta(hours=2)
        db_session.commit()
        queue.jobs.clear()

        assert deliveries.sweep() == 1
        db_session.refresh(row)
        assert row.status is EmailDeliveryStatus.PENDING

    def test_a_send_in_flight_is_not_reclaimed(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """A send that is merely slow must never be reclaimed underneath the
        worker still doing it — that is how a message goes out twice."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        row.status = EmailDeliveryStatus.SENDING
        row.started_at = datetime.now(UTC)
        db_session.commit()

        deliveries.sweep()
        db_session.refresh(row)
        assert row.status is EmailDeliveryStatus.SENDING

    def test_a_sent_delivery_is_never_requeued(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        queue: RecordingQueue,
    ) -> None:
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        deliveries.process(EmailJob(delivery_id=row.id))
        queue.jobs.clear()

        assert deliveries.sweep() == 0

    def test_the_batch_is_bounded(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        make_user: Any,
        db_session: Session,
        queue: RecordingQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A relay down overnight leaves a backlog rather than a page, and loading
        all of it to re-queue it is how a recovery becomes its own outage."""
        monkeypatch.setattr(settings, "EMAIL_RETRY_BATCH_SIZE", 2)
        people = [
            make_user(email=f"person{index}@firm.example", role=UserRole.LAWYER)
            for index in range(5)
        ]
        notifications.create(
            rule=RULE_CASE_ASSIGNED, recipient_ids=[person.id for person in people]
        )
        queue.jobs.clear()

        assert deliveries.sweep() == 2


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_the_figures_come_from_both_halves(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        deliveries.process(EmailJob(delivery_id=row.id))

        metrics = deliveries.metrics()
        # SQL: exact, and survives a restart.
        assert metrics.statistics.sent == 1
        assert metrics.statistics.attempts == 1
        assert metrics.statistics.recipients == 1
        # In-process: carries a `since`.
        assert metrics.counters.sent == 1
        assert metrics.counters.queued == 1
        assert metrics.counters.sent_by_rule == {"case.assigned": 1}
        assert metrics.counters.average_delivery_latency_ms is not None

    def test_a_failure_is_visible_in_both(
        self,
        deliveries: EmailDeliveryService,
        notifications: NotificationService,
        provider: FakeEmailProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        provider.failure = EmailFailureCode.RECIPIENT_REFUSED
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        deliveries.process(EmailJob(delivery_id=_stored(db_session)[0].id))

        metrics = deliveries.metrics()
        assert metrics.statistics.failed == 1
        assert metrics.statistics.by_failure_code == {"recipient_refused": 1}
        assert metrics.counters.failures_by_code == {"recipient_refused": 1}

    def test_availability_is_reported(
        self, deliveries: EmailDeliveryService, provider: FakeEmailProvider
    ) -> None:
        assert deliveries.metrics().provider_available is True
        provider.available = False
        assert deliveries.metrics().provider_available is False
        assert deliveries.metrics().templates_available is True


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


class TestWiring:
    """That the application actually supplies the channel.

    Every service on this platform defaults its optional collaborators to a
    no-op — a null queue, a null recorder, a null timeline — so "the default is
    nothing" is one forgotten line away from being the production behaviour.
    These are the tests that make that a failing build rather than a quiet gap,
    exactly as the OCR, indexing, and report features each have one.
    """

    def test_the_request_path_wires_the_email_channel(
        self, db_session: Session, provider: FakeEmailProvider, queue: RecordingQueue
    ) -> None:
        from api.deps import get_notification_channels

        service = EmailDeliveryService(
            EmailDeliveryRepository(db_session),
            NotificationRepository(db_session),
            provider,
            get_email_template_renderer(),
            queue,
        )
        assert get_notification_channels(service) == [service]

    def test_the_worker_path_wires_it_too(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Most notifications are created on the notification worker's thread,
        which has no request to resolve a dependency from — so the two paths are
        wired separately and both have to be right."""
        from services.email_delivery import EmailDeliveryService as Service
        from services.notification_events import _delivery_channels

        channels = _delivery_channels(db_session)
        assert len(channels) == 1
        assert isinstance(channels[0], Service)

    def test_no_channel_is_built_when_the_feature_is_off(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a deployment with `EMAIL_ENABLED=false` does not construct a
        delivery service per event."""
        from services.notification_events import _delivery_channels

        monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
        assert _delivery_channels(db_session) == ()

    def test_a_service_with_no_channels_still_creates_notifications(
        self, db_session: Session, lawyer: Any
    ) -> None:
        """Nothing depends on the channel in either direction: the in-app feed is
        exactly as it was before this feature existed."""
        service = NotificationService(NotificationRepository(db_session))
        assert service.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
