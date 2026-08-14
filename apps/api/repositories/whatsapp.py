"""WhatsApp delivery data access.

Single responsibility: reading and persisting
:class:`~models.whatsapp.WhatsAppDelivery` rows. No authorization rules, no
rendering, no provider knowledge, no retry policy — those belong to
:mod:`services.whatsapp_delivery`, :mod:`services.whatsapp_templates`,
:mod:`services.whatsapp_provider`, and :mod:`core.whatsapp` respectively.

Four things are load-bearing rather than routine, and each is the same mechanism
:mod:`repositories.email` uses — which is the point: the second outbound channel
needed no new machinery at all, only its own table.

* **Concurrency is a conditional ``UPDATE``, not a lock.**
  :meth:`WhatsAppDeliveryRepository.claim` moves a delivery ``pending → sending``
  with ``WHERE status = 'pending'``, so exactly one worker updates a row and any
  other updates none. No Redis key, nothing to expire or leak — the row *is* the
  lock, held for exactly as long as its state says.
* **Queueing is idempotent by constraint, not by check.**
  :meth:`WhatsAppDeliveryRepository.create_many` inserts one row per notification
  against a unique index on ``notification_id``, and
  :meth:`WhatsAppDeliveryRepository.existing_notification_ids` is the ordinary
  path that keeps that constraint from being the thing that discovers a
  duplicate. Both are needed and they cover different halves: the query keeps the
  common case cheap, the constraint makes the guarantee true under concurrency.
* **The retry sweep is one bounded query.**
  :meth:`WhatsAppDeliveryRepository.due_deliveries` asks for what is queued and
  due, oldest first, capped — because "everything that is pending" on a
  deployment that spent a day rate-limited is not a page, it is the backlog.
* **Nothing here reads a notification's context or a user's name.** The service
  joins what it needs; this module returns delivery rows and aggregates. That is
  what keeps a query in this file from being the place a phone number or a case
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

from models.whatsapp import WhatsAppDelivery, WhatsAppDeliveryStatus


@dataclass(frozen=True, slots=True)
class WhatsAppRecipientProfile:
    """The three things about an account a WhatsApp message actually needs.

    A narrow value rather than a :class:`~models.user.User`, and deliberately: a
    ``User`` carries a password hash, a session generation, and a role, none of
    which has any business being reachable from a message template. Passing the
    identifier, the number, and the display name means the template layer *cannot*
    reach the rest, which is a stronger guarantee than remembering not to.
    """

    user_id: uuid.UUID
    #: Whatever is stored on the account, **unnormalized**. Turning it into an
    #: E.164 recipient is :func:`~core.whatsapp.normalize_phone`'s job, and it is
    #: not this module's to do — a repository that silently rewrote a column's
    #: value on the way out would make "what is actually stored?" unanswerable
    #: from the code that reads it.
    phone: str
    full_name: str


@dataclass(frozen=True, slots=True)
class WhatsAppDeliveryStatistics:
    """Platform-wide figures that are properties of rows rather than of a process.

    Everything a monitoring view can know exactly and permanently: how many
    deliveries exist in each state, how many people have been messaged, and how
    many attempts the platform has made. Retry *rate* and latency are **not**
    here — an attempt is not a row, so those accumulate in the process behind
    :class:`~services.whatsapp_metrics.WhatsAppMetricsRecorder`.
    """

    total: int = 0
    #: Waiting for a worker, or waiting out a retry backoff. **The spec's "queued
    #: messages"**, and the figure that matters most after a restart.
    pending: int = 0
    #: Claimed by a worker right now. A number that stays high is the signature of
    #: a process that died mid-send; see
    #: :meth:`WhatsAppDeliveryRepository.reclaim_stale`.
    sending: int = 0
    #: Accepted by the provider. **The spec's "delivered messages"** — see
    #: :attr:`~models.whatsapp.WhatsAppDeliveryStatus.DELIVERED` for what the
    #: platform can and cannot honestly claim by that word.
    delivered: int = 0
    failed: int = 0
    #: Distinct accounts the platform has messaged. A count, never a list.
    recipients: int = 0
    #: Send attempts made across every delivery in the window, including the ones
    #: that succeeded first time.
    attempts: int = 0
    by_failure_code: dict[str, int] = field(default_factory=dict)

    @property
    def delivery_rate(self) -> float:
        """Share of finished deliveries that were accepted, as a percentage.

        ``0.0`` when none has finished — there is nothing to have succeeded at
        yet. Same shape and reasoning as every other rate on this platform.
        """
        finished = self.delivered + self.failed
        if finished <= 0:
            return 0.0
        return round(self.delivered / finished * 100, 2)


class WhatsAppDeliveryRepository:
    """Queries and mutations for the ``whatsapp_deliveries`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get(self, delivery_id: uuid.UUID) -> WhatsAppDelivery | None:
        """Return one delivery, or ``None``.

        Unscoped, and that is correct here rather than an omission, for the reason
        :meth:`~repositories.email.EmailDeliveryRepository.get` records: a delivery
        is an **operational record**, not a resource anybody reads through the
        API. There is no endpoint behind this method — the only callers are the
        worker processing a job it was handed and the sweeper re-queueing one — so
        there is no caller whose identity it could be scoped to.
        """
        return self._session.execute(
            select(WhatsAppDelivery).where(WhatsAppDelivery.id == delivery_id)
        ).scalar_one_or_none()

    def existing_notification_ids(
        self, notification_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these notifications already have a delivery.

        The ordinary half of duplicate prevention, and the query *behind* the
        unique constraint rather than a replacement for it. One statement for the
        whole batch, so a re-dispatched event about a three-person case costs one
        query rather than three.
        """
        if not notification_ids:
            return set()

        rows = self._session.execute(
            select(WhatsAppDelivery.notification_id).where(
                WhatsAppDelivery.notification_id.in_(list(notification_ids))
            )
        ).scalars()
        return set(rows)

    def due_deliveries(
        self, *, limit: int, now: datetime | None = None
    ) -> list[uuid.UUID]:
        """Identifiers of the deliveries that are queued and due, oldest first.

        **Identifiers only, and bounded.** The sweeper's job is to hand work to a
        queue, and a queue takes an identifier; returning whole rows would load a
        backlog into memory to read one column off each.

        ``next_attempt_at IS NULL`` is included because a first attempt has no
        schedule: it is due the moment it is written. Expressed as an explicit
        ``OR`` rather than relying on how a dialect sorts NULLs, since the platform
        runs SQLite in tests and PostgreSQL in production and the two do not have
        to agree.
        """
        reference = now or datetime.now(UTC)
        rows = self._session.execute(
            select(WhatsAppDelivery.id)
            .where(
                WhatsAppDelivery.status == WhatsAppDeliveryStatus.PENDING,
                or_(
                    WhatsAppDelivery.next_attempt_at.is_(None),
                    WhatsAppDelivery.next_attempt_at <= reference,
                ),
            )
            .order_by(asc(WhatsAppDelivery.created_at), asc(WhatsAppDelivery.id))
            .limit(max(1, limit))
        ).scalars()
        return list(rows)

    def recipient_profiles(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, WhatsAppRecipientProfile]:
        """The number and display name of each account, in one query.

        The one query in this feature that reads the ``users`` table, and it is
        here rather than on :class:`~repositories.user.UserRepository`
        deliberately — the same reasoning
        :meth:`~repositories.email.EmailDeliveryRepository.recipient_profiles`
        records. That repository's reads serve the **administrative user
        directory**: they carry search, filters, sort fields, and a page ceiling
        that exist for a screen, and borrowing one would couple a message batch to
        the page size of an unrelated admin table.

        **Active accounts only, and accounts with a number only.** A suspended or
        deactivated user cannot sign in, so messaging them a link to a case would
        be an invitation to a door that is closed. An account with a blank
        ``phone`` is excluded **in SQL** rather than in Python because most
        accounts on this platform have one — ``07-user-management`` made it
        optional — and filtering after the fact would load every recipient of every
        batch to discard most of them.

        Four columns and no more: an identifier, a number, and the two name parts
        a greeting needs.
        """
        if not user_ids:
            return {}

        # Imported here rather than at module scope: this is the only method that
        # touches the users table, and a module-level import would suggest the
        # repository is about users as much as about deliveries.
        from models.user import User, UserStatus

        rows = self._session.execute(
            select(User.id, User.phone, User.first_name, User.last_name).where(
                User.id.in_(list(user_ids)),
                User.status == UserStatus.ACTIVE,
                User.phone.is_not(None),
                User.phone != "",
            )
        ).all()
        return {
            user_id: WhatsAppRecipientProfile(
                user_id=user_id,
                phone=str(phone or ""),
                full_name=f"{first_name or ''} {last_name or ''}".strip(),
            )
            for user_id, phone, first_name, last_name in rows
        }

    # ------------------------------------------------------------- writing #

    def create_many(
        self, deliveries: Sequence[WhatsAppDelivery]
    ) -> list[WhatsAppDelivery]:
        """Persist a batch of queued deliveries in one transaction.

        One flush and one commit for the whole fan-out, which is the spec's
        *"support batch delivery"* at the place it actually pays: an event about a
        case queues a message for everyone party to it, and a commit per recipient
        would make the cost of a hearing change proportional to the size of the
        team.

        The batch is deliberately **all-or-nothing**, matching
        :meth:`~repositories.email.EmailDeliveryRepository.create_many`: a partial
        commit would leave some people queued and others not, with nothing to say
        which — and the caller's failure path (log it, count it, move on) is the
        same either way.
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

        ``started_at`` is stamped here rather than by the caller, so the "how long
        has this been sending?" the stale reclaim depends on is written by the same
        statement that made it true.
        """
        stamp = now or datetime.now(UTC)
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(WhatsAppDelivery)
                .where(
                    WhatsAppDelivery.id == delivery_id,
                    WhatsAppDelivery.status == WhatsAppDeliveryStatus.PENDING,
                )
                .values(
                    status=WhatsAppDeliveryStatus.SENDING,
                    started_at=stamp,
                    attempts=WhatsAppDelivery.attempts + 1,
                    next_attempt_at=None,
                )
            ),
        )
        self._session.commit()
        return bool(outcome.rowcount)

    def mark_delivered(
        self,
        delivery_id: uuid.UUID,
        *,
        provider: str,
        duration_ms: float,
        provider_message_id: str | None = None,
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
                update(WhatsAppDelivery)
                .where(
                    WhatsAppDelivery.id == delivery_id,
                    WhatsAppDelivery.status == WhatsAppDeliveryStatus.SENDING,
                )
                .values(
                    status=WhatsAppDeliveryStatus.DELIVERED,
                    delivered_at=stamp,
                    provider=provider,
                    provider_message_id=provider_message_id,
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
                update(WhatsAppDelivery)
                .where(
                    WhatsAppDelivery.id == delivery_id,
                    WhatsAppDelivery.status == WhatsAppDeliveryStatus.SENDING,
                )
                .values(
                    status=WhatsAppDeliveryStatus.FAILED,
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
                update(WhatsAppDelivery)
                .where(
                    WhatsAppDelivery.id == delivery_id,
                    WhatsAppDelivery.status == WhatsAppDeliveryStatus.SENDING,
                )
                .values(
                    status=WhatsAppDeliveryStatus.PENDING,
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
        by a deployment would sit there forever.

        Safe to run repeatedly and safe to run while workers are live: the
        threshold is generous enough that a send in flight is never older than it,
        and a row reclaimed underneath a worker that then succeeds is refused by
        :meth:`mark_delivered`'s own ``WHERE`` clause rather than being
        overwritten.
        """
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(WhatsAppDelivery)
                .where(
                    WhatsAppDelivery.status == WhatsAppDeliveryStatus.SENDING,
                    WhatsAppDelivery.started_at.is_not(None),
                    WhatsAppDelivery.started_at < older_than,
                )
                .values(status=WhatsAppDeliveryStatus.PENDING, next_attempt_at=None)
            ),
        )
        self._session.commit()
        return int(outcome.rowcount or 0)

    # ---------------------------------------------------------- monitoring #

    def statistics(self, *, since: datetime | None = None) -> WhatsAppDeliveryStatistics:
        """Aggregate deliveries by state, recipients, attempts, and failure cause.

        Computed in the database rather than by loading rows, for the same reason
        every list endpoint pushes its filters into SQL: the cost must not grow
        with the number of messages the platform has ever sent.
        """
        totals = self._session.execute(
            self._windowed(
                select(
                    func.count().label("total"),
                    func.count()
                    .filter(WhatsAppDelivery.status == WhatsAppDeliveryStatus.PENDING)
                    .label("pending"),
                    func.count()
                    .filter(WhatsAppDelivery.status == WhatsAppDeliveryStatus.SENDING)
                    .label("sending"),
                    func.count()
                    .filter(WhatsAppDelivery.status == WhatsAppDeliveryStatus.DELIVERED)
                    .label("delivered"),
                    func.count()
                    .filter(WhatsAppDelivery.status == WhatsAppDeliveryStatus.FAILED)
                    .label("failed"),
                    func.count(func.distinct(WhatsAppDelivery.recipient_id)).label(
                        "recipients"
                    ),
                    func.coalesce(func.sum(WhatsAppDelivery.attempts), 0).label("attempts"),
                ).select_from(WhatsAppDelivery),
                since=since,
            )
        ).one()

        failures = self._session.execute(
            self._windowed(
                select(
                    WhatsAppDelivery.error_code,
                    # Deliberately **not** labelled ``count``: a SQLAlchemy ``Row``
                    # is tuple-like and already has a ``count`` *method*, so that
                    # label would shadow it and read back as a bound method rather
                    # than a number — silently, and only at runtime.
                    func.count().label("failure_count"),
                ).where(
                    WhatsAppDelivery.status == WhatsAppDeliveryStatus.FAILED,
                    WhatsAppDelivery.error_code.is_not(None),
                ),
                since=since,
            ).group_by(WhatsAppDelivery.error_code)
        ).all()

        return WhatsAppDeliveryStatistics(
            total=int(totals.total or 0),
            pending=int(totals.pending or 0),
            sending=int(totals.sending or 0),
            delivered=int(totals.delivered or 0),
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
            return statement.where(WhatsAppDelivery.created_at >= since)
        return statement


__all__ = [
    "WhatsAppDeliveryRepository",
    "WhatsAppDeliveryStatistics",
    "WhatsAppRecipientProfile",
]
