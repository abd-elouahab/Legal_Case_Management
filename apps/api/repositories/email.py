"""Email delivery data access.

Single responsibility: reading and persisting
:class:`~models.email.EmailDelivery` rows. No authorization rules, no rendering,
no provider knowledge, no retry policy — those belong to
:mod:`services.email_delivery`, :mod:`services.email_templates`,
:mod:`services.email_provider`, and :mod:`core.email` respectively.

Four things are load-bearing rather than routine:

* **Concurrency is a conditional ``UPDATE``, not a lock.**
  :meth:`EmailDeliveryRepository.claim` moves a delivery ``pending → sending``
  with ``WHERE status = 'pending'``, so exactly one worker updates a row and any
  other updates none. No Redis key, nothing to expire or leak — the row *is* the
  lock, held for exactly as long as its state says. The same mechanism
  ``ocr_results`` and ``document_indexes`` use, and the reason this feature's
  queue is one file to replace.
* **Queueing is idempotent by constraint, not by check.**
  :meth:`EmailDeliveryRepository.create_many` inserts one row per notification
  against a unique index on ``notification_id``, and
  :meth:`EmailDeliveryRepository.existing_notification_ids` is the ordinary path
  that keeps that constraint from being the thing that discovers a duplicate.
  Both are needed and they cover different halves: the query keeps the common
  case cheap, the constraint makes the guarantee true under concurrency.
* **The retry sweep is one bounded query.**
  :meth:`EmailDeliveryRepository.due_deliveries` asks for what is queued and due,
  oldest first, capped — because "everything that is pending" on a deployment
  whose relay was down for a day is not a page, it is the backlog, and loading it
  into memory to re-queue it is how a recovery becomes an outage.
* **Nothing here reads a notification's context or a user's name.** The service
  joins what it needs; this module returns delivery rows and aggregates. That is
  what keeps a query in this file from being the place an address or a case
  number leaks into a log line built around a repository result.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, asc, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from models.email import EmailDelivery, EmailDeliveryStatus


@dataclass(frozen=True, slots=True)
class EmailRecipientProfile:
    """The three things about an account an email actually needs.

    A narrow value rather than a :class:`~models.user.User`, and deliberately: a
    ``User`` carries a password hash, a session generation, and a role, none of
    which has any business being reachable from a mail template. Passing the
    identifier, the address, and the display name means the template layer
    *cannot* reach the rest, which is a stronger guarantee than remembering not
    to.
    """

    user_id: uuid.UUID
    email: str
    full_name: str


@dataclass(frozen=True, slots=True)
class EmailDeliveryStatistics:
    """Platform-wide figures that are properties of rows rather than of a process.

    Everything a monitoring view can know exactly and permanently: how many
    deliveries exist in each state, how many people have been written to, and how
    many attempts the platform has made. Retry *rate* and latency are **not**
    here — an attempt is not a row, so those accumulate in the process behind
    :class:`~services.email_metrics.EmailMetricsRecorder`.
    """

    total: int = 0
    #: Waiting for a worker, or waiting out a retry backoff. **The spec's "queued
    #: emails"**, and the figure that matters most after a restart.
    pending: int = 0
    #: Claimed by a worker right now. A number that stays high is the signature of
    #: a process that died mid-send; see
    #: :meth:`EmailDeliveryRepository.reclaim_stale`.
    sending: int = 0
    sent: int = 0
    failed: int = 0
    #: Distinct accounts the platform has written to. A count, never a list.
    recipients: int = 0
    #: Send attempts made across every delivery in the window, including the ones
    #: that succeeded first time.
    attempts: int = 0
    by_failure_code: dict[str, int] = field(default_factory=dict)

    @property
    def delivery_rate(self) -> float:
        """Share of finished deliveries that were sent, as a percentage.

        ``0.0`` when none has finished — there is nothing to have succeeded at
        yet. Same shape and reasoning as every other rate on this platform.
        """
        finished = self.sent + self.failed
        if finished <= 0:
            return 0.0
        return round(self.sent / finished * 100, 2)


class EmailDeliveryRepository:
    """Queries and mutations for the ``email_deliveries`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get(self, delivery_id: uuid.UUID) -> EmailDelivery | None:
        """Return one delivery, or ``None``.

        Unscoped, and that is correct here rather than an omission: a delivery is
        an **operational record**, not a resource anybody reads through the API.
        There is no endpoint behind this method — the only callers are the worker
        processing a job it was handed and the sweeper re-queueing one — so there
        is no caller whose identity it could be scoped to. The moment a
        client-facing read of this table is added, it gets a scoped method of its
        own, exactly as :mod:`repositories.notification` has.
        """
        return self._session.execute(
            select(EmailDelivery).where(EmailDelivery.id == delivery_id)
        ).scalar_one_or_none()

    def existing_notification_ids(
        self, notification_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these notifications already have a delivery.

        The ordinary half of duplicate prevention, and the query *behind* the
        unique constraint rather than a replacement for it: the constraint is what
        makes the guarantee true under concurrency, and this is what keeps the
        common path from provoking an integrity error to discover something it
        could have asked.

        One statement for the whole batch, so a re-dispatched event about a
        three-person case costs one query rather than three.
        """
        if not notification_ids:
            return set()

        rows = self._session.execute(
            select(EmailDelivery.notification_id).where(
                EmailDelivery.notification_id.in_(list(notification_ids))
            )
        ).scalars()
        return set(rows)

    def due_deliveries(self, *, limit: int, now: datetime | None = None) -> list[uuid.UUID]:
        """Identifiers of the deliveries that are queued and due, oldest first.

        **Identifiers only, and bounded.** The sweeper's job is to hand work to a
        queue, and a queue takes an identifier; returning whole rows would load a
        backlog into memory to read one column off each. The bound is what keeps a
        recovery from being its own outage — a relay that was down overnight
        leaves thousands of rows, and the sweeper takes them a batch per pass.

        ``next_attempt_at IS NULL`` is included because a first attempt has no
        schedule: it is due the moment it is written. Expressed as an explicit
        ``OR`` rather than relying on how a dialect sorts NULLs, since the
        platform runs SQLite in tests and PostgreSQL in production and the two do
        not have to agree.
        """
        reference = now or datetime.now(UTC)
        rows = self._session.execute(
            select(EmailDelivery.id)
            .where(
                EmailDelivery.status == EmailDeliveryStatus.PENDING,
                or_(
                    EmailDelivery.next_attempt_at.is_(None),
                    EmailDelivery.next_attempt_at <= reference,
                ),
            )
            .order_by(asc(EmailDelivery.created_at), asc(EmailDelivery.id))
            .limit(max(1, limit))
        ).scalars()
        return list(rows)

    def recipient_profiles(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, EmailRecipientProfile]:
        """The address and display name of each account, in one query.

        The one query in this feature that reads the ``users`` table, and it is
        here rather than on :class:`~repositories.user.UserRepository`
        deliberately — the same reasoning
        :meth:`~repositories.notification.NotificationRepository.active_recipient_ids`
        records. That repository's reads serve the **administrative user
        directory**: they carry search, filters, sort fields, and a page ceiling
        that exist for a screen, and borrowing one would couple an email batch to
        the page size of an unrelated admin table.

        **Active accounts only.** A suspended or deactivated user cannot sign in,
        so mailing them a link to a case would be an invitation to a door that is
        closed — and the notification itself was already withheld from them by
        :class:`~services.notification_recipients.NotificationRecipientResolver`,
        so this is the second of two independent checks rather than the only one.

        Three columns and no more: an identifier, an address, and the two name
        parts a greeting needs. A method that returned ``User`` objects would put
        a password hash and a session generation into the hands of a mail
        template.
        """
        if not user_ids:
            return {}

        # Imported here rather than at module scope: this is the only method that
        # touches the users table, and a module-level import would suggest the
        # repository is about users as much as about deliveries.
        from models.user import User, UserStatus

        rows = self._session.execute(
            select(User.id, User.email, User.first_name, User.last_name).where(
                User.id.in_(list(user_ids)), User.status == UserStatus.ACTIVE
            )
        ).all()
        return {
            user_id: EmailRecipientProfile(
                user_id=user_id,
                email=str(email or ""),
                full_name=f"{first_name or ''} {last_name or ''}".strip(),
            )
            for user_id, email, first_name, last_name in rows
        }

    # ------------------------------------------------------------- writing #

    def create_many(self, deliveries: Sequence[EmailDelivery]) -> list[EmailDelivery]:
        """Persist a batch of queued deliveries in one transaction.

        One flush and one commit for the whole fan-out, which is the spec's
        *"support batch delivery"* at the place it actually pays: an event about a
        case queues an email for everyone party to it, and a commit per recipient
        would make the cost of a hearing change proportional to the size of the
        team.

        The batch is deliberately **all-or-nothing**, matching
        :meth:`~repositories.notification.NotificationRepository.create_many`: a
        partial commit would leave some people queued and others not, with nothing
        to say which — and the caller's failure path (log it, count it, move on)
        is the same either way.
        """
        if not deliveries:
            return []

        self._session.add_all(list(deliveries))
        self._session.commit()
        for delivery in deliveries:
            self._session.refresh(delivery)
        return list(deliveries)

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when a write fails part-way: a failed flush leaves the session
        unusable until the transaction is rolled back.
        """
        self._session.rollback()

    def claim(self, delivery_id: uuid.UUID, *, now: datetime | None = None) -> bool:
        """Move one delivery ``pending → sending``. Returns whether this call won.

        **The concurrency control, and it is a statement rather than a lock.**
        ``WHERE status = 'pending'`` means exactly one caller updates the row and
        every other updates none, so two workers handed the same job — which a
        sweeper re-queueing beside a live dispatch can genuinely produce — cannot
        both send the message.

        ``started_at`` is stamped here rather than by the caller, so the
        "how long has this been sending?" the stale reclaim depends on is written
        by the same statement that made it true.
        """
        stamp = now or datetime.now(UTC)
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(EmailDelivery)
                .where(
                    EmailDelivery.id == delivery_id,
                    EmailDelivery.status == EmailDeliveryStatus.PENDING,
                )
                .values(
                    status=EmailDeliveryStatus.SENDING,
                    started_at=stamp,
                    attempts=EmailDelivery.attempts + 1,
                    next_attempt_at=None,
                )
            ),
        )
        self._session.commit()
        return bool(outcome.rowcount)

    def mark_sent(
        self,
        delivery_id: uuid.UUID,
        *,
        provider: str,
        duration_ms: float,
        now: datetime | None = None,
    ) -> bool:
        """Record that a provider accepted the message.

        Conditional on the row still being ``sending``, for the same reason the
        claim is conditional: a delivery reclaimed as stale underneath a worker
        that then succeeds must not overwrite whatever the reclaim decided.
        """
        stamp = now or datetime.now(UTC)
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(EmailDelivery)
                .where(
                    EmailDelivery.id == delivery_id,
                    EmailDelivery.status == EmailDeliveryStatus.SENDING,
                )
                .values(
                    status=EmailDeliveryStatus.SENT,
                    sent_at=stamp,
                    provider=provider,
                    duration_ms=int(max(0.0, duration_ms)),
                    error_code=None,
                    next_attempt_at=None,
                )
            ),
        )
        self._session.commit()
        return bool(outcome.rowcount)

    def mark_failed(
        self, delivery_id: uuid.UUID, *, error_code: str, provider: str | None = None
    ) -> bool:
        """Record that the platform has given up on this delivery.

        Terminal. The notification itself is **untouched** — still in the
        recipient's feed, still unread, still exactly as useful as it was — which
        is the spec's *"failures should never interrupt application
        functionality"* made structural: this service writes to one table, so
        there is nothing else it could damage.
        """
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(EmailDelivery)
                .where(
                    EmailDelivery.id == delivery_id,
                    EmailDelivery.status == EmailDeliveryStatus.SENDING,
                )
                .values(
                    status=EmailDeliveryStatus.FAILED,
                    error_code=error_code,
                    provider=provider,
                    next_attempt_at=None,
                )
            ),
        )
        self._session.commit()
        return bool(outcome.rowcount)

    def reschedule(
        self,
        delivery_id: uuid.UUID,
        *,
        error_code: str,
        next_attempt: datetime,
        provider: str | None = None,
    ) -> bool:
        """Return a delivery to the queue after a transient failure.

        ``sending → pending`` with a time in the future, which is the whole of the
        retry mechanism as far as storage is concerned: the attempt count is
        already on the row (the claim incremented it), the delay is in
        ``next_attempt_at``, and the sweeper picks it up when it comes due.

        **A worker never sleeps out a backoff.** An hour-long delay held in a
        ``time.sleep`` would occupy one of two worker threads for an hour and lose
        the schedule entirely on restart; here the schedule is a column and
        survives both.
        """
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(EmailDelivery)
                .where(
                    EmailDelivery.id == delivery_id,
                    EmailDelivery.status == EmailDeliveryStatus.SENDING,
                )
                .values(
                    status=EmailDeliveryStatus.PENDING,
                    error_code=error_code,
                    provider=provider,
                    next_attempt_at=next_attempt,
                    started_at=None,
                )
            ),
        )
        self._session.commit()
        return bool(outcome.rowcount)

    def reclaim_stale(self, *, older_than: datetime) -> int:
        """Return deliveries stuck in ``sending`` to the queue. Returns how many.

        The recovery for a process that died mid-send. ``sending`` is the one
        state no other worker will claim, so without this a delivery interrupted
        by a deployment would sit there forever — the same failure
        :meth:`~repositories.report.ReportRepository` guards against for a report
        left ``processing``, and the reason both are recoverable rather than
        terminal.

        Safe to run repeatedly and safe to run while workers are live: the
        threshold is generous enough that a send in flight is never older than it,
        and a row reclaimed underneath a worker that then succeeds is refused by
        :meth:`mark_sent`'s own ``WHERE`` clause rather than being overwritten.
        """
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(EmailDelivery)
                .where(
                    EmailDelivery.status == EmailDeliveryStatus.SENDING,
                    EmailDelivery.started_at.is_not(None),
                    EmailDelivery.started_at < older_than,
                )
                .values(status=EmailDeliveryStatus.PENDING, next_attempt_at=None)
            ),
        )
        self._session.commit()
        return int(outcome.rowcount or 0)

    # ---------------------------------------------------------- monitoring #

    def statistics(self, *, since: datetime | None = None) -> EmailDeliveryStatistics:
        """Aggregate deliveries by state, recipients, attempts, and failure cause.

        Computed in the database rather than by loading rows, for the same reason
        every list endpoint pushes its filters into SQL: the cost must not grow
        with the number of emails the platform has ever sent.
        """
        totals = self._session.execute(
            self._windowed(
                select(
                    func.count().label("total"),
                    func.count()
                    .filter(EmailDelivery.status == EmailDeliveryStatus.PENDING)
                    .label("pending"),
                    func.count()
                    .filter(EmailDelivery.status == EmailDeliveryStatus.SENDING)
                    .label("sending"),
                    func.count()
                    .filter(EmailDelivery.status == EmailDeliveryStatus.SENT)
                    .label("sent"),
                    func.count()
                    .filter(EmailDelivery.status == EmailDeliveryStatus.FAILED)
                    .label("failed"),
                    func.count(func.distinct(EmailDelivery.recipient_id)).label("recipients"),
                    func.coalesce(func.sum(EmailDelivery.attempts), 0).label("attempts"),
                ).select_from(EmailDelivery),
                since=since,
            )
        ).one()

        failures = self._session.execute(
            self._windowed(
                select(
                    EmailDelivery.error_code,
                    # Deliberately **not** labelled ``count``: a SQLAlchemy ``Row``
                    # is tuple-like and already has a ``count`` *method*, so that
                    # label would shadow it and read back as a bound method rather
                    # than a number — silently, and only at runtime. The same note
                    # `NotificationRepository.counts` carries.
                    func.count().label("failure_count"),
                ).where(
                    EmailDelivery.status == EmailDeliveryStatus.FAILED,
                    EmailDelivery.error_code.is_not(None),
                ),
                since=since,
            ).group_by(EmailDelivery.error_code)
        ).all()

        return EmailDeliveryStatistics(
            total=int(totals.total or 0),
            pending=int(totals.pending or 0),
            sending=int(totals.sending or 0),
            sent=int(totals.sent or 0),
            failed=int(totals.failed or 0),
            recipients=int(totals.recipients or 0),
            attempts=int(totals.attempts or 0),
            by_failure_code={
                str(code): int(count) for code, count in failures if code is not None
            },
        )

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _windowed(statement: Select[Any], *, since: datetime | None) -> Select[Any]:
        """Apply the monitoring window to an aggregate."""
        if since is not None:
            return statement.where(EmailDelivery.created_at >= since)
        return statement


__all__ = [
    "EmailDeliveryRepository",
    "EmailDeliveryStatistics",
    "EmailRecipientProfile",
]
