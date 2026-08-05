"""Timeline data access.

Single responsibility: reading and appending
:class:`~models.timeline.TimelineEvent` rows. No authorization rules and no
decisions about *what* is worth recording — those belong to
``services/timeline_access.py`` and to the business services that publish.

Search, filtering, sorting, and pagination all execute **in the database**, so
the cost of a page does not grow with the length of a case's history. The one
authorization concern that has to live here is the case scope: restricting rows
in Python would mean fetching every event in order to hide most of them, and
would make the page totals count events the caller is not entitled to know exist.

**There is no update and no delete.** The timeline is append-only, and the
absence of those methods is what enforces it — a repository that cannot express
"change this event" cannot be talked into it by a future caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

from sqlalchemy import Select, asc, desc, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from models.case import Case
from models.timeline import TimelineEvent
from repositories.case import assigned_case_scope
from schemas.case import SortOrder
from schemas.timeline import TimelineListQuery


class TimelineRepository:
    """Queries and appends for the ``timeline_events`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get_by_id(self, event_id: uuid.UUID) -> TimelineEvent | None:
        """Return the event with this id, or ``None``."""
        return self._session.get(TimelineEvent, event_id)

    def list_events(
        self,
        query: TimelineListQuery,
        *,
        case_id: uuid.UUID | None = None,
        visible_to: uuid.UUID | None = None,
    ) -> tuple[list[TimelineEvent], int]:
        """Return one page of events and the total number matching the filters.

        Args:
            query: search term, filters, sort, and page.
            case_id: when given, restrict the result to one case's timeline.
            visible_to: when given, restrict the result to events on cases this
                user is assigned to. ``None`` means no restriction — reserved for
                callers holding ``cases:view-all``, which the service decides.

        The total is counted over the *filtered and scoped* set but before
        pagination, from the same clause as the page itself, so it cannot drift
        when a filter is added later — and, more importantly, cannot reveal how
        many events a lawyer is not allowed to see.
        """
        filtered = self._apply_filters(
            select(TimelineEvent), query, case_id=case_id, visible_to=visible_to
        )

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(*self._order_by(query)).offset(query.offset).limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().unique().all()), total

    # ------------------------------------------------------------- writing #

    def add(self, event: TimelineEvent) -> TimelineEvent:
        """Append one event and return it with its generated columns populated.

        The only mutation this repository offers. Committed immediately rather
        than left pending: the publishing service has already committed the
        business change by this point, so leaving the event uncommitted would
        make the record depend on whether *some later* statement in the request
        happened to succeed.
        """
        self._session.add(event)
        self._session.commit()
        self._session.refresh(event)
        return event

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when the append fails: a failed flush leaves the session unusable
        until the transaction is rolled back, and the request that triggered the
        event still has to return its own result.
        """
        self._session.rollback()

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _apply_filters(
        statement: Select[tuple[TimelineEvent]],
        query: TimelineListQuery,
        *,
        case_id: uuid.UUID | None,
        visible_to: uuid.UUID | None,
    ) -> Select[tuple[TimelineEvent]]:
        """Narrow a timeline query by the case, the caller's scope, and the filters.

        Everything combines with AND, so the filters are freely combinable exactly
        as the spec requires — and the scope, being one more AND, cannot be
        widened by any combination of them.
        """
        if case_id is not None:
            statement = statement.where(TimelineEvent.case_id == case_id)

        if visible_to is not None:
            # An event is reachable exactly when its case is, so the scope is the
            # case scope, imported rather than restated. A subquery rather than a
            # join keeps the count query identical in shape to the page query and
            # cannot multiply rows.
            statement = statement.where(
                TimelineEvent.case_id.in_(select(Case.id).where(assigned_case_scope(visible_to)))
            )

        if query.search:
            # ILIKE with escaped wildcards: a term containing % or _ must match
            # those characters literally rather than turning into a pattern.
            pattern = f"%{_escape_like(query.search)}%"
            statement = statement.where(
                or_(
                    TimelineEvent.title.ilike(pattern, escape="\\"),
                    TimelineEvent.description.ilike(pattern, escape="\\"),
                )
            )

        if query.event_type is not None:
            statement = statement.where(TimelineEvent.event_type == query.event_type)

        if query.actor_id is not None:
            statement = statement.where(TimelineEvent.actor_id == query.actor_id)

        # The bounds are calendar dates but the column is a timestamp, so the
        # upper bound has to cover the whole day — `<= 2026-07-31` must include an
        # event recorded at 14:32 that day, which `<= 2026-07-31 00:00` would
        # silently exclude.
        if query.date_from is not None:
            statement = statement.where(
                TimelineEvent.created_at >= datetime.combine(query.date_from, time.min)
            )
        if query.date_to is not None:
            statement = statement.where(
                TimelineEvent.created_at <= datetime.combine(query.date_to, time.max)
            )

        return statement

    @staticmethod
    def _order_by(query: TimelineListQuery) -> tuple[UnaryExpression[object], ...]:
        """Build the ORDER BY clause for the requested sort.

        The primary key is appended as a tiebreaker, in the *same* direction as
        the sort. Two events sharing a timestamp could otherwise come back in a
        different order on each request — duplicated or skipped across a page
        boundary, which on a timeline reads as history changing.

        Note that the tiebreaker only guarantees *stability*, not insertion order:
        the key is a random UUID. Reading events in the order they happened is
        :attr:`~models.timeline.TimelineEvent.created_at`'s job, which is why the
        service stamps it in Python at microsecond precision rather than letting
        the database default it.
        """
        direction = asc if query.sort_order is SortOrder.ASC else desc
        return (direction(TimelineEvent.created_at), direction(TimelineEvent.id))


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
