"""Case data access.

Single responsibility: reading and persisting :class:`~models.case.Case` rows.
No authorization or lifecycle rules live here — those belong to
``services/case.py`` and ``services/case_access.py``. In particular this layer
never decides *whether* a change is allowed, only how it is written.

Search, filtering, sorting, and pagination all execute **in the database**, so
the cost of a page does not grow with the size of the caseload. The one
authorization concern that must live here is the assignment scope: restricting
rows in Python would mean fetching every case in order to hide most of them, and
would make the page counts wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Case as SqlCase
from sqlalchemy import Select, asc, case, desc, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement, UnaryExpression

from core.cases import (
    CASE_NUMBER_PREFIX,
    PRIORITY_RANK,
    case_number_sequence,
    normalize_case_number,
)
from models.case import Case
from schemas.case import CaseListQuery, CaseSortField, SortOrder


def assigned_case_scope(user_id: uuid.UUID) -> ColumnElement[bool]:
    """SQL predicate matching the cases ``user_id`` is party to.

    The per-resource visibility rule, expressed once. Document Management applies
    the *same* restriction to documents (a document is reachable exactly when its
    case is), so it imports this rather than re-writing the OR — two copies would
    be one policy change away from disagreeing about who can see what.
    """
    return or_(
        Case.assigned_lawyer_id == user_id,
        Case.assigned_court_representative_id == user_id,
    )


class CaseRepository:
    """Queries and mutations for the ``cases`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get_by_id(self, case_id: uuid.UUID) -> Case | None:
        """Return the case with this id, or ``None``."""
        return self._session.get(Case, case_id)

    def get_by_number(self, case_number: str) -> Case | None:
        """Return the case with this case number, or ``None``.

        Case numbers are stored uppercase, so the lookup is normalized to match
        and is therefore case-insensitive.
        """
        statement = select(Case).where(Case.case_number == normalize_case_number(case_number))
        return self._session.execute(statement).scalar_one_or_none()

    def case_number_taken(self, case_number: str, *, excluding: uuid.UUID | None = None) -> bool:
        """Whether another case already uses this case number."""
        statement = select(Case.id).where(Case.case_number == normalize_case_number(case_number))
        if excluding is not None:
            statement = statement.where(Case.id != excluding)
        return self._session.execute(statement.limit(1)).scalar_one_or_none() is not None

    def list_cases(
        self,
        query: CaseListQuery,
        *,
        visible_to: uuid.UUID | None = None,
    ) -> tuple[list[Case], int]:
        """Return one page of cases and the total number matching the filters.

        Args:
            query: search term, filters, sort, and page.
            visible_to: when given, restrict the result to cases this user is
                assigned to. ``None`` means no restriction — reserved for callers
                holding ``cases:view-all``, which the service decides.

        The total is counted over the *filtered and scoped* set but before
        pagination, so a client can render "showing 20 of 137" and know how many
        pages exist. Both queries are built from the same clause, which is what
        keeps the count honest when a filter is added later — and, more
        importantly here, what stops the count from revealing how many cases a
        lawyer is *not* allowed to see.
        """
        filtered = self._apply_filters(select(Case), query, visible_to=visible_to)

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(*self._order_by(query)).offset(query.offset).limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().all()), total

    def highest_generated_sequence(self, year: int) -> int:
        """The largest sequence already issued in ``year``'s generated series.

        Returns ``0`` when the year has no generated numbers yet, so the caller
        can simply add one.

        The prefix match is done in SQL and the sequence parsed in Python: the
        set is at most one year of cases, and doing the arithmetic in the
        database would hard-code the number format into a SQL expression, where
        it could drift from :mod:`core.cases`.
        """
        prefix = f"{CASE_NUMBER_PREFIX}-{year:04d}-"
        numbers = (
            self._session.execute(
                select(Case.case_number).where(Case.case_number.startswith(prefix))
            )
            .scalars()
            .all()
        )

        sequences = [
            sequence
            for number in numbers
            if (sequence := case_number_sequence(number, year=year)) is not None
        ]
        return max(sequences, default=0)

    # ------------------------------------------------------------- writing #

    def create(self, legal_case: Case) -> Case:
        """Persist a new case and return it with its generated columns populated."""
        self._session.add(legal_case)
        self._session.commit()
        self._session.refresh(legal_case)
        return legal_case

    def save(self, legal_case: Case) -> Case:
        """Persist pending changes to an existing case."""
        self._session.commit()
        self._session.refresh(legal_case)
        return legal_case

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when a unique-constraint violation has to be retried: a failed
        flush leaves the session unusable until the transaction is rolled back.
        """
        self._session.rollback()

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _apply_filters(
        statement: Select[tuple[Case]],
        query: CaseListQuery,
        *,
        visible_to: uuid.UUID | None,
    ) -> Select[tuple[Case]]:
        """Narrow a case query by the assignment scope, search term, and filters.

        Everything combines with AND, so the filters are freely combinable
        exactly as the spec requires — and the scope, being one more AND, cannot
        be widened by any combination of them.
        """
        if visible_to is not None:
            statement = statement.where(assigned_case_scope(visible_to))

        if query.search:
            # ILIKE with escaped wildcards: a term containing % or _ must match
            # those characters literally rather than turning into a pattern.
            pattern = f"%{_escape_like(query.search)}%"
            statement = statement.where(
                or_(
                    Case.case_number.ilike(pattern, escape="\\"),
                    Case.title.ilike(pattern, escape="\\"),
                    Case.description.ilike(pattern, escape="\\"),
                    Case.court_name.ilike(pattern, escape="\\"),
                )
            )

        if query.status is not None:
            statement = statement.where(Case.status == query.status)

        if query.priority is not None:
            statement = statement.where(Case.priority == query.priority)

        if query.assigned_lawyer_id is not None:
            statement = statement.where(Case.assigned_lawyer_id == query.assigned_lawyer_id)

        if query.assigned_court_representative_id is not None:
            statement = statement.where(
                Case.assigned_court_representative_id == query.assigned_court_representative_id
            )

        if query.court_name:
            # A substring match rather than equality: courts are recorded as free
            # text, so "Tribunal de Première Instance de Casablanca" must be
            # findable by "Casablanca".
            statement = statement.where(
                Case.court_name.ilike(f"%{_escape_like(query.court_name)}%", escape="\\")
            )

        if query.filing_date_from is not None:
            statement = statement.where(Case.filing_date >= query.filing_date_from)
        if query.filing_date_to is not None:
            statement = statement.where(Case.filing_date <= query.filing_date_to)

        if query.hearing_date_from is not None:
            statement = statement.where(Case.next_hearing_date >= query.hearing_date_from)
        if query.hearing_date_to is not None:
            statement = statement.where(Case.next_hearing_date <= query.hearing_date_to)

        return statement

    @classmethod
    def _order_by(cls, query: CaseListQuery) -> tuple[UnaryExpression[object], ...]:
        """Build the ORDER BY clause for the requested sort.

        The primary key is appended as a tiebreaker: without it, rows that share
        a sort value — every case at the same priority, every case with no
        hearing scheduled — could come back in a different order on each request
        and be duplicated or skipped across page boundaries. Invisible until the
        caseload outgrows one page.
        """
        direction = asc if query.sort_order is SortOrder.ASC else desc

        # `Any` because the mapping mixes plain columns with the priority CASE
        # expression; SQLAlchemy's own union of the two is not assignable to a
        # single annotation, and nothing here depends on the element type.
        columns: Sequence[Any] = {
            CaseSortField.CASE_NUMBER: (Case.case_number,),
            CaseSortField.CREATED_AT: (Case.created_at,),
            CaseSortField.UPDATED_AT: (Case.updated_at,),
            CaseSortField.FILING_DATE: (Case.filing_date,),
            CaseSortField.NEXT_HEARING_DATE: (Case.next_hearing_date,),
            CaseSortField.PRIORITY: (cls._priority_rank(),),
        }[query.sort_by]

        return (*(direction(column) for column in columns), asc(Case.id))

    @staticmethod
    def _priority_rank() -> SqlCase[int]:
        """Map each priority onto its sort weight.

        Ordering by the stored value would sort alphabetically — high, low,
        medium, urgent — which is meaningless. The weights come from
        :data:`~core.cases.PRIORITY_RANK`, so the SQL and the rest of the
        platform cannot disagree about which priority outranks which.

        Built as a *searched* CASE (``WHEN priority = :value``) rather than the
        shorthand ``case({...}, value=Case.priority)``. The shorthand binds its
        keys as plain strings, and PostgreSQL refuses to compare its
        ``case_priority`` enum to ``character varying`` — a 500 on every
        sort-by-priority request. Comparing against the column makes SQLAlchemy
        bind each value with the column's own type, so it is cast correctly.
        SQLite is untyped enough not to care, which is exactly why this was
        invisible to the test suite and only appeared against a real database.
        """
        return case(
            *((Case.priority == priority, rank) for priority, rank in PRIORITY_RANK.items()),
            else_=0,
        )


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
