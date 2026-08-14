"""Unit tests for ``services/whatsapp_delivery.py`` — the WhatsApp Delivery Service.

A real database session, the real descriptors, and the **provider** replaced: a
fake that records what it was handed and can be told to fail with any
:class:`~core.whatsapp.WhatsAppFailureCode`. That is the only substitution, and it
is the right one — every decision this feature makes (what is eligible, whose
preference silences it, whose number it is willing to use, when to retry, when to
give up) is on this side of the provider boundary, and a fake Cloud API would only
re-test :mod:`urllib`.

The queue is a recorder rather than a thread, for the reason every other worker
test on this platform uses one: ``process`` is public and synchronous precisely so
a test can assert about a row instead of waiting to see.

**The Notification Service is wired in for real**, which is how the two actually
meet in production and is what makes the boundary assertions meaningful: these
tests create *notifications* and then assert about *deliveries*, so a change that
let this channel see an event or widen an audience would fail here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from core.config import settings
from core.events import DomainEventType
from core.localization import default_language
from core.notifications import (
    EVENT_RULES,
    RULE_CASE_ASSIGNED,
    RULE_HEARING_UPDATED,
    ChannelPreferenceUpdate,
    NotificationCategory,
    NotificationPreferenceKey,
    NotificationTarget,
    NotificationTargetType,
    render_notification,
)
from core.whatsapp import WhatsAppFailureCode, provider_language_code
from models.user import UserRole
from models.whatsapp import WhatsAppDeliveryStatus
from repositories.notification import NotificationRepository
from repositories.whatsapp import WhatsAppDeliveryRepository
from services.notification import NotificationService
from services.whatsapp_delivery import WhatsAppDeliveryService, WhatsAppJob
from services.whatsapp_metrics import InMemoryWhatsAppMetrics, WhatsAppSkipReason
from services.whatsapp_provider import OutgoingWhatsAppMessage, WhatsAppSendResult
from services.whatsapp_templates import get_whatsapp_template_renderer

DOCUMENT_UPLOADED = EVENT_RULES[DomainEventType.DOCUMENT_UPLOADED]
REPORT_GENERATED = EVENT_RULES[DomainEventType.REPORT_GENERATED]
PASSWORD_RESET = EVENT_RULES[DomainEventType.USER_PASSWORD_RESET]


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


@dataclass
class FakeWhatsAppProvider:
    """A provider that records what it was handed and fails on demand.

    Keeps the real contract — :meth:`send` never raises, and a refusal is a
    *result* rather than an exception — which is what makes a test that passes
    against it exercise the same error handling production uses.
    """

    name: str = "fake"
    available: bool = True
    failure: WhatsAppFailureCode | None = None
    sent: list[OutgoingWhatsAppMessage] = field(default_factory=list)

    def is_available(self) -> bool:
        return self.available

    def configuration_errors(self) -> list[str]:
        return [] if self.available else ["WHATSAPP_ACCESS_TOKEN"]

    def send(self, message: OutgoingWhatsAppMessage) -> WhatsAppSendResult:
        if self.failure is not None:
            return WhatsAppSendResult.refused(provider=self.name, failure=self.failure)
        self.sent.append(message)
        return WhatsAppSendResult.success(
            provider=self.name, duration_ms=6.0, message_id="wamid.TEST"
        )


@dataclass
class RecordingQueue:
    """A queue that records rather than schedules."""

    jobs: list[WhatsAppJob] = field(default_factory=list)

    def enqueue(self, job: WhatsAppJob) -> None:
        self.jobs.append(job)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _enable_whatsapp(monkeypatch: pytest.MonkeyPatch) -> None:
    """WhatsApp is off by default on this platform — see ``WHATSAPP_ENABLED``."""
    monkeypatch.setattr(settings, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_BASE_URL", "https://legal.example")
    monkeypatch.setattr(settings, "WHATSAPP_DEFAULT_COUNTRY_CODE", None)


@pytest.fixture
def provider() -> FakeWhatsAppProvider:
    return FakeWhatsAppProvider()


@pytest.fixture
def queue() -> RecordingQueue:
    return RecordingQueue()


@pytest.fixture
def whatsapp_metrics() -> InMemoryWhatsAppMetrics:
    return InMemoryWhatsAppMetrics()


@pytest.fixture
def deliveries(
    db_session: Session,
    provider: FakeWhatsAppProvider,
    queue: RecordingQueue,
    whatsapp_metrics: InMemoryWhatsAppMetrics,
) -> WhatsAppDeliveryService:
    return WhatsAppDeliveryService(
        WhatsAppDeliveryRepository(db_session),
        NotificationRepository(db_session),
        provider,
        get_whatsapp_template_renderer(),
        queue,
        metrics=whatsapp_metrics,
    )


@pytest.fixture
def notifications(
    db_session: Session, deliveries: WhatsAppDeliveryService
) -> NotificationService:
    """The Notification Service with the WhatsApp channel wired in, which is how
    the two actually meet in production."""
    return NotificationService(NotificationRepository(db_session), channels=[deliveries])


@pytest.fixture
def lawyer(make_user: Any) -> Any:
    return make_user(
        email="amina@firm.example", role=UserRole.LAWYER, phone="+212612345678"
    )


@pytest.fixture
def other_lawyer(make_user: Any) -> Any:
    return make_user(
        email="karim@firm.example", role=UserRole.LAWYER, phone="+212698765432"
    )


def _stored(db_session: Session) -> list[Any]:
    from models.whatsapp import WhatsAppDelivery

    return list(db_session.query(WhatsAppDelivery).all())


def _aware(value: datetime) -> datetime:
    """Read a stored timestamp as UTC.

    SQLite returns naive datetimes for a ``TIMESTAMP WITH TIME ZONE`` column, so a
    comparison against an aware one raises rather than being slightly wrong.
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
        assert rows[0].status is WhatsAppDeliveryStatus.PENDING
        assert rows[0].rule_key == "case.assigned"
        assert rows[0].template == "notification"
        assert len(queue.jobs) == 1

    def test_an_excluded_notification_is_not(
        self,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        whatsapp_metrics: InMemoryWhatsAppMetrics,
    ) -> None:
        """A document upload is an in-app notification and nothing else — the
        spec's "Events That Must NOT Generate WhatsApp Messages"."""
        notifications.create(rule=DOCUMENT_UPLOADED, recipient_ids=[lawyer.id])

        assert _stored(db_session) == []
        assert whatsapp_metrics.snapshot().skipped_by_reason == {
            WhatsAppSkipReason.NOT_WHATSAPP_ELIGIBLE.value: 1
        }

    def test_the_number_is_normalized_and_snapshotted_onto_the_row(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        """Normalized because ``users.phone`` is a free-text display field;
        snapshotted because a join would render the number the account has *today*
        and rewrite the record of where messages were actually sent."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        assert row.recipient_phone == "212612345678"

        lawyer.phone = "+33612345678"
        db_session.commit()

        db_session.refresh(row)
        assert row.recipient_phone == "212612345678"

    def test_the_batch_costs_one_insert(
        self,
        notifications: NotificationService,
        lawyer: Any,
        other_lawyer: Any,
        db_session: Session,
    ) -> None:
        """The spec's "support batch delivery" at the place it pays."""
        notifications.create(
            rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id, other_lawyer.id]
        )
        assert len(_stored(db_session)) == 2

    def test_a_hearing_update_travels_on_this_channel(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        """The spec's "Hearing Rescheduled" and "Urgent Hearing Update", which are
        both what a change to a case's court-facing fields is."""
        notifications.create(
            rule=RULE_HEARING_UPDATED,
            recipient_ids=[lawyer.id],
            context={"case_number": "CASE-2026-0001"},
        )
        assert [row.rule_key for row in _stored(db_session)] == ["hearing.updated"]


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class TestPreferences:
    def test_a_switched_off_whatsapp_channel_queues_nothing(
        self,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        whatsapp_metrics: InMemoryWhatsAppMetrics,
    ) -> None:
        """The spec's *"users should be able to enable or disable WhatsApp
        delivery"*."""
        notifications.update_preferences(
            {
                NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(
                    whatsapp=False
                )
            },
            actor=lawyer,
        )
        created = notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])

        assert created  # the in-app notification is untouched
        assert _stored(db_session) == []
        assert (
            whatsapp_metrics.snapshot().skipped_by_reason[
                WhatsAppSkipReason.SUPPRESSED_BY_PREFERENCE.value
            ]
            == 1
        )

    def test_it_is_independent_of_the_other_channels(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        """The spec's *"independently from In-App notifications, Email
        notifications"*, and the whole reason the preference is a column per
        channel rather than one boolean."""
        notifications.update_preferences(
            {
                NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(
                    whatsapp=False
                )
            },
            actor=lawyer,
        )
        stored = notifications.preferences(actor=lawyer)[
            NotificationPreferenceKey.CASE_UPDATES
        ]

        assert stored.whatsapp is False
        assert stored.in_app is True
        assert stored.email is True
        assert notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        assert _stored(db_session) == []

    def test_silencing_in_app_stops_the_message_too(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        """Because there is no notification to deliver: this channel only ever
        narrows what the Notification Service already created."""
        notifications.update_preferences(
            {
                NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(
                    in_app=False
                )
            },
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
            {
                NotificationPreferenceKey.CASE_UPDATES: ChannelPreferenceUpdate(
                    whatsapp=False
                )
            },
            actor=lawyer,
        )
        notifications.create(
            rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id, other_lawyer.id]
        )
        assert [row.recipient_id for row in _stored(db_session)] == [other_lawyer.id]

    def test_a_preference_silences_only_its_own_rule(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> None:
        notifications.update_preferences(
            {
                NotificationPreferenceKey.AI_REPORT_COMPLETION: ChannelPreferenceUpdate(
                    whatsapp=False
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


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #


class TestRecipients:
    def test_an_account_with_no_number_is_skipped(
        self,
        notifications: NotificationService,
        make_user: Any,
        db_session: Session,
        whatsapp_metrics: InMemoryWhatsAppMetrics,
    ) -> None:
        """`users.phone` is optional, so this is the *expected* outcome for much
        of the platform rather than a fault — which is why it is a skip reason
        and not a failure."""
        no_phone = make_user(email="nophone@firm.example", role=UserRole.LAWYER)

        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[no_phone.id])
        assert _stored(db_session) == []
        assert (
            whatsapp_metrics.snapshot().skipped_by_reason[
                WhatsAppSkipReason.NO_PHONE_NUMBER.value
            ]
            == 1
        )

    def test_an_ambiguous_number_is_refused_rather_than_guessed_at(
        self, notifications: NotificationService, make_user: Any, db_session: Session
    ) -> None:
        """One message not sent is a failure this channel may have; a legal
        notification delivered to a stranger is not."""
        national = make_user(
            email="national@firm.example", role=UserRole.LAWYER, phone="0612345678"
        )
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[national.id])
        assert _stored(db_session) == []

    def test_a_configured_country_code_makes_it_usable(
        self,
        notifications: NotificationService,
        make_user: Any,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "WHATSAPP_DEFAULT_COUNTRY_CODE", "212")
        national = make_user(
            email="national@firm.example", role=UserRole.LAWYER, phone="0612345678"
        )
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[national.id])
        assert [row.recipient_phone for row in _stored(db_session)] == ["212612345678"]

    def test_a_deactivated_account_is_never_messaged(
        self, notifications: NotificationService, make_user: Any, db_session: Session
    ) -> None:
        """Messaging a link into a platform somebody can no longer sign in to."""
        gone = make_user(
            email="gone@firm.example",
            role=UserRole.LAWYER,
            phone="+212611111111",
            is_active=False,
        )
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[gone.id])
        assert _stored(db_session) == []


# --------------------------------------------------------------------------- #
# Duplicates and switches
# --------------------------------------------------------------------------- #


class TestDuplicates:
    def test_one_notification_is_one_message(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """Whatever re-dispatches it — a retried worker, a second process, a
        restart. Two phone alerts about the same hearing leave a reader unable to
        tell which one is current."""
        created = notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        deliveries.dispatch(created)
        deliveries.dispatch(created)

        assert len(_stored(db_session)) == 1

    def test_a_genuine_repeat_is_a_second_message(
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
        monkeypatch.setattr(settings, "WHATSAPP_ENABLED", False)
        assert notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        assert _stored(db_session) == []

    def test_nothing_is_queued_when_no_provider_is_configured(
        self,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
        lawyer: Any,
        db_session: Session,
        whatsapp_metrics: InMemoryWhatsAppMetrics,
    ) -> None:
        """Rather than building a backlog whose only outcome is a burst of very
        old notices the day somebody finishes the configuration."""
        provider.available = False
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])

        assert _stored(db_session) == []
        assert (
            whatsapp_metrics.snapshot().skipped_by_reason[
                WhatsAppSkipReason.PROVIDER_UNAVAILABLE.value
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

    def test_a_delivery_reaches_delivered(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        row = self._queue_one(notifications, lawyer, db_session)
        assert deliveries.process(WhatsAppJob(delivery_id=row.id)) is True

        db_session.refresh(row)
        assert row.status is WhatsAppDeliveryStatus.DELIVERED
        assert row.delivered_at is not None
        assert row.attempts == 1
        assert row.error_code is None
        assert row.provider == "fake"

    def test_the_provider_message_id_is_recorded(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """The only handle that correlates this row with anything on Meta's side —
        a support case, a Business Manager log, and the delivery-receipt webhook a
        later feature would consume."""
        row = self._queue_one(notifications, lawyer, db_session)
        deliveries.process(WhatsAppJob(delivery_id=row.id))

        db_session.refresh(row)
        assert row.provider_message_id == "wamid.TEST"

    def test_the_message_is_a_template_with_the_notifications_own_wording(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """Not a copy of it, and not a sentence held in Meta's console: the
        parameters *are* `core/notifications.py`'s rendered title and message."""
        row = self._queue_one(notifications, lawyer, db_session)
        deliveries.process(WhatsAppJob(delivery_id=row.id))

        message = provider.sent[0]
        assert message.to_number == "212612345678"
        assert message.template_name == "notification"
        # The provider tag follows the *recipient's* language, which decides
        # which approved template Meta is asked for; this account has chosen
        # nothing, so it is the application default's.
        assert message.language_code == provider_language_code(default_language())
        assert message.parameters[1] == render_notification(
            rule_key="case.assigned",
            category=NotificationCategory.CASE,
            language=default_language(),
        ).title
        assert "CASE-2026-0001" in message.parameters[2]

    def test_a_link_points_at_the_case(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        row = self._queue_one(notifications, lawyer, db_session)
        deliveries.process(WhatsAppJob(delivery_id=row.id))
        assert "https://legal.example/cases/" in provider.sent[0].parameters[3]

    def test_a_security_message_is_rendered_by_its_own_template_and_offers_no_link(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
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

        deliveries.process(WhatsAppJob(delivery_id=row.id))
        assert not any("http" in value for value in provider.sent[0].parameters)

    def test_a_claim_is_won_once(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """A sweeper re-queueing beside a live dispatch genuinely produces two jobs
        for one row; the conditional UPDATE means one of them sends."""
        row = self._queue_one(notifications, lawyer, db_session)
        assert deliveries.process(WhatsAppJob(delivery_id=row.id)) is True
        assert deliveries.process(WhatsAppJob(delivery_id=row.id)) is False

    def test_a_deleted_notification_leaves_nothing_to_send(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
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

        assert deliveries.process(WhatsAppJob(delivery_id=row.id)) is False
        assert provider.sent == []

    def test_an_account_deactivated_after_queueing_is_not_messaged(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """A suspended user should not be messaged a link into a platform they can
        no longer sign in to."""
        from models.user import UserStatus

        row = self._queue_one(notifications, lawyer, db_session)
        lawyer.status = UserStatus.SUSPENDED
        db_session.commit()

        assert deliveries.process(WhatsAppJob(delivery_id=row.id)) is False
        db_session.refresh(row)
        assert row.status is WhatsAppDeliveryStatus.FAILED
        assert row.error_code == WhatsAppFailureCode.INVALID_RECIPIENT.value
        assert provider.sent == []


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


class TestRetry:
    def _queue_one(
        self, notifications: NotificationService, lawyer: Any, db_session: Session
    ) -> Any:
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        return _stored(db_session)[0]

    def test_a_transient_failure_returns_to_the_queue_with_a_backoff(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
        lawyer: Any,
        db_session: Session,
        whatsapp_metrics: InMemoryWhatsAppMetrics,
    ) -> None:
        """A rate limit is the failure this channel meets most, and it is the
        textbook transient one."""
        row = self._queue_one(notifications, lawyer, db_session)
        provider.failure = WhatsAppFailureCode.THROTTLED

        assert deliveries.process(WhatsAppJob(delivery_id=row.id)) is False
        db_session.refresh(row)

        assert row.status is WhatsAppDeliveryStatus.PENDING
        assert row.attempts == 1
        assert row.error_code == WhatsAppFailureCode.THROTTLED.value
        assert row.next_attempt_at is not None
        assert _aware(row.next_attempt_at) > datetime.now(UTC)
        assert whatsapp_metrics.snapshot().retried == 1

    @pytest.mark.parametrize(
        "failure",
        [
            WhatsAppFailureCode.AUTHENTICATION_FAILED,
            WhatsAppFailureCode.RECIPIENT_REFUSED,
            WhatsAppFailureCode.TEMPLATE_REJECTED,
            WhatsAppFailureCode.MESSAGE_REFUSED,
        ],
    )
    def test_a_permanent_failure_is_not_retried(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
        lawyer: Any,
        db_session: Session,
        failure: WhatsAppFailureCode,
    ) -> None:
        row = self._queue_one(notifications, lawyer, db_session)
        provider.failure = failure

        deliveries.process(WhatsAppJob(delivery_id=row.id))
        db_session.refresh(row)

        assert row.status is WhatsAppDeliveryStatus.FAILED
        assert row.error_code == failure.value
        assert row.next_attempt_at is None

    def test_retries_are_exhausted_and_then_it_gives_up(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
        lawyer: Any,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "WHATSAPP_MAX_ATTEMPTS", 2)
        row = self._queue_one(notifications, lawyer, db_session)
        provider.failure = WhatsAppFailureCode.TIMEOUT

        deliveries.process(WhatsAppJob(delivery_id=row.id))
        db_session.refresh(row)
        assert row.status is WhatsAppDeliveryStatus.PENDING

        row.next_attempt_at = None
        db_session.commit()
        deliveries.process(WhatsAppJob(delivery_id=row.id))
        db_session.refresh(row)

        assert row.status is WhatsAppDeliveryStatus.FAILED
        assert row.attempts == 2

    def test_a_failure_leaves_the_notification_untouched(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        provider: FakeWhatsAppProvider,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """The spec's *"failures should never interrupt application
        functionality"*, and it is structural: this service writes to one table."""
        from models.notification import Notification

        row = self._queue_one(notifications, lawyer, db_session)
        provider.failure = WhatsAppFailureCode.RECIPIENT_REFUSED
        deliveries.process(WhatsAppJob(delivery_id=row.id))

        notification = db_session.get(Notification, row.notification_id)
        assert notification is not None
        assert notification.read_at is None


class TestSweep:
    def test_a_due_delivery_is_re_queued(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        queue: RecordingQueue,
    ) -> None:
        """The retry mechanism's other half: no worker ever sleeps out a backoff,
        so something has to pick the row up when its time arrives."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        row.next_attempt_at = datetime.now(UTC) - timedelta(minutes=5)
        db_session.commit()
        queue.jobs.clear()

        assert deliveries.sweep() == 1
        assert [job.delivery_id for job in queue.jobs] == [row.id]

    def test_a_delivery_not_yet_due_is_left_alone(
        self,
        deliveries: WhatsAppDeliveryService,
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

    def test_a_stranded_send_is_reclaimed(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """`sending` is the one state no other worker will claim, so without this
        a delivery interrupted by a deployment would sit there forever."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        row.status = WhatsAppDeliveryStatus.SENDING
        row.started_at = datetime.now(UTC) - timedelta(hours=2)
        db_session.commit()

        deliveries.sweep()
        db_session.refresh(row)
        assert row.status is WhatsAppDeliveryStatus.PENDING

    def test_a_delivered_message_is_never_re_queued(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        queue: RecordingQueue,
    ) -> None:
        """The one mistake this feature must not make."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        deliveries.process(WhatsAppJob(delivery_id=row.id))
        queue.jobs.clear()

        assert deliveries.sweep() == 0

    def test_the_sweep_does_nothing_when_the_channel_is_off(
        self,
        deliveries: WhatsAppDeliveryService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "WHATSAPP_ENABLED", False)
        assert deliveries.sweep() == 0


# --------------------------------------------------------------------------- #
# The boundary, and what never leaves it
# --------------------------------------------------------------------------- #


class TestBoundary:
    def test_dispatch_never_raises(
        self, deliveries: WhatsAppDeliveryService, lawyer: Any
    ) -> None:
        """The `NotificationDispatcher` contract: it runs on the notification
        worker's thread after that batch has already committed."""

        class _Exploding:
            id = uuid.uuid4()
            recipient_id = lawyer.id
            rule_key = RULE_CASE_ASSIGNED.key

            def __getattr__(self, name: str) -> Any:
                raise RuntimeError("nothing here works")

        deliveries.dispatch([_Exploding()])  # type: ignore[list-item]

    def test_a_channel_failure_does_not_fail_the_notification(
        self,
        db_session: Session,
        lawyer: Any,
    ) -> None:
        """The Notification Service catches per channel, so a broken WhatsApp
        channel costs a message rather than a notification."""

        class _BrokenChannel:
            def dispatch(self, notifications: Any) -> None:
                raise RuntimeError("channel is down")

        service = NotificationService(
            NotificationRepository(db_session), channels=[_BrokenChannel()]
        )
        assert service.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])

    def test_the_service_holds_no_event_publisher(
        self, deliveries: WhatsAppDeliveryService
    ) -> None:
        """The spec's *"build WhatsApp Delivery as a notification consumer, not as
        a business event consumer"*, asserted against the object rather than
        trusted: there is no attribute here that could reach the dispatcher."""
        collaborators = vars(deliveries)
        assert not any(
            "event" in name or "dispatcher" in name for name in collaborators
        )

    def test_metrics_are_counted_by_rule_and_never_by_recipient(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
        whatsapp_metrics: InMemoryWhatsAppMetrics,
    ) -> None:
        """"Eleven hearing changes were messaged" is throughput; "eleven messages
        to Amina" is a statement about a person's work."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        deliveries.process(WhatsAppJob(delivery_id=row.id))

        snapshot = whatsapp_metrics.snapshot()
        assert snapshot.delivered_by_rule == {"case.assigned": 1}
        assert str(lawyer.id) not in str(snapshot)
        assert "212612345678" not in str(snapshot)


class TestMonitoring:
    def test_it_reports_both_halves(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        deliveries.process(WhatsAppJob(delivery_id=row.id))

        metrics = deliveries.metrics()
        assert metrics.statistics.delivered == 1
        assert metrics.counters.delivered == 1
        assert metrics.enabled is True
        assert metrics.provider == "fake"
        assert metrics.templates_available is True

    def test_it_names_the_missing_configuration(
        self, deliveries: WhatsAppDeliveryService, provider: FakeWhatsAppProvider
    ) -> None:
        """The spec's *"provide meaningful error messages"*, reachable at any time
        rather than only from a startup log that scrolled past."""
        provider.available = False
        metrics = deliveries.metrics()

        assert metrics.provider_available is False
        assert metrics.configuration_errors == ["WHATSAPP_ACCESS_TOKEN"]

    def test_the_provider_response_time_is_separate_from_the_latency(
        self,
        deliveries: WhatsAppDeliveryService,
        notifications: NotificationService,
        lawyer: Any,
        db_session: Session,
    ) -> None:
        """One number would answer neither "are we slow?" nor "is WhatsApp
        slow?"."""
        notifications.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
        row = _stored(db_session)[0]
        deliveries.process(WhatsAppJob(delivery_id=row.id))

        counters = deliveries.metrics().counters
        assert counters.average_provider_response_ms == 6.0
        assert counters.average_delivery_latency_ms is not None


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


class TestWiring:
    """That the application actually supplies the channel.

    Every service on this platform defaults its optional collaborators to a
    no-op, so "the default is nothing" is one forgotten line away from being the
    production behaviour. These are the tests that make that a failing build
    rather than a quiet gap — and there are **two** paths, because a worker thread
    has no request to resolve a dependency from, so both have to be right.
    """

    def test_the_request_path_wires_the_whatsapp_channel(
        self, db_session: Session, deliveries: WhatsAppDeliveryService
    ) -> None:
        from api.deps import get_notification_channels
        from services.email_delivery import build_delivery_service
        from services.email_provider import NullEmailProvider
        from services.email_templates import get_email_template_renderer

        email = build_delivery_service(
            db_session,
            provider=NullEmailProvider(),
            templates=get_email_template_renderer(),
        )
        assert deliveries in get_notification_channels(email, deliveries)

    def test_the_worker_path_wires_it_too(self, db_session: Session) -> None:
        """Most notifications are created on the notification worker's thread,
        which is exactly the path a request-scoped dependency does not cover."""
        from services.notification_events import _delivery_channels

        assert any(
            isinstance(channel, WhatsAppDeliveryService)
            for channel in _delivery_channels(db_session)
        )

    def test_no_channel_is_built_when_the_feature_is_off(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a deployment with `WHATSAPP_ENABLED=false` does not construct a
        delivery service per event."""
        from services.notification_events import _delivery_channels

        monkeypatch.setattr(settings, "WHATSAPP_ENABLED", False)
        assert not any(
            isinstance(channel, WhatsAppDeliveryService)
            for channel in _delivery_channels(db_session)
        )

    def test_the_two_channels_are_switched_independently(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which is what makes them separate switches rather than one "outbound
        delivery" flag: a deployment with a relay and no WhatsApp Business account
        is the ordinary case, not an edge one."""
        from services.email_delivery import EmailDeliveryService
        from services.notification_events import _delivery_channels

        monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
        channels = _delivery_channels(db_session)

        assert any(isinstance(channel, WhatsAppDeliveryService) for channel in channels)
        assert not any(isinstance(channel, EmailDeliveryService) for channel in channels)

    def test_a_service_with_no_channels_still_creates_notifications(
        self, db_session: Session, lawyer: Any
    ) -> None:
        """Nothing depends on the channel in either direction: the in-app feed is
        exactly as it was before this feature existed."""
        service = NotificationService(NotificationRepository(db_session))
        assert service.create(rule=RULE_CASE_ASSIGNED, recipient_ids=[lawyer.id])
