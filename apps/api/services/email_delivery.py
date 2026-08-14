"""The Email Delivery Service: the one place an email is composed and sent.

``17-email-delivery-channel.md`` asks for a dedicated service that *"receives
notifications marked for email, renders templates, sends emails, tracks delivery,
and retries failures"*, and requires that it *"remain independent from business
modules"*. This module is that service, and the independence is a property of the
dependency graph rather than a convention: it holds two repositories, a provider,
a template renderer, a queue, and a metrics recorder, and **not one business
service**. There is no path from here to a case, a document, a report, or the
event dispatcher.

**It consumes notifications, never events**, which is the spec's central
boundary:

    The Email Delivery Channel should never receive domain events directly. It
    consumes notifications, not business events.

That is structural here. The class implements
:class:`~services.notification.NotificationDispatcher` — one method, taking rows
that have **already been created, authorized, de-duplicated, and persisted** by
the Notification Service. It cannot see an event, and it has no subscription on
the dispatcher to receive one through. Everything it does from there only
*narrows*: an email goes out to a subset of the people the platform had already
decided to tell, about a subset of the things it had already decided to say.

**Four filters, in cheap-first order**, and each is one of the spec's
requirements:

1. the feature switch and the provider's availability — a deployment with no
   relay writes no rows at all, rather than accumulating a backlog of failures;
2. :data:`~core.email.EMAIL_RULES` — *"only delivers notifications that have
   already been marked for email delivery"*, and the reason the spec's
   *"Events That Must NOT Generate Emails"* list needs no code of its own: those
   rules simply are not in the table;
3. the recipient's **email channel preference** — *"if a user disables email
   delivery for a supported notification type, no email should be sent"*, one
   query for the whole batch;
4. a usable address on an **active** account.

**Nothing here can fail a business operation, or even a notification.**
:meth:`EmailDeliveryService.dispatch` runs on the notification worker's thread,
after that batch has committed, and every path on it catches its own exceptions,
logs them, and counts them. :meth:`EmailDeliveryService.process` runs on an email
worker thread with no caller at all. The spec's *"failures should never interrupt
application functionality"* is therefore a fact about threads and return types
rather than a rule to remember — the same contract
:class:`~services.notification.NotificationService` keeps one layer up.

**No email content ever reaches a log**, and this module holds itself to a
stricter line than any other on the platform because it handles the one thing
none of the others does: a personal address. The logs here carry delivery
identifiers, rule keys, statuses, failure codes, attempt counts, and durations.
Never a subject, never a body, never a case number, and **never an address** —
not even at debug, and not even hashed, because an address hashed with no salt is
an address anyone with a user list can reverse.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.orm import Session

from core.config import settings
from core.email import (
    EmailFailureCode,
    InvalidEmailRecipientError,
    build_email_context,
    email_rule_for,
    is_transient,
    next_attempt_at,
    normalize_email,
    resolve_email_language,
    subject_for,
)
from core.notifications import (
    RULES_BY_KEY,
    NotificationChannel,
    default_preference,
    preference_from_value,
)
from models.email import EmailDelivery, EmailDeliveryStatus
from models.notification import Notification
from repositories.email import EmailDeliveryRepository, EmailDeliveryStatistics
from repositories.notification import NotificationRepository
from services.email_metrics import (
    EmailMetricsRecorder,
    EmailMetricsSnapshot,
    EmailSkipReason,
    NullEmailMetrics,
)
from services.email_provider import EmailProvider, OutgoingEmail
from services.email_templates import EmailTemplateError, EmailTemplateRenderer
from services.job_queue import JobQueue, NullJobQueue
from services.localization import (
    LanguageDirectory,
    StaticLanguageDirectory,
    build_language_directory,
)

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# The unit of background work
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EmailJob:
    """One queued delivery, as it crosses a thread boundary.

    **Identifiers only**, never an ORM instance and never a composed message — the
    rule :mod:`services.job_queue` states for every job on this platform. A job
    outlives the request or the worker turn that created it, and a detached
    SQLAlchemy object on the far side of either is a source of stale reads and
    cross-session errors. Carrying the *rendered* message would be worse still: it
    would put a subject and a body into an in-memory queue, which is exactly the
    place this feature keeps them out of.
    """

    delivery_id: uuid.UUID


# --------------------------------------------------------------------------- #
# What the monitoring view reads
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EmailDeliveryMetrics:
    """The monitoring view's two halves, joined.

    Row counts come from SQL and are exact; retries, latency, and skips come from
    the process and carry a ``since``. See :mod:`services.email_metrics` for why
    that split is not an inconsistency.
    """

    statistics: EmailDeliveryStatistics
    counters: EmailMetricsSnapshot
    enabled: bool
    provider: str
    provider_available: bool
    templates_available: bool
    window_days: int | None


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class EmailDeliveryService:
    """Queues, renders, sends, retries, and reports on notification emails."""

    def __init__(
        self,
        deliveries: EmailDeliveryRepository,
        notifications: NotificationRepository,
        provider: EmailProvider,
        templates: EmailTemplateRenderer,
        queue: JobQueue[EmailJob] | None = None,
        *,
        metrics: EmailMetricsRecorder | None = None,
        languages: LanguageDirectory | None = None,
    ) -> None:
        self._deliveries = deliveries
        self._notifications = notifications
        self._provider = provider
        self._templates = templates
        self._queue: JobQueue[EmailJob] = queue or NullJobQueue(name="email")
        self._metrics = metrics or NullEmailMetrics()
        # Defaults to the deployment's own answer for everybody, which is exactly
        # what this channel did before `21-localization.md` shipped — so a service
        # constructed by a script or a unit test needs no settings table to compose
        # a message, and the localized path is additive rather than a rewrite.
        self._languages: LanguageDirectory = languages or StaticLanguageDirectory(
            settings.EMAIL_DEFAULT_LANGUAGE
        )

    # --------------------------------------------------------------- queue #

    def dispatch(self, notifications: Sequence[Notification]) -> None:
        """Queue emails for whichever of these notifications belong on this channel.

        The :class:`~services.notification.NotificationDispatcher` contract:
        **must not raise, must not block**. Both are satisfied by doing four
        cheap reads and a batched insert, and by handing the actual sending to a
        queue — a mail server's round trip has no business happening on the
        thread that just created somebody's in-app notification.

        Returns nothing, deliberately. The Notification Service must not be able
        to learn whether a channel accepted a batch, because the moment it can, it
        acquires an opinion about delivery and the boundary this feature is built
        around starts to erode. What happened is on the delivery rows and in the
        metrics.
        """
        try:
            self._queue_batch(notifications)
        except Exception:  # pragma: no cover - defensive; every path below catches
            logger.exception("email_dispatch_failed", count=len(notifications))

    def _queue_batch(self, notifications: Sequence[Notification]) -> int:
        """Write pending deliveries for an eligible batch. Returns how many.

        Public behaviour is :meth:`dispatch`; this is separated so a test can
        assert on a **count** rather than on a queue, and so the exception barrier
        above has exactly one thing to guard.
        """
        if not notifications:
            return 0

        if not settings.EMAIL_ENABLED:
            self._metrics.record_skipped(EmailSkipReason.DISABLED, len(notifications))
            return 0

        if not self._provider.is_available():
            # Skipped rather than queued, and this is the decision that keeps a
            # deployment with no relay honest. Writing `pending` rows for mail
            # that has nowhere to go would build a backlog whose only outcome is
            # a burst of very old messages the day somebody configures SMTP —
            # including "your case was assigned" for a case that closed weeks ago.
            self._metrics.record_skipped(
                EmailSkipReason.PROVIDER_UNAVAILABLE, len(notifications)
            )
            logger.debug("email_provider_unavailable", provider=self._provider.name)
            return 0

        eligible = self._eligible(notifications)
        if not eligible:
            return 0

        eligible = self._filter_by_preference(eligible)
        if not eligible:
            return 0

        eligible = self._filter_already_queued(eligible)
        if not eligible:
            return 0

        rows = self._build_rows(eligible)
        if not rows:
            return 0

        try:
            created = self._deliveries.create_many(rows)
        except Exception:
            # The one expected cause is the unique constraint, and when it fires
            # the outcome is already correct: two processes handled the same
            # notification and one of them committed, so exactly one email exists.
            # Recorded rather than retried — a retry would re-collide, and
            # `_filter_already_queued` is what keeps this path rare.
            self._deliveries.rollback()
            self._metrics.record_skipped(EmailSkipReason.ALREADY_QUEUED, len(rows))
            logger.warning("email_queue_write_failed", count=len(rows))
            return 0

        self._metrics.record_queued(len(created))
        logger.info(
            "email_queued",
            count=len(created),
            # By **rule**, which is a throughput figure. Counting by recipient
            # would be a live index of whose mailbox the platform writes to.
            rules=sorted({row.rule_key for row in created}),
        )

        for row in created:
            self._queue.enqueue(EmailJob(delivery_id=row.id))

        return len(created)

    # ------------------------------------------------------------- filters #

    def _eligible(self, notifications: Sequence[Notification]) -> list[Notification]:
        """Keep the notifications whose rule is marked for email delivery.

        A dictionary lookup with no I/O, and the **first** filter for that reason
        — most of the platform's notifications are in-app only, so this is what
        keeps a document upload from costing a preference query.
        """
        wanted = [entry for entry in notifications if email_rule_for(entry.rule_key)]
        skipped = len(notifications) - len(wanted)
        if skipped:
            self._metrics.record_skipped(EmailSkipReason.NOT_EMAIL_ELIGIBLE, skipped)
        return wanted

    def _filter_by_preference(self, notifications: Sequence[Notification]) -> list[Notification]:
        """Drop the recipients who have switched email off for this kind of news.

        Grouped by rule so each distinct preference key costs **one query for the
        whole batch**, rather than one per person: an event about a case fans out
        to everyone party to it, and asking per recipient would make the cost of a
        hearing change proportional to the size of the team.

        A failure here **admits**, exactly as
        :meth:`~services.notification.NotificationService._filter_by_preference`
        does and for the same reason: a preference lookup that could not run is
        not evidence that somebody asked not to be emailed, and a hearing reminder
        silently swallowed because the database hiccuped is the worse of the two
        errors.
        """
        by_key: dict[str, list[Notification]] = {}
        wanted: list[Notification] = []

        for entry in notifications:
            key = self._preference_key(entry)
            if key is None:
                # Unreachable — every rule in `EMAIL_RULES` is a real notification
                # rule, which a test asserts — and it **admits** rather than
                # dropping if it ever happens: a rule whose preference cannot be
                # resolved is not evidence that somebody asked not to be emailed,
                # and counting it as a suppression would label a bug as a user's
                # choice in the monitoring view.
                wanted.append(entry)
                continue
            by_key.setdefault(key, []).append(entry)

        for key, batch in by_key.items():
            resolved = preference_from_value(key)
            fallback = (
                default_preference(resolved, NotificationChannel.EMAIL)
                if resolved is not None
                else True
            )
            try:
                stored = self._notifications.preferences_for_many(
                    [entry.recipient_id for entry in batch],
                    preference_key=key,
                    channel=NotificationChannel.EMAIL,
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("email_preference_lookup_failed", preference=key)
                wanted.extend(batch)
                continue

            wanted.extend(
                entry for entry in batch if stored.get(entry.recipient_id, fallback)
            )

        skipped = len(notifications) - len(wanted)
        if skipped:
            self._metrics.record_skipped(EmailSkipReason.SUPPRESSED_BY_PREFERENCE, skipped)
        return wanted

    @staticmethod
    def _preference_key(notification: Notification) -> str | None:
        """Which preference governs this notification, from its own rule.

        Resolved through :data:`~core.notifications.RULES_BY_KEY` rather than
        stored on the delivery, so the email channel and the in-app feed are
        governed by the **same** key — a rule whose preference is re-pointed in
        :mod:`core.notifications` moves both channels at once, which is the only
        behaviour a user would be able to predict.
        """
        rule = RULES_BY_KEY.get(notification.rule_key)
        return rule.preference.value if rule is not None else None

    def _filter_already_queued(
        self, notifications: Sequence[Notification]
    ) -> list[Notification]:
        """Drop the notifications that already have a delivery.

        The ordinary half of "avoid duplicate emails"; the unique constraint on
        ``email_deliveries.notification_id`` is the half that holds under
        concurrency. One query for the whole batch.

        A failure here **excludes** rather than admitting, which is the opposite
        of the preference filter above and deliberately so: the two errors are not
        symmetric. A duplicate *notification* is a repeated line in a feed; a
        duplicate *email* is a second message in somebody's inbox saying a hearing
        moved, and a recipient who cannot tell which one is current is worse off
        than one who has to open the application to check.
        """
        try:
            already = self._deliveries.existing_notification_ids(
                [entry.id for entry in notifications]
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("email_duplicate_check_failed")
            self._metrics.record_skipped(
                EmailSkipReason.ALREADY_QUEUED, len(notifications)
            )
            return []

        wanted = [entry for entry in notifications if entry.id not in already]
        skipped = len(notifications) - len(wanted)
        if skipped:
            self._metrics.record_skipped(EmailSkipReason.ALREADY_QUEUED, skipped)
        return wanted

    def _build_rows(self, notifications: Sequence[Notification]) -> list[EmailDelivery]:
        """Turn eligible notifications into queued delivery rows.

        The **address is resolved and validated here**, before anything is
        queued, so an account with no usable address never becomes a job that a
        worker picks up only to fail on. It is also snapshotted onto the row — see
        :attr:`~models.email.EmailDelivery.recipient_email` for why a join would
        rewrite history.
        """
        recipient_ids = [entry.recipient_id for entry in notifications]
        profiles = self._deliveries.recipient_profiles(recipient_ids)
        # One query for the whole batch, resolved **before** anything is queued and
        # snapshotted onto each row — for the same reason the address is. A
        # language read at *send* time would rewrite history: somebody who changed
        # their preference between a hearing update being queued and the relay
        # accepting it would receive a message in a language the platform decided
        # after the fact, and a retry three attempts later would differ from the
        # first.
        languages = self._languages.languages_for(recipient_ids)

        rows: list[EmailDelivery] = []
        missing = 0
        for entry in notifications:
            rule = email_rule_for(entry.rule_key)
            profile = profiles.get(entry.recipient_id)
            address = normalize_email(profile.email) if profile is not None else None

            if rule is None or address is None:
                # `rule is None` cannot happen — `_eligible` ran first — and is
                # checked because the alternative is a type ignore on the line
                # below. A missing address genuinely can, and is the interesting
                # half: a deactivated account (dropped by `recipient_profiles`) or
                # a blank column.
                missing += 1
                continue

            rows.append(
                EmailDelivery(
                    id=uuid.uuid4(),
                    notification_id=entry.id,
                    recipient_id=entry.recipient_id,
                    recipient_email=address,
                    rule_key=entry.rule_key,
                    category=entry.category,
                    template=rule.template,
                    template_version=rule.version,
                    language=resolve_email_language(
                        languages.get(entry.recipient_id)
                    ),
                    status=EmailDeliveryStatus.PENDING,
                )
            )

        if missing:
            self._metrics.record_skipped(EmailSkipReason.NO_ADDRESS, missing)
            logger.info("email_recipient_unusable", count=missing)

        return rows

    # ------------------------------------------------------------ sending #

    def process(self, job: EmailJob) -> bool:
        """Send one queued delivery. Returns whether the provider accepted it.

        **Never raises**, and it is the whole of what an email worker thread runs.
        Every way a send can go wrong becomes a recorded state on the delivery row
        — retried with a backoff if the cause was transient, ``failed`` if it was
        not — and the notification, the case, the document, and the account are
        untouched throughout, because this service writes to one table.

        Public and synchronous so the feature is testable **without a thread**: a
        test hands it a job and asserts about a row, rather than queueing one and
        waiting to see. The worker loop is the only production caller.
        """
        if not self._deliveries.claim(job.delivery_id):
            # Somebody else has it, or it is no longer pending. Both are ordinary
            # — a sweeper re-queueing beside a live dispatch produces exactly this
            # — and both mean this call has nothing to do.
            logger.debug("email_claim_lost", delivery_id=str(job.delivery_id))
            return False

        delivery = self._deliveries.get(job.delivery_id)
        if delivery is None:  # pragma: no cover - claimed a row that then vanished
            return False

        logger.info(
            "email_sending",
            delivery_id=str(delivery.id),
            rule=delivery.rule_key,
            attempt=delivery.attempts,
        )

        try:
            message = self._compose(delivery)
        except (EmailTemplateError, InvalidEmailRecipientError) as exc:
            return self._record_failure(
                delivery,
                code=(
                    EmailFailureCode.TEMPLATE_FAILURE
                    if isinstance(exc, EmailTemplateError)
                    else EmailFailureCode.INVALID_RECIPIENT
                ),
            )
        except _NotificationGoneError:
            return self._record_failure(delivery, code=EmailFailureCode.MESSAGE_REFUSED)
        except Exception:
            logger.exception("email_compose_failed", delivery_id=str(delivery.id))
            return self._record_failure(delivery, code=EmailFailureCode.UNKNOWN)

        result = self._provider.send(message)

        if not result.accepted:
            return self._record_failure(
                delivery,
                code=result.failure or EmailFailureCode.UNKNOWN,
                provider=result.provider,
            )

        self._deliveries.mark_sent(
            delivery.id, provider=result.provider, duration_ms=result.duration_ms
        )
        self._metrics.record_sent(
            delivery.rule_key,
            latency_ms=_elapsed_ms(delivery.created_at),
            duration_ms=result.duration_ms,
        )
        logger.info(
            "email_delivered",
            delivery_id=str(delivery.id),
            rule=delivery.rule_key,
            provider=result.provider,
            attempts=delivery.attempts,
            duration_ms=round(result.duration_ms, 2),
        )
        return True

    def _compose(self, delivery: EmailDelivery) -> OutgoingEmail:
        """Render one delivery into an addressed message.

        **The notification is read back through the recipient-scoped query**, and
        that is not incidental. Every read in
        :mod:`repositories.notification` is keyed by recipient and there is
        deliberately no unscoped variant, so the worker asks *"this recipient's
        notification with this id"* — which means an email can only ever be
        composed from something the addressee is entitled to read. The spec's
        *"the Email Delivery Channel should trust the Notification Service and
        never attempt to broaden notification visibility"* is therefore enforced
        by the shape of the repository rather than by this function's good
        behaviour.

        Raises:
            _NotificationGoneError: the notification no longer exists or was
                archived.
            EmailTemplateError: the template is missing or could not be rendered.
            InvalidEmailRecipientError: a header value is unusable.
        """
        notification = self._notifications.get(
            delivery.notification_id, recipient_id=delivery.recipient_id
        )
        if notification is None:
            logger.warning("email_notification_missing", delivery_id=str(delivery.id))
            raise _NotificationGoneError

        profile = self._deliveries.recipient_profiles([delivery.recipient_id]).get(
            delivery.recipient_id
        )
        if profile is None:
            # The account was deactivated between queueing and sending. Refusing
            # is the right answer: a suspended user should not be mailed a link
            # into a platform they can no longer sign in to.
            logger.info("email_recipient_inactive", delivery_id=str(delivery.id))
            raise InvalidEmailRecipientError("The recipient's account is not active.")

        context = build_email_context(
            rule_key=notification.rule_key,
            category=notification.category,
            priority=notification.priority.value,
            context=notification.context,
            recipient_name=profile.full_name or profile.email.partition("@")[0],
            language=delivery.language,
            base_url=settings.email_base_url,
            target_type=notification.target_type,
            target_id=notification.target_id,
            platform_name=settings.EMAIL_FROM_NAME,
        )

        rendered = self._templates.render(
            delivery.template,
            version=delivery.template_version,
            context=context.as_mapping(),
        )

        return OutgoingEmail(
            to_address=delivery.recipient_email,
            to_name=profile.full_name,
            subject=subject_for(rendered.subject, prefix=settings.EMAIL_SUBJECT_PREFIX),
            html_body=rendered.html,
            text_body=rendered.text,
            from_address=settings.EMAIL_FROM_ADDRESS or "",
            from_name=settings.EMAIL_FROM_NAME,
            reply_to=settings.EMAIL_REPLY_TO,
        )

    def _record_failure(
        self,
        delivery: EmailDelivery,
        *,
        code: EmailFailureCode,
        provider: str | None = None,
    ) -> bool:
        """Reschedule or give up, and say which in the log. Always returns ``False``.

        **The whole retry decision**, and it turns on two things and no others:
        whether the cause is in :data:`~core.email.TRANSIENT_FAILURE_CODES`, and
        whether the delivery has attempts left. Neither is a judgement made here —
        the first is a property of the code, the second is a column and a setting
        — which is what makes the behaviour the same whichever provider produced
        the failure.
        """
        exhausted = delivery.attempts >= max(1, settings.EMAIL_MAX_ATTEMPTS)

        if is_transient(code) and not exhausted:
            due = next_attempt_at(
                delivery.attempts,
                base=settings.EMAIL_RETRY_BACKOFF_SECONDS,
                cap=settings.EMAIL_RETRY_MAX_BACKOFF_SECONDS,
            )
            self._deliveries.reschedule(
                delivery.id, error_code=code.value, next_attempt=due, provider=provider
            )
            self._metrics.record_retry(code)
            logger.info(
                "email_retry_scheduled",
                delivery_id=str(delivery.id),
                rule=delivery.rule_key,
                error_code=code.value,
                attempt=delivery.attempts,
                # A duration, not a timestamp: a wall-clock time in a log line is
                # one more thing a reader has to convert.
                retry_in_seconds=round(max(0.0, (due - datetime.now(UTC)).total_seconds()), 1),
            )
            return False

        self._deliveries.mark_failed(
            delivery.id, error_code=code.value, provider=provider
        )
        self._metrics.record_failed(code)
        logger.warning(
            "email_delivery_failed",
            delivery_id=str(delivery.id),
            rule=delivery.rule_key,
            error_code=code.value,
            attempts=delivery.attempts,
            permanent=not is_transient(code),
        )
        return False

    # -------------------------------------------------------------- sweep #

    def sweep(self) -> int:
        """Re-queue everything that is due, and rescue anything stranded.

        **The retry mechanism's other half**, and the reason no worker ever sleeps
        out a backoff: a transient failure writes a time onto the row and returns
        the thread, and this picks the row up when that time arrives. Run on an
        interval by :mod:`services.email_worker`, and once at startup — which is
        also the recovery for a process that was stopped with work queued, since a
        ``pending`` row whose schedule lived in a dead process's memory is exactly
        what this looks for.

        Bounded per pass (``EMAIL_RETRY_BATCH_SIZE``), because a relay that was
        down overnight leaves a backlog rather than a page, and loading all of it
        to re-queue it is how a recovery becomes its own outage. The next pass
        takes the next batch.

        Returns:
            How many deliveries were handed back to the queue. Never raises: this
            runs on a timer thread with nobody to report to.
        """
        if not settings.EMAIL_ENABLED:
            return 0

        try:
            reclaimed = self._deliveries.reclaim_stale(
                older_than=datetime.now(UTC)
                - timedelta(seconds=settings.EMAIL_STALE_SENDING_SECONDS)
            )
            if reclaimed:
                logger.warning("email_stale_reclaimed", count=reclaimed)

            due = self._deliveries.due_deliveries(limit=settings.EMAIL_RETRY_BATCH_SIZE)
        except Exception:
            logger.exception("email_sweep_failed")
            return 0

        for delivery_id in due:
            self._queue.enqueue(EmailJob(delivery_id=delivery_id))

        if due:
            logger.info("email_requeued", count=len(due))
        return len(due)

    # ---------------------------------------------------------- monitoring #

    def metrics(self, *, window_days: int | None = None) -> EmailDeliveryMetrics:
        """Return platform-wide email delivery health.

        Args:
            window_days: apply a window to the **SQL** figures. The in-process
                counters have their own window — the process's lifetime — which is
                reported as ``since`` rather than pretending the two are the same
                thing.
        """
        since = (
            datetime.now(UTC) - timedelta(days=window_days) if window_days is not None else None
        )
        return EmailDeliveryMetrics(
            statistics=self._deliveries.statistics(since=since),
            counters=self._metrics.snapshot(),
            enabled=settings.EMAIL_ENABLED,
            provider=self._provider.name,
            provider_available=self._provider.is_available(),
            templates_available=self._templates.is_available(),
            window_days=window_days,
        )


class _NotificationGoneError(RuntimeError):
    """The notification a queued delivery carries no longer exists.

    Private, and a distinct type rather than a ``None`` return, so
    :meth:`EmailDeliveryService.process` can map it onto a **permanent** failure:
    a notification that was deleted or archived is not going to come back, and
    retrying would be four more attempts to render something that is gone.
    """


def _elapsed_ms(reference: datetime) -> float:
    """Milliseconds from ``reference`` until now, never negative.

    ``reference`` is read as UTC when it carries no timezone: SQLite returns naive
    datetimes for a ``TIMESTAMP WITH TIME ZONE`` column, so a latency computed
    against one would raise rather than be slightly wrong. The same helper
    :mod:`services.notification` carries, for the same reason.
    """
    aware = reference if reference.tzinfo is not None else reference.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - aware).total_seconds() * 1000.0)


def build_delivery_service(
    session: Session,
    *,
    provider: EmailProvider,
    templates: EmailTemplateRenderer,
    queue: JobQueue[EmailJob] | None = None,
    metrics: EmailMetricsRecorder | None = None,
) -> EmailDeliveryService:
    """Assemble a delivery service on one session.

    A small factory, and it exists because this service is built in **three**
    places that have nothing else in common — a request dependency, the
    notification worker's thread, and the email worker's own thread — and each
    assembling it by hand is three places for the collaborator list to drift.

    The language directory is built on the **same session**, so resolving a
    hundred recipients' languages costs one query on the connection that is
    already open — and it is handed over as the one-method
    :class:`~services.localization.LanguageDirectory`, never as a settings
    repository, which is the deliberate answer to the open question this channel
    recorded when it shipped.
    """
    return EmailDeliveryService(
        EmailDeliveryRepository(session),
        NotificationRepository(session),
        provider,
        templates,
        queue,
        metrics=metrics,
        languages=build_language_directory(
            session, channel_default=settings.EMAIL_DEFAULT_LANGUAGE
        ),
    )


__all__ = [
    "EmailDeliveryMetrics",
    "EmailDeliveryService",
    "EmailJob",
    "build_delivery_service",
]
