"""Notification data access.

Single responsibility: reading and persisting :class:`~models.notification.Notification`
and :class:`~models.notification.NotificationPreference` rows. No authorization
rules, no event interpretation, no rendering — those belong to
``services/notification_recipients.py``, ``services/notification_events.py``, and
``core/notifications.py`` respectively.

Two tables in one repository, unlike most modules here and for the same reason
:mod:`repositories.conversation` holds three: they are one module's storage, they
are written in the same transaction, and a preference exists only to decide
whether a notification is created.

Four things are load-bearing rather than routine:

* **Every read takes a ``recipient_id`` and puts it in the ``WHERE`` clause.**
  ``16-notifications.md`` requires that history *"remain user-specific"*, and
  this is where that is made true. There is deliberately **no method that
  resolves a notification by identifier alone**, so no call site can forget to
  scope one — the same shape :mod:`repositories.conversation` and
  :mod:`repositories.report` have, and the reason this feature needs no
  per-resource access module for *reading*.
* :meth:`NotificationRepository.create_many` is a **single batched insert**,
  which is the spec's *"batch operations where appropriate"* at the one place it
  matters: an event about a case fans out to everyone party to it, and a round
  trip per recipient would make a three-person case three inserts.
* :meth:`NotificationRepository.existing_event_recipients` and
  :meth:`NotificationRepository.recent_dedupe_recipients` are the **two duplicate
  checks**, and they are two rather than one because they catch different things
  — see :func:`~core.notifications.dedupe_key`. Both answer for a whole batch of
  recipients in one query, so duplicate prevention costs two statements per
  event rather than two per person.
* :meth:`NotificationRepository.unread_count` is **capped in SQL**. The badge is
  this feature's most frequent read, and an exact count over a user with four
  thousand unread rows is a scan to render something that would display "999+".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, asc, case, desc, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from core.notifications import (
    PRIORITY_RANK,
    ChannelPreference,
    ChannelPreferenceUpdate,
    NotificationChannel,
    NotificationPriority,
    default_preference,
    preference_from_value,
)
from models.notification import Notification, NotificationPreference
from schemas.case import SortOrder
from schemas.notification import NotificationListQuery, NotificationSortField


@dataclass(frozen=True, slots=True)
class NotificationCounts:
    """One recipient's unread state, in one value.

    Read together rather than as three queries, because the panel that shows the
    badge, the per-category counters, and the urgency indicator asks for all
    three at the same moment.
    """

    total: int = 0
    unread: int = 0
    #: Whether :attr:`unread` hit its ceiling and means "this many or more".
    unread_capped: bool = False
    unread_by_category: dict[str, int] = field(default_factory=dict)
    highest_unread_priority: NotificationPriority | None = None

    @property
    def read(self) -> int:
        """Notifications the recipient has read."""
        return max(0, self.total - self.unread)


@dataclass(frozen=True, slots=True)
class NotificationStatistics:
    """Platform-wide figures that are properties of rows rather than of a process.

    Everything a monitoring view can know exactly and permanently: how many
    notifications exist, how many are unread, how many people have any, and what
    they are about. Delivery, latency, and failure are **not** here — a delivery
    is not a row, so those accumulate in the process behind
    :class:`~services.notification_metrics.NotificationMetricsRecorder`.
    """

    total: int = 0
    unread: int = 0
    recipients: int = 0
    by_category: dict[str, int] = field(default_factory=dict)

    @property
    def read(self) -> int:
        """Stored notifications that have been read."""
        return max(0, self.total - self.unread)

    @property
    def read_rate(self) -> float:
        """Share of stored notifications that have been read, as a percentage.

        ``0.0`` when there are none — there is nothing to have been read yet.
        Same shape and reasoning as every other rate on the platform.
        """
        if self.total <= 0:
            return 0.0
        return round(self.read / self.total * 100, 2)


class NotificationRepository:
    """Queries and mutations for the ``notifications`` and preference tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get(
        self, notification_id: uuid.UUID, *, recipient_id: uuid.UUID
    ) -> Notification | None:
        """Return one of this recipient's notifications, or ``None``.

        **There is no unscoped variant of this method by design.** A notification
        belongs to exactly one person, so a lookup that could return somebody
        else's is a query no part of the platform should be able to write.
        """
        return self._session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.recipient_id == recipient_id,
                Notification.archived_at.is_(None),
            )
        ).scalar_one_or_none()

    def list_notifications(
        self, query: NotificationListQuery, *, recipient_id: uuid.UUID
    ) -> tuple[list[Notification], int]:
        """Return one page of **this recipient's** notifications and the filtered total.

        The total is counted over the *filtered and scoped* set but before
        pagination, from the same clause as the page itself, so it cannot drift
        when a filter is added later and cannot reveal how many notifications
        other people have.
        """
        filtered = self._apply_filters(select(Notification), query, recipient_id=recipient_id)

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(*self._order_by(query))
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().unique().all()), total

    def counts(self, *, recipient_id: uuid.UUID, unread_cap: int) -> NotificationCounts:
        """Return this recipient's unread state.

        Three aggregates in two statements, both scoped to one person and both
        served by the composite indexes on that column. The unread breakdown and
        the urgency come from a single grouped query rather than one per
        category, because the number of categories is open by design and a query
        per member would grow with the vocabulary.
        """
        totals = self._session.execute(
            select(
                func.count().label("total"),
                func.count().filter(Notification.read_at.is_(None)).label("unread"),
            )
            .select_from(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.archived_at.is_(None),
            )
        ).one()

        breakdown = self._session.execute(
            select(
                Notification.category,
                Notification.priority,
                # Deliberately **not** labelled ``count``: a SQLAlchemy ``Row`` is
                # tuple-like and already has a ``count`` *method*, so that label
                # would shadow it and read back as a bound method rather than a
                # number — silently, and only at runtime. The same note
                # :meth:`~repositories.report.ReportRepository.metrics` carries.
                func.count().label("unread_count"),
            )
            .where(
                Notification.recipient_id == recipient_id,
                Notification.archived_at.is_(None),
                Notification.read_at.is_(None),
            )
            .group_by(Notification.category, Notification.priority)
        ).all()

        by_category: dict[str, int] = {}
        highest: NotificationPriority | None = None
        for row in breakdown:
            category = str(row.category)
            by_category[category] = by_category.get(category, 0) + int(row.unread_count)
            priority = _as_priority(row.priority)
            if priority is not None and (
                highest is None or PRIORITY_RANK[priority] > PRIORITY_RANK[highest]
            ):
                highest = priority

        unread = int(totals.unread or 0)
        return NotificationCounts(
            total=int(totals.total or 0),
            unread=min(unread, unread_cap),
            unread_capped=unread > unread_cap,
            unread_by_category=by_category,
            highest_unread_priority=highest,
        )

    def unread_count(self, *, recipient_id: uuid.UUID, cap: int) -> tuple[int, bool]:
        """How many unread notifications one person has, counted no further than ``cap``.

        Bounded **in SQL** with a subquery ``LIMIT``, not by counting everything
        and clamping in Python: the point of the cap is that the database stops
        early. The badge is this feature's most frequent read by a wide margin,
        and an exact count over a neglected account is a scan to render "999+".

        Returns:
            ``(count, capped)``. ``capped`` says the real figure is at least
            ``count``, which is what lets a client render "999+" honestly rather
            than a number it would be wrong to state.
        """
        bounded = (
            select(Notification.id)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.archived_at.is_(None),
                Notification.read_at.is_(None),
            )
            .limit(cap + 1)
            .subquery()
        )
        counted = int(
            self._session.execute(select(func.count()).select_from(bounded)).scalar_one()
        )
        return (min(counted, cap), counted > cap)

    # ------------------------------------------------------- duplicate checks #

    def existing_event_recipients(
        self, event_id: uuid.UUID, recipient_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these people were already notified about **this dispatched event**.

        The exact half of duplicate prevention, and the query behind the unique
        constraint rather than a replacement for it: the constraint is what makes
        the guarantee true under concurrency, and this is what keeps the ordinary
        path from provoking an integrity error to discover something it could
        have asked.

        One statement for the whole batch, so a redelivered event about a
        three-person case costs one query rather than three.
        """
        if not recipient_ids:
            return set()

        rows = self._session.execute(
            select(Notification.recipient_id).where(
                Notification.event_id == event_id,
                Notification.recipient_id.in_(list(recipient_ids)),
            )
        ).scalars()
        return set(rows)

    def recent_dedupe_recipients(
        self, dedupe: str, recipient_ids: Sequence[uuid.UUID], *, since: datetime
    ) -> set[uuid.UUID]:
        """Which of these people were already told **this same thing** since ``since``.

        The heuristic half. A *window* rather than a unique constraint, because a
        case genuinely updated twice in a week is two notifications while the
        same case updated twice in ten seconds — a retried worker, a
        double-click, two writes producing one change — is one. A constraint
        cannot express "recently", and one that tried would silence the first
        case forever.
        """
        if not recipient_ids:
            return set()

        rows = self._session.execute(
            select(Notification.recipient_id).where(
                Notification.dedupe_key == dedupe,
                Notification.recipient_id.in_(list(recipient_ids)),
                Notification.created_at >= since,
            )
        ).scalars()
        return set(rows)

    # ------------------------------------------------------------- writing #

    def create_many(self, notifications: Sequence[Notification]) -> list[Notification]:
        """Persist a batch of notifications in one transaction.

        One flush and one commit for the whole fan-out, which is the spec's
        *"batch operations where appropriate"* at the place it actually pays:
        every event about a case notifies everyone party to it, and a commit per
        recipient would make the cost of an upload proportional to the size of
        the team.

        The batch is deliberately **all-or-nothing**. A partial commit would
        leave some people told and others not, with nothing to say which — and
        the caller's failure path (log it, count it, move on) is the same either
        way, so there is no benefit to trade for the ambiguity.
        """
        if not notifications:
            return []

        self._session.add_all(list(notifications))
        self._session.commit()
        for notification in notifications:
            self._session.refresh(notification)
        return list(notifications)

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when a write fails part-way: a failed flush leaves the session
        unusable until the transaction is rolled back.
        """
        self._session.rollback()

    def mark_read(
        self, notification_ids: Iterable[uuid.UUID], *, recipient_id: uuid.UUID
    ) -> int:
        """Mark specific notifications as read. Returns how many changed.

        A single conditional ``UPDATE`` rather than a read-then-write: the
        recipient clause is part of the statement, so a request naming somebody
        else's notification updates nothing rather than being refused — which is
        what lets a panel mark what it has on screen without failing because one
        row was archived underneath it.

        ``read_at IS NULL`` is in the ``WHERE`` clause, so re-reading an already
        read notification is a no-op that returns ``0`` rather than rewriting its
        timestamp. "When did I read this" must not move because a list refreshed.
        """
        identifiers = list(notification_ids)
        if not identifiers:
            return 0

        stamp = datetime.now(UTC)
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(Notification)
                .where(
                    Notification.id.in_(identifiers),
                    Notification.recipient_id == recipient_id,
                    Notification.read_at.is_(None),
                )
                .values(read_at=stamp)
            ),
        )
        self._session.commit()
        return int(outcome.rowcount or 0)

    def mark_all_read(
        self, *, recipient_id: uuid.UUID, category: str | None = None
    ) -> int:
        """Mark every unread notification as read. Returns how many changed.

        One statement whatever the history's size, which is why "mark all as
        read" is its own endpoint rather than a bulk call: loading a thousand
        identifiers to send them back would be a page of network to do what the
        database does in a clause.
        """
        stamp = datetime.now(UTC)
        statement = (
            update(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.archived_at.is_(None),
                Notification.read_at.is_(None),
            )
            .values(read_at=stamp)
        )
        if category is not None:
            statement = statement.where(Notification.category == category)

        outcome = cast("CursorResult[Any]", self._session.execute(statement))
        self._session.commit()
        return int(outcome.rowcount or 0)

    # --------------------------------------------------------- preferences #

    def preferences_for(self, user_id: uuid.UUID) -> dict[str, ChannelPreference]:
        """Every preference this user has expressed, as ``key → every channel``.

        Only the stored rows. An account that has never opened the settings page
        has none, and the caller applies
        :data:`~core.notifications.DEFAULT_PREFERENCES` — which is what makes a
        change to those defaults reach every untouched account without a
        backfill.

        Every row that exists is, by definition, a choice somebody made, which is
        why :attr:`~core.notifications.ChannelPreference.is_default` is ``False``
        on all of them. The service is what fills in the keys with no row at all.
        """
        rows = self._session.execute(
            select(
                NotificationPreference.preference_key,
                NotificationPreference.in_app,
                NotificationPreference.email,
            ).where(NotificationPreference.user_id == user_id)
        ).all()
        return {
            str(key): ChannelPreference(
                in_app=bool(in_app), email=bool(email), is_default=False
            )
            for key, in_app, email in rows
        }

    def preferences_for_many(
        self,
        user_ids: Sequence[uuid.UUID],
        *,
        preference_key: str,
        channel: NotificationChannel = NotificationChannel.IN_APP,
    ) -> dict[uuid.UUID, bool]:
        """One preference's value on one channel, for a whole batch, in one query.

        The query the subscriber runs per event — it has a rule, therefore one
        preference key, and a list of candidate recipients — and the query the
        email dispatcher runs per batch, with ``channel=EMAIL``. Asking per person
        would make the cost of an event proportional to the size of the team it
        fans out to, which is exactly what the spec's Performance section rules
        out.

        The channel selects a **column**, chosen from
        :class:`~core.notifications.NotificationChannel` rather than from a
        caller-supplied string — so this stays one method as channels are added
        instead of one method per channel, and there is still no path from a
        request to a column name.

        Absent entries mean "no opinion expressed" and are the caller's to
        default, not this method's to invent.
        """
        if not user_ids:
            return {}

        column = (
            NotificationPreference.email
            if channel is NotificationChannel.EMAIL
            else NotificationPreference.in_app
        )
        rows = self._session.execute(
            select(NotificationPreference.user_id, column).where(
                NotificationPreference.user_id.in_(list(user_ids)),
                NotificationPreference.preference_key == preference_key,
            )
        ).all()
        return {user_id: bool(enabled) for user_id, enabled in rows}

    def set_preferences(
        self, user_id: uuid.UUID, values: Mapping[str, ChannelPreferenceUpdate]
    ) -> dict[str, ChannelPreference]:
        """Store this user's answers, creating or updating one row per key.

        **Partial per channel, not only per key.** A change carrying only
        ``email`` leaves ``in_app`` exactly as it was, which is what lets a
        settings page with two switches per row save one of them without
        rewriting the other — and what stops an older client that has never heard
        of a channel from turning it off by omission.

        A key with no stored row takes the platform default for whichever channel
        the change does not mention, so the row that gets written says what the
        user would have seen rather than ``False`` for a switch they never
        touched.

        Read-then-write rather than a dialect-specific upsert, deliberately: the
        platform's test database is SQLite and its production database is
        PostgreSQL, and ``ON CONFLICT`` is spelled differently on each. The race
        this leaves open is one person saving their own settings from two tabs at
        the same moment, which the unique constraint turns into an integrity
        error rather than a duplicate — the correct outcome, and one the caller
        surfaces as a retryable failure rather than as a silently lost setting.

        Returns:
            The user's complete stored preferences after the write.
        """
        if not values:
            return self.preferences_for(user_id)

        existing = {
            row.preference_key: row
            for row in self._session.execute(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.preference_key.in_(list(values)),
                )
            )
            .scalars()
            .all()
        }

        for key, change in values.items():
            if change.is_empty:
                continue

            row = existing.get(key)
            if row is None:
                resolved = preference_from_value(key)
                fallback = (
                    default_preference(resolved, NotificationChannel.IN_APP)
                    if resolved is not None
                    else True
                )
                self._session.add(
                    NotificationPreference(
                        id=uuid.uuid4(),
                        user_id=user_id,
                        preference_key=key,
                        in_app=change.in_app if change.in_app is not None else fallback,
                        email=change.email if change.email is not None else fallback,
                    )
                )
                continue

            if change.in_app is not None and row.in_app != change.in_app:
                row.in_app = change.in_app
            if change.email is not None and row.email != change.email:
                row.email = change.email

        self._session.commit()
        return self.preferences_for(user_id)

    # -------------------------------------------------------- announcements #

    def active_recipient_ids(self) -> list[uuid.UUID]:
        """Every account that may currently sign in, as identifiers only.

        The one query in this feature that reads the ``users`` table, and it is
        here rather than on :class:`~repositories.user.UserRepository`
        deliberately. That repository's listing is the **administrative user
        directory**: it carries search, role and status filters, sort fields, and
        a page ceiling of 100, all of which exist to serve a screen. Borrowing it
        would couple the size of an announcement batch to the page size of an
        unrelated admin table, and would make this path break the day somebody
        tightens that ceiling.

        **Identifiers only, and active accounts only.** A suspended or
        deactivated user cannot read the platform, so writing them a notification
        would accumulate a feed nobody can see — and would hand them a history of
        announcements they were not party to if the account were ever reinstated.

        Unbounded on purpose: the result is one UUID per account and an
        announcement is by definition addressed to all of them. The *writes* are
        batched (``NOTIFICATION_ANNOUNCEMENT_BATCH_SIZE``), which is where the
        cost actually is.
        """
        # Imported here rather than at module scope: this is the only method that
        # touches the users table, and a module-level import would suggest the
        # repository is about users as much as about notifications.
        from models.user import User, UserStatus

        rows = self._session.execute(
            select(User.id).where(User.status == UserStatus.ACTIVE).order_by(asc(User.id))
        ).scalars()
        return list(rows)

    # ---------------------------------------------------------- monitoring #

    def statistics(self, *, since: datetime | None = None) -> NotificationStatistics:
        """Aggregate stored notifications, unread state, recipients, and categories.

        Computed in the database rather than by loading rows, for the same reason
        every list endpoint pushes its filters into SQL: the cost must not grow
        with the number of notifications the platform has ever created.

        Archived notifications are **included**, and that is not an oversight:
        the platform did the work of creating and delivering them, and a
        monitoring figure that fell when somebody tidied their feed would be
        measuring housekeeping rather than the platform.
        """
        totals = self._session.execute(
            self._windowed(
                select(
                    func.count().label("total"),
                    func.count().filter(Notification.read_at.is_(None)).label("unread"),
                    func.count(func.distinct(Notification.recipient_id)).label("recipients"),
                ).select_from(Notification),
                since=since,
            )
        ).one()

        categories = self._session.execute(
            self._windowed(
                select(Notification.category, func.count().label("count")), since=since
            ).group_by(Notification.category)
        ).all()

        return NotificationStatistics(
            total=int(totals.total or 0),
            unread=int(totals.unread or 0),
            recipients=int(totals.recipients or 0),
            by_category={str(category): int(count) for category, count in categories},
        )

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _windowed(statement: Select[Any], *, since: datetime | None) -> Select[Any]:
        """Apply the monitoring window to an aggregate."""
        if since is not None:
            return statement.where(Notification.created_at >= since)
        return statement

    @staticmethod
    def _apply_filters(
        statement: Select[tuple[Notification]],
        query: NotificationListQuery,
        *,
        recipient_id: uuid.UUID,
    ) -> Select[tuple[Notification]]:
        """Narrow a feed by its recipient and the requested filters.

        The recipient clause is applied **first and unconditionally**, and
        everything else combines with it using AND — so no combination of filters
        can widen the set beyond the caller's own feed. Archived notifications
        are excluded here rather than by each caller, so one cannot be resurrected
        by a filter nobody thought about.
        """
        statement = statement.where(
            Notification.recipient_id == recipient_id,
            Notification.archived_at.is_(None),
        )

        if query.unread_only:
            statement = statement.where(Notification.read_at.is_(None))

        if query.category is not None:
            statement = statement.where(Notification.category == query.category.value)

        if query.notification_type is not None:
            statement = statement.where(
                Notification.notification_type == query.notification_type
            )

        if query.priority is not None:
            statement = statement.where(Notification.priority == query.priority)

        if query.case_id is not None:
            statement = statement.where(Notification.case_id == query.case_id)

        return statement

    @staticmethod
    def _order_by(query: NotificationListQuery) -> tuple[UnaryExpression[object], ...]:
        """Build the ORDER BY clause for the requested sort.

        Sorting by **priority** orders on
        :data:`~core.notifications.PRIORITY_RANK` rather than on the stored
        value, for the reason ``cases.priority`` does: alphabetically, ``critical``
        sorts below ``low`` and "most urgent first" would mean nothing. The rank
        is expressed as a ``CASE`` so the ordering happens in the database rather
        than by loading the page and sorting it.

        ``created_at`` is appended as a secondary key when sorting by priority —
        a hundred notifications share four priorities, and without it the order
        within each band would be arbitrary. The primary key is appended last, for
        the reason every other repository here appends one: rows sharing a sort
        value could otherwise come back in a different order per request and be
        duplicated or skipped across page boundaries.
        """
        direction = asc if query.sort_order is SortOrder.ASC else desc

        if query.sort_by is NotificationSortField.PRIORITY:
            rank = case(
                {
                    member.value: PRIORITY_RANK[member]
                    for member in NotificationPriority
                },
                value=Notification.priority,
                else_=0,
            )
            return (direction(rank), desc(Notification.created_at), asc(Notification.id))

        return (direction(Notification.created_at), asc(Notification.id))


def _as_priority(value: Any) -> NotificationPriority | None:
    """Read a stored priority, tolerating one this version does not define."""
    if isinstance(value, NotificationPriority):
        return value
    try:
        return NotificationPriority(str(value))
    except ValueError:  # pragma: no cover - guarded by the column's enum type
        return None


__all__ = ["NotificationCounts", "NotificationRepository", "NotificationStatistics"]
