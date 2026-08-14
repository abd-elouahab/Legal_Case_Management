"""The WhatsApp Delivery Service: the one place a WhatsApp message is composed and sent.

``18-whatsapp-delivery-channel.md`` asks for a dedicated service that *"receives
notifications marked for WhatsApp, renders templates, sends WhatsApp messages,
tracks delivery, and retries temporary failures"*, and requires that it *"remain
independent from business modules"*. This module is that service, and the
independence is a property of the dependency graph rather than a convention: it
holds two repositories, a provider, a template renderer, a queue, and a metrics
recorder, and **not one business service**. There is no path from here to a case,
a document, a report, or the event dispatcher.

**It consumes notifications, never events**, which is the spec's central boundary:

    The WhatsApp Delivery Channel consumes notifications rather than business
    events. […] Build WhatsApp Delivery as a notification consumer, not as a
    business event consumer.

That is structural here. The class implements
:class:`~services.notification.NotificationDispatcher` — one method, taking rows
that have **already been created, authorized, de-duplicated, and persisted** by
the Notification Service. It cannot see an event, and it has no subscription on
the dispatcher to receive one through. Everything it does from there only
*narrows*: a message goes out to a subset of the people the platform had already
decided to tell, about a subset of the things it had already decided to say. That
is also the whole of the spec's Authorization section — *"the WhatsApp Delivery
Channel should trust the Notification Service and never broaden notification
visibility"* — and it is inherited rather than re-implemented, which is why there
is no ``whatsapp_access.py``.

**Five filters, in cheap-first order**, and each is one of the spec's
requirements:

1. the feature switch and the provider's availability — a deployment with no
   business account writes no rows at all, rather than accumulating a backlog of
   failures;
2. :data:`~core.whatsapp.WHATSAPP_RULES` — *"only delivers notifications that have
   already been marked for WhatsApp delivery"*, and the reason the spec's
   *"Events That Must NOT Generate WhatsApp Messages"* list needs no code of its
   own: those rules simply are not in the table;
3. the recipient's **WhatsApp channel preference** — *"users should be able to
   enable or disable WhatsApp delivery independently from in-app and email"*, one
   query for the whole batch;
4. a delivery that does not already exist — the ordinary half of "avoid duplicate
   messages";
5. a **usable phone number** on an **active** account. This is the filter that has
   no counterpart on the email channel, and it is the one that skips most of what
   reaches it: ``users.phone`` is optional, so an account created without one is
   never messaged, forever and correctly.

**Nothing here can fail a business operation, or even a notification.**
:meth:`WhatsAppDeliveryService.dispatch` runs on the notification worker's thread,
after that batch has committed, and every path on it catches its own exceptions,
logs them, and counts them. :meth:`WhatsAppDeliveryService.process` runs on a
WhatsApp worker thread with no caller at all. The spec's *"failures should never
interrupt application functionality"* is therefore a fact about threads and return
types rather than a rule to remember.

**No message content ever reaches a log**, and this module holds itself to the
same line the email channel does, for a target that is more identifying rather
than less: a phone number is a device somebody carries. The logs here carry
delivery identifiers, rule keys, statuses, failure codes, attempt counts, and
durations. Never a title, never a message, never a case number, and **never a
number** — not even at debug, and not even hashed, because a phone number hashed
with no salt is a phone number anyone with a user list can reverse.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.orm import Session

from core.config import settings
from core.notifications import (
    RULES_BY_KEY,
    NotificationChannel,
    default_preference,
    preference_from_value,
)
from core.whatsapp import (
    InvalidWhatsAppRecipientError,
    WhatsAppFailureCode,
    build_whatsapp_context,
    is_transient,
    next_attempt_at,
    normalize_phone,
    provider_language_code,
    resolve_whatsapp_language,
    whatsapp_rule_for,
)
from models.notification import Notification
from models.whatsapp import WhatsAppDelivery, WhatsAppDeliveryStatus
from repositories.notification import NotificationRepository
from repositories.whatsapp import (
    WhatsAppDeliveryRepository,
    WhatsAppDeliveryStatistics,
)
from services.job_queue import JobQueue, NullJobQueue
from services.localization import (
    LanguageDirectory,
    StaticLanguageDirectory,
    build_language_directory,
)
from services.whatsapp_metrics import (
    NullWhatsAppMetrics,
    WhatsAppMetricsRecorder,
    WhatsAppMetricsSnapshot,
    WhatsAppSkipReason,
)
from services.whatsapp_provider import OutgoingWhatsAppMessage, WhatsAppProvider
from services.whatsapp_templates import WhatsAppTemplateError, WhatsAppTemplateRenderer

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# The unit of background work
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WhatsAppJob:
    """One queued delivery, as it crosses a thread boundary.

    **Identifiers only**, never an ORM instance and never a composed message — the
    rule :mod:`services.job_queue` states for every job on this platform. A job
    outlives the request or the worker turn that created it, and a detached
    SQLAlchemy object on the far side of either is a source of stale reads and
    cross-session errors. Carrying the *rendered* parameters would be worse still:
    it would put a recipient's name and a case number into an in-memory queue,
    which is exactly the place this feature keeps them out of.
    """

    delivery_id: uuid.UUID


# --------------------------------------------------------------------------- #
# What the monitoring view reads
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WhatsAppDeliveryMetrics:
    """The monitoring view's two halves, joined.

    Row counts come from SQL and are exact; retries, latency, provider response
    time, and skips come from the process and carry a ``since``. See
    :mod:`services.whatsapp_metrics` for why that split is not an inconsistency.
    """

    statistics: WhatsAppDeliveryStatistics
    counters: WhatsAppMetricsSnapshot
    enabled: bool
    provider: str
    provider_available: bool
    #: Which required settings are missing, **by name**. The spec's *"provide
    #: meaningful error messages"* for a misconfiguration, and it is here rather
    #: than only in a startup log because a deployment that turned the channel on
    #: without finishing its configuration finds out from the monitoring endpoint
    #: at any time, not only from a log line that scrolled past at boot.
    configuration_errors: list[str]
    templates_available: bool
    window_days: int | None


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class WhatsAppDeliveryService:
    """Queues, renders, sends, retries, and reports on notification messages."""

    def __init__(
        self,
        deliveries: WhatsAppDeliveryRepository,
        notifications: NotificationRepository,
        provider: WhatsAppProvider,
        templates: WhatsAppTemplateRenderer,
        queue: JobQueue[WhatsAppJob] | None = None,
        *,
        metrics: WhatsAppMetricsRecorder | None = None,
        languages: LanguageDirectory | None = None,
    ) -> None:
        self._deliveries = deliveries
        self._notifications = notifications
        self._provider = provider
        self._templates = templates
        self._queue: JobQueue[WhatsAppJob] = queue or NullJobQueue(name="whatsapp")
        self._metrics = metrics or NullWhatsAppMetrics()
        # Defaults to the deployment's own answer for everybody, which is exactly
        # what this channel did before `21-localization.md` shipped — so a service
        # constructed by a script or a unit test needs no settings table to compose
        # a message.
        self._languages: LanguageDirectory = languages or StaticLanguageDirectory(
            settings.WHATSAPP_DEFAULT_LANGUAGE
        )

    # --------------------------------------------------------------- queue #

    def dispatch(self, notifications: Sequence[Notification]) -> None:
        """Queue messages for whichever of these notifications belong on this channel.

        The :class:`~services.notification.NotificationDispatcher` contract:
        **must not raise, must not block**. Both are satisfied by doing a few cheap
        reads and a batched insert, and by handing the actual sending to a queue —
        an HTTPS round trip to Meta has no business happening on the thread that
        just created somebody's in-app notification.

        Returns nothing, deliberately. The Notification Service must not be able to
        learn whether a channel accepted a batch, because the moment it can, it
        acquires an opinion about delivery and the boundary this feature is built
        around starts to erode. What happened is on the delivery rows and in the
        metrics.
        """
        try:
            self._queue_batch(notifications)
        except Exception:  # pragma: no cover - defensive; every path below catches
            logger.exception("whatsapp_dispatch_failed", count=len(notifications))

    def _queue_batch(self, notifications: Sequence[Notification]) -> int:
        """Write pending deliveries for an eligible batch. Returns how many.

        Public behaviour is :meth:`dispatch`; this is separated so a test can
        assert on a **count** rather than on a queue, and so the exception barrier
        above has exactly one thing to guard.
        """
        if not notifications:
            return 0

        if not settings.WHATSAPP_ENABLED:
            self._metrics.record_skipped(WhatsAppSkipReason.DISABLED, len(notifications))
            return 0

        if not self._provider.is_available():
            # Skipped rather than queued, and this is the decision that keeps a
            # deployment with no business account honest. Writing `pending` rows
            # for messages that have nowhere to go would build a backlog whose only
            # outcome is a burst of very old notices the day somebody finishes the
            # configuration — including "you were assigned a case" for a case that
            # closed weeks ago.
            self._metrics.record_skipped(
                WhatsAppSkipReason.PROVIDER_UNAVAILABLE, len(notifications)
            )
            logger.debug(
                "whatsapp_provider_unavailable",
                provider=self._provider.name,
                # Setting **names**, never values. This is the one log line that
                # tells an operator why nothing is being sent.
                missing=self._provider.configuration_errors(),
            )
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
            # notification and one of them committed, so exactly one message
            # exists. Recorded rather than retried — a retry would re-collide, and
            # `_filter_already_queued` is what keeps this path rare.
            self._deliveries.rollback()
            self._metrics.record_skipped(WhatsAppSkipReason.ALREADY_QUEUED, len(rows))
            logger.warning("whatsapp_queue_write_failed", count=len(rows))
            return 0

        self._metrics.record_queued(len(created))
        logger.info(
            "whatsapp_queued",
            count=len(created),
            # By **rule**, which is a throughput figure. Counting by recipient
            # would be a live index of whose phone the platform writes to.
            rules=sorted({row.rule_key for row in created}),
        )

        for row in created:
            self._queue.enqueue(WhatsAppJob(delivery_id=row.id))

        return len(created)

    # ------------------------------------------------------------- filters #

    def _eligible(self, notifications: Sequence[Notification]) -> list[Notification]:
        """Keep the notifications whose rule is marked for WhatsApp delivery.

        A dictionary lookup with no I/O, and the **first** filter for that reason —
        most of the platform's notifications are in-app only, so this is what keeps
        a document upload from costing a preference query.
        """
        wanted = [entry for entry in notifications if whatsapp_rule_for(entry.rule_key)]
        skipped = len(notifications) - len(wanted)
        if skipped:
            self._metrics.record_skipped(
                WhatsAppSkipReason.NOT_WHATSAPP_ELIGIBLE, skipped
            )
        return wanted

    def _filter_by_preference(
        self, notifications: Sequence[Notification]
    ) -> list[Notification]:
        """Drop the recipients who have switched WhatsApp off for this kind of news.

        Grouped by rule so each distinct preference key costs **one query for the
        whole batch**, rather than one per person: an event about a case fans out
        to everyone party to it, and asking per recipient would make the cost of a
        hearing change proportional to the size of the team.

        A failure here **admits**, exactly as
        :meth:`~services.notification.NotificationService._filter_by_preference`
        does and for the same reason: a preference lookup that could not run is not
        evidence that somebody asked not to be messaged, and a hearing update
        silently swallowed because the database hiccuped is the worse of the two
        errors.
        """
        by_key: dict[str, list[Notification]] = {}
        wanted: list[Notification] = []

        for entry in notifications:
            key = self._preference_key(entry)
            if key is None:
                # Unreachable — every rule in `WHATSAPP_RULES` is a real
                # notification rule, which a test asserts — and it **admits**
                # rather than dropping if it ever happens: a rule whose preference
                # cannot be resolved is not evidence that somebody asked not to be
                # messaged, and counting it as a suppression would label a bug as a
                # user's choice in the monitoring view.
                wanted.append(entry)
                continue
            by_key.setdefault(key, []).append(entry)

        for key, batch in by_key.items():
            resolved = preference_from_value(key)
            fallback = (
                default_preference(resolved, NotificationChannel.WHATSAPP)
                if resolved is not None
                else True
            )
            try:
                stored = self._notifications.preferences_for_many(
                    [entry.recipient_id for entry in batch],
                    preference_key=key,
                    channel=NotificationChannel.WHATSAPP,
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("whatsapp_preference_lookup_failed", preference=key)
                wanted.extend(batch)
                continue

            wanted.extend(
                entry for entry in batch if stored.get(entry.recipient_id, fallback)
            )

        skipped = len(notifications) - len(wanted)
        if skipped:
            self._metrics.record_skipped(
                WhatsAppSkipReason.SUPPRESSED_BY_PREFERENCE, skipped
            )
        return wanted

    @staticmethod
    def _preference_key(notification: Notification) -> str | None:
        """Which preference governs this notification, from its own rule.

        Resolved through :data:`~core.notifications.RULES_BY_KEY` rather than
        stored on the delivery, so every channel is governed by the **same** key —
        a rule whose preference is re-pointed in :mod:`core.notifications` moves
        all three channels at once, which is the only behaviour a user would be
        able to predict.
        """
        rule = RULES_BY_KEY.get(notification.rule_key)
        return rule.preference.value if rule is not None else None

    def _filter_already_queued(
        self, notifications: Sequence[Notification]
    ) -> list[Notification]:
        """Drop the notifications that already have a delivery.

        The ordinary half of "avoid duplicate messages"; the unique constraint on
        ``whatsapp_deliveries.notification_id`` is the half that holds under
        concurrency. One query for the whole batch.

        A failure here **excludes** rather than admitting, which is the opposite of
        the preference filter above and deliberately so: the two errors are not
        symmetric, and the asymmetry is sharper on this channel than on email. A
        duplicate *notification* is a repeated line in a feed; a duplicate
        *WhatsApp message* is a second phone alert saying a hearing moved, and a
        recipient who cannot tell which one is current is worse off than one who
        has to open the application to check.
        """
        try:
            already = self._deliveries.existing_notification_ids(
                [entry.id for entry in notifications]
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("whatsapp_duplicate_check_failed")
            self._metrics.record_skipped(
                WhatsAppSkipReason.ALREADY_QUEUED, len(notifications)
            )
            return []

        wanted = [entry for entry in notifications if entry.id not in already]
        skipped = len(notifications) - len(wanted)
        if skipped:
            self._metrics.record_skipped(WhatsAppSkipReason.ALREADY_QUEUED, skipped)
        return wanted

    def _build_rows(
        self, notifications: Sequence[Notification]
    ) -> list[WhatsAppDelivery]:
        """Turn eligible notifications into queued delivery rows.

        The **number is resolved, normalized, and validated here**, before anything
        is queued, so an account with no usable number never becomes a job that a
        worker picks up only to fail on. It is also snapshotted onto the row in the
        E.164 form the provider takes — see
        :attr:`~models.whatsapp.WhatsAppDelivery.recipient_phone` for why a join
        would rewrite history, and :func:`~core.whatsapp.normalize_phone` for why
        an ambiguous number is refused rather than guessed at.
        """
        recipient_ids = [entry.recipient_id for entry in notifications]
        profiles = self._deliveries.recipient_profiles(recipient_ids)
        # One query for the whole batch, resolved **before** anything is queued and
        # snapshotted onto each row — for the same reason the number is, and with
        # one consequence of its own on this channel: the language decides which
        # *approved template* Meta is asked for, so resolving it at send time would
        # let a preference changed mid-retry send an attempt against a template
        # that was never submitted in that language.
        languages = self._languages.languages_for(recipient_ids)

        rows: list[WhatsAppDelivery] = []
        missing = 0
        for entry in notifications:
            rule = whatsapp_rule_for(entry.rule_key)
            profile = profiles.get(entry.recipient_id)
            number = (
                normalize_phone(
                    profile.phone,
                    default_country_code=settings.WHATSAPP_DEFAULT_COUNTRY_CODE,
                )
                if profile is not None
                else None
            )

            if rule is None or number is None:
                # `rule is None` cannot happen — `_eligible` ran first — and is
                # checked because the alternative is a type ignore on the line
                # below. A missing number genuinely can, and is the ordinary case
                # on this channel rather than the exception: an account with no
                # phone, a deactivated one (dropped by `recipient_profiles`), or a
                # number this platform will not guess at.
                missing += 1
                continue

            rows.append(
                WhatsAppDelivery(
                    id=uuid.uuid4(),
                    notification_id=entry.id,
                    recipient_id=entry.recipient_id,
                    recipient_phone=number,
                    rule_key=entry.rule_key,
                    category=entry.category,
                    template=rule.template,
                    template_version=rule.version,
                    language=resolve_whatsapp_language(
                        languages.get(entry.recipient_id)
                    ),
                    status=WhatsAppDeliveryStatus.PENDING,
                )
            )

        if missing:
            self._metrics.record_skipped(WhatsAppSkipReason.NO_PHONE_NUMBER, missing)
            logger.info("whatsapp_recipient_unusable", count=missing)

        return rows

    # ------------------------------------------------------------ sending #

    def process(self, job: WhatsAppJob) -> bool:
        """Send one queued delivery. Returns whether the provider accepted it.

        **Never raises**, and it is the whole of what a WhatsApp worker thread
        runs. Every way a send can go wrong becomes a recorded state on the
        delivery row — retried with a backoff if the cause was transient,
        ``failed`` if it was not — and the notification, the case, the document,
        and the account are untouched throughout, because this service writes to
        one table.

        Public and synchronous so the feature is testable **without a thread**: a
        test hands it a job and asserts about a row, rather than queueing one and
        waiting to see. The worker loop is the only production caller.
        """
        if not self._deliveries.claim(job.delivery_id):
            # Somebody else has it, or it is no longer pending. Both are ordinary —
            # a sweeper re-queueing beside a live dispatch produces exactly this —
            # and both mean this call has nothing to do.
            logger.debug("whatsapp_claim_lost", delivery_id=str(job.delivery_id))
            return False

        delivery = self._deliveries.get(job.delivery_id)
        if delivery is None:  # pragma: no cover - claimed a row that then vanished
            return False

        logger.info(
            "whatsapp_sending",
            delivery_id=str(delivery.id),
            rule=delivery.rule_key,
            attempt=delivery.attempts,
        )

        try:
            message = self._compose(delivery)
        except (WhatsAppTemplateError, InvalidWhatsAppRecipientError) as exc:
            return self._record_failure(
                delivery,
                code=(
                    WhatsAppFailureCode.TEMPLATE_FAILURE
                    if isinstance(exc, WhatsAppTemplateError)
                    else WhatsAppFailureCode.INVALID_RECIPIENT
                ),
            )
        except _NotificationGoneError:
            return self._record_failure(
                delivery, code=WhatsAppFailureCode.MESSAGE_REFUSED
            )
        except Exception:
            logger.exception("whatsapp_compose_failed", delivery_id=str(delivery.id))
            return self._record_failure(delivery, code=WhatsAppFailureCode.UNKNOWN)

        result = self._provider.send(message)

        if not result.accepted:
            return self._record_failure(
                delivery,
                code=result.failure or WhatsAppFailureCode.UNKNOWN,
                provider=result.provider,
            )

        self._deliveries.mark_delivered(
            delivery.id,
            provider=result.provider,
            duration_ms=result.duration_ms,
            provider_message_id=result.message_id,
        )
        self._metrics.record_delivered(
            delivery.rule_key,
            latency_ms=_elapsed_ms(delivery.created_at),
            duration_ms=result.duration_ms,
        )
        logger.info(
            "whatsapp_delivered",
            delivery_id=str(delivery.id),
            rule=delivery.rule_key,
            provider=result.provider,
            attempts=delivery.attempts,
            duration_ms=round(result.duration_ms, 2),
        )
        return True

    def _compose(self, delivery: WhatsAppDelivery) -> OutgoingWhatsAppMessage:
        """Render one delivery into an addressed template message.

        **The notification is read back through the recipient-scoped query**, and
        that is not incidental. Every read in :mod:`repositories.notification` is
        keyed by recipient and there is deliberately no unscoped variant, so the
        worker asks *"this recipient's notification with this id"* — which means a
        message can only ever be composed from something the addressee is entitled
        to read. The spec's *"the WhatsApp Delivery Channel should trust the
        Notification Service and never broaden notification visibility"* is
        therefore enforced by the shape of the repository rather than by this
        function's good behaviour.

        Raises:
            _NotificationGoneError: the notification no longer exists or was
                archived.
            WhatsAppTemplateError: the descriptor is missing or could not be
                rendered.
            InvalidWhatsAppRecipientError: the account is no longer messageable.
        """
        notification = self._notifications.get(
            delivery.notification_id, recipient_id=delivery.recipient_id
        )
        if notification is None:
            logger.warning(
                "whatsapp_notification_missing", delivery_id=str(delivery.id)
            )
            raise _NotificationGoneError

        profile = self._deliveries.recipient_profiles([delivery.recipient_id]).get(
            delivery.recipient_id
        )
        if profile is None:
            # The account was deactivated, or its number removed, between queueing
            # and sending. Refusing is the right answer: a suspended user should not
            # be messaged a link into a platform they can no longer sign in to.
            logger.info("whatsapp_recipient_inactive", delivery_id=str(delivery.id))
            raise InvalidWhatsAppRecipientError(
                "The recipient's account is not messageable."
            )

        context = build_whatsapp_context(
            rule_key=notification.rule_key,
            category=notification.category,
            priority=notification.priority.value,
            context=notification.context,
            # A display name, and **never the number as a fallback**. The email
            # channel falls back to the local part of an address, which is a name
            # somebody chose; the equivalent here would greet a lawyer with their
            # own phone number, which is both odd and a piece of contact
            # information the platform has no reason to read back to them.
            recipient_name=profile.full_name or UNNAMED_RECIPIENT,
            language=delivery.language,
            base_url=settings.whatsapp_base_url,
            target_type=notification.target_type,
            target_id=notification.target_id,
            platform_name=settings.WHATSAPP_SENDER_NAME,
        )

        rendered = self._templates.render(
            delivery.template,
            version=delivery.template_version,
            context=context.as_mapping(),
        )

        return OutgoingWhatsAppMessage(
            to_number=delivery.recipient_phone,
            template_name=delivery.template,
            language_code=provider_language_code(delivery.language),
            parameters=rendered.parameters,
        )

    def _record_failure(
        self,
        delivery: WhatsAppDelivery,
        *,
        code: WhatsAppFailureCode,
        provider: str | None = None,
    ) -> bool:
        """Reschedule or give up, and say which in the log. Always returns ``False``.

        **The whole retry decision**, and it turns on two things and no others:
        whether the cause is in :data:`~core.whatsapp.TRANSIENT_FAILURE_CODES`, and
        whether the delivery has attempts left. Neither is a judgement made here —
        the first is a property of the code, the second is a column and a setting —
        which is what makes the behaviour the same whichever provider produced the
        failure.
        """
        exhausted = delivery.attempts >= max(1, settings.WHATSAPP_MAX_ATTEMPTS)

        if is_transient(code) and not exhausted:
            due = next_attempt_at(
                delivery.attempts,
                base=settings.WHATSAPP_RETRY_BACKOFF_SECONDS,
                cap=settings.WHATSAPP_RETRY_MAX_BACKOFF_SECONDS,
            )
            self._deliveries.reschedule(
                delivery.id, error_code=code.value, next_attempt=due, provider=provider
            )
            self._metrics.record_retry(code)
            logger.info(
                "whatsapp_retry_scheduled",
                delivery_id=str(delivery.id),
                rule=delivery.rule_key,
                error_code=code.value,
                attempt=delivery.attempts,
                # A duration, not a timestamp: a wall-clock time in a log line is
                # one more thing a reader has to convert.
                retry_in_seconds=round(
                    max(0.0, (due - datetime.now(UTC)).total_seconds()), 1
                ),
            )
            return False

        self._deliveries.mark_failed(
            delivery.id, error_code=code.value, provider=provider
        )
        self._metrics.record_failed(code)
        logger.warning(
            "whatsapp_delivery_failed",
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
        interval by :mod:`services.whatsapp_worker`, and once at startup — which is
        also the recovery for a process that was stopped with work queued, since a
        ``pending`` row whose schedule lived in a dead process's memory is exactly
        what this looks for.

        Bounded per pass (``WHATSAPP_RETRY_BATCH_SIZE``), because a provider that
        rate-limited the platform overnight leaves a backlog rather than a page,
        and loading all of it to re-queue it is how a recovery becomes its own
        outage — which on this channel would also be a burst that gets the business
        number throttled again. The next pass takes the next batch.

        Returns:
            How many deliveries were handed back to the queue. Never raises: this
            runs on a timer thread with nobody to report to.
        """
        if not settings.WHATSAPP_ENABLED:
            return 0

        try:
            reclaimed = self._deliveries.reclaim_stale(
                older_than=datetime.now(UTC)
                - timedelta(seconds=settings.WHATSAPP_STALE_SENDING_SECONDS)
            )
            if reclaimed:
                logger.warning("whatsapp_stale_reclaimed", count=reclaimed)

            due = self._deliveries.due_deliveries(
                limit=settings.WHATSAPP_RETRY_BATCH_SIZE
            )
        except Exception:
            logger.exception("whatsapp_sweep_failed")
            return 0

        for delivery_id in due:
            self._queue.enqueue(WhatsAppJob(delivery_id=delivery_id))

        if due:
            logger.info("whatsapp_requeued", count=len(due))
        return len(due)

    # ---------------------------------------------------------- monitoring #

    def metrics(self, *, window_days: int | None = None) -> WhatsAppDeliveryMetrics:
        """Return platform-wide WhatsApp delivery health.

        Args:
            window_days: apply a window to the **SQL** figures. The in-process
                counters have their own window — the process's lifetime — which is
                reported as ``since`` rather than pretending the two are the same
                thing.
        """
        since = (
            datetime.now(UTC) - timedelta(days=window_days)
            if window_days is not None
            else None
        )
        return WhatsAppDeliveryMetrics(
            statistics=self._deliveries.statistics(since=since),
            counters=self._metrics.snapshot(),
            enabled=settings.WHATSAPP_ENABLED,
            provider=self._provider.name,
            provider_available=self._provider.is_available(),
            configuration_errors=self._provider.configuration_errors(),
            templates_available=self._templates.is_available(),
            window_days=window_days,
        )


class _NotificationGoneError(RuntimeError):
    """The notification a queued delivery carries no longer exists.

    Private, and a distinct type rather than a ``None`` return, so
    :meth:`WhatsAppDeliveryService.process` can map it onto a **permanent**
    failure: a notification that was deleted or archived is not going to come back,
    and retrying would be four more attempts to render something that is gone.
    """


#: What fills the greeting for an account with no name on it.
#:
#: Named rather than written at the call site because it is **user-facing text**,
#: and `ai-workflow-rules.md` requires that to be findable. It is deliberately not
#: localized: it fills the ``{name}`` slot of a greeting that is already in the
#: reader's language (``"Bonjour {name}"``, ``"مرحبًا {name}"``), so a *word* here
#: would need translating while a neutral mark reads the same in all three. It is
#: also very nearly unreachable — ``users.first_name`` and ``users.last_name`` are
#: both required — which is exactly why it must not be a phone number.
UNNAMED_RECIPIENT = "—"


def _elapsed_ms(reference: datetime) -> float:
    """Milliseconds from ``reference`` until now, never negative.

    ``reference`` is read as UTC when it carries no timezone: SQLite returns naive
    datetimes for a ``TIMESTAMP WITH TIME ZONE`` column, so a latency computed
    against one would raise rather than be slightly wrong. The same helper
    :mod:`services.email_delivery` carries, for the same reason.
    """
    aware = reference if reference.tzinfo is not None else reference.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - aware).total_seconds() * 1000.0)


def build_whatsapp_delivery_service(
    session: Session,
    *,
    provider: WhatsAppProvider,
    templates: WhatsAppTemplateRenderer,
    queue: JobQueue[WhatsAppJob] | None = None,
    metrics: WhatsAppMetricsRecorder | None = None,
) -> WhatsAppDeliveryService:
    """Assemble a delivery service on one session.

    A small factory, and it exists because this service is built in **three**
    places that have nothing else in common — a request dependency, the
    notification worker's thread, and the WhatsApp worker's own thread — and each
    assembling it by hand is three places for the collaborator list to drift.

    The language directory is built on the **same session** and handed over as the
    one-method :class:`~services.localization.LanguageDirectory`, exactly as the
    email channel's is — the two channels resolve the same fact about the same
    person and must not be able to disagree about it.
    """
    return WhatsAppDeliveryService(
        WhatsAppDeliveryRepository(session),
        NotificationRepository(session),
        provider,
        templates,
        queue,
        metrics=metrics,
        languages=build_language_directory(
            session, channel_default=settings.WHATSAPP_DEFAULT_LANGUAGE
        ),
    )


__all__ = [
    "UNNAMED_RECIPIENT",
    "WhatsAppDeliveryMetrics",
    "WhatsAppDeliveryService",
    "WhatsAppJob",
    "build_whatsapp_delivery_service",
]
