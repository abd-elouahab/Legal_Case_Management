"""Tests for the pure case-domain helpers.

These rules — the lifecycle, the priority ordering, and the case-number format —
are the ones every other layer depends on, and they are the cheapest place to
catch a mistake: no database, no request, no fixtures.
"""

from __future__ import annotations

import pytest
from sqlalchemy.sql.compiler import Compiled

from core.cases import (
    CASE_NUMBER_PREFIX,
    PRIORITY_RANK,
    STATUS_TRANSITIONS,
    InvalidCaseNumberError,
    allowed_transitions,
    build_case_number,
    can_transition,
    case_number_sequence,
    normalize_case_number,
    normalize_description,
    normalize_text,
)
from models.case import CasePriority, CaseStatus


class TestStatusTransitions:
    def test_every_status_has_a_rule(self) -> None:
        # A status with no entry would raise a KeyError the first time a case
        # reached it, which is the worst possible moment to find out.
        assert set(STATUS_TRANSITIONS) == set(CaseStatus)

    def test_no_rule_names_an_unknown_status(self) -> None:
        for current, targets in STATUS_TRANSITIONS.items():
            assert targets <= set(CaseStatus), f"{current!r} allows an unknown status"

    def test_the_rules_cannot_be_mutated_at_runtime(self) -> None:
        with pytest.raises(TypeError):
            STATUS_TRANSITIONS[CaseStatus.DRAFT] = frozenset()  # type: ignore[index]

    @pytest.mark.parametrize("status", list(CaseStatus))
    def test_staying_put_is_always_allowed(self, status: CaseStatus) -> None:
        # Re-submitting the current status is not a transition. A form that
        # round-trips every field must not fail because of it.
        assert can_transition(status, status) is True
        assert status not in allowed_transitions(status)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (CaseStatus.DRAFT, CaseStatus.OPEN),
            (CaseStatus.OPEN, CaseStatus.IN_PROGRESS),
            (CaseStatus.IN_PROGRESS, CaseStatus.WAITING_FOR_HEARING),
            (CaseStatus.WAITING_FOR_HEARING, CaseStatus.IN_PROGRESS),
            (CaseStatus.WAITING_FOR_HEARING, CaseStatus.CLOSED),
            (CaseStatus.CLOSED, CaseStatus.OPEN),
            (CaseStatus.ARCHIVED, CaseStatus.OPEN),
        ],
    )
    def test_the_lifecycle_moves_a_case_forward_and_back(
        self, current: CaseStatus, target: CaseStatus
    ) -> None:
        assert can_transition(current, target) is True

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            # A draft has not been filed, so it cannot start mid-lifecycle.
            (CaseStatus.DRAFT, CaseStatus.IN_PROGRESS),
            (CaseStatus.DRAFT, CaseStatus.CLOSED),
            # Nothing returns to draft: the case was filed, and saying otherwise
            # would falsify the record.
            (CaseStatus.OPEN, CaseStatus.DRAFT),
            (CaseStatus.CLOSED, CaseStatus.DRAFT),
            (CaseStatus.ARCHIVED, CaseStatus.DRAFT),
            # An archived case is restored to open first; anything else is then
            # an ordinary status change.
            (CaseStatus.ARCHIVED, CaseStatus.CLOSED),
            (CaseStatus.ARCHIVED, CaseStatus.IN_PROGRESS),
        ],
    )
    def test_invalid_moves_are_refused(self, current: CaseStatus, target: CaseStatus) -> None:
        assert can_transition(current, target) is False

    @pytest.mark.parametrize("current", [status for status in CaseStatus if status is not CaseStatus.ARCHIVED])
    def test_anything_can_be_archived(self, current: CaseStatus) -> None:
        # Archiving is the soft delete, and it must never be blocked by the
        # state a case happens to be in.
        assert can_transition(current, CaseStatus.ARCHIVED) is True


class TestPriorityRank:
    def test_every_priority_has_a_rank(self) -> None:
        assert set(PRIORITY_RANK) == set(CasePriority)

    def test_the_ranks_are_strictly_increasing(self) -> None:
        # Sorting by the stored value would order alphabetically — high, low,
        # medium, urgent — which is meaningless.
        ordered = [PRIORITY_RANK[priority] for priority in CasePriority]

        assert ordered == sorted(ordered)
        assert len(set(ordered)) == len(ordered)

    def test_urgent_outranks_low(self) -> None:
        assert PRIORITY_RANK[CasePriority.URGENT] > PRIORITY_RANK[CasePriority.LOW]


class TestPrioritySortSql:
    """The priority ORDER BY must be valid PostgreSQL, not just valid SQLite.

    The rest of the suite runs on SQLite, which is untyped enough to accept a
    comparison between an enum column and a plain string. PostgreSQL is not, and
    the first version of this expression — ``case({...}, value=Case.priority)``,
    whose keys bind as ``VARCHAR`` — produced *operator does not exist:
    case_priority = character varying* and a 500 on every sort-by-priority
    request. It reached a live database before anything noticed.

    Compiling against the PostgreSQL dialect here closes that gap without
    needing a running server.
    """

    @staticmethod
    def _compiled(*, literal: bool = False) -> Compiled:
        """Compile the priority sort against the PostgreSQL dialect.

        ``literal`` inlines the bound values, which makes the generated SQL
        readable; leave it off to inspect the bind parameters' *types*, which is
        where the bug actually lived.
        """
        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql

        from models.case import Case
        from repositories.case import CaseRepository
        from schemas.case import CaseListQuery, CaseSortField

        statement = select(Case.id).order_by(
            *CaseRepository._order_by(CaseListQuery(sort_by=CaseSortField.PRIORITY))
        )
        return statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True} if literal else {},
        )

    def test_no_priority_value_is_bound_as_a_plain_string(self) -> None:
        # The exact discriminator. The broken shorthand bound its keys as
        # `String`, which psycopg renders as `$n::VARCHAR` — and PostgreSQL has
        # no `case_priority = character varying` operator. Comparing against the
        # column instead binds each value with the column's own `Enum` type.
        bound = {type(bind.type).__name__ for bind in self._compiled().binds.values()}

        assert "String" not in bound
        assert "Enum" in bound

    def test_it_is_a_searched_case_over_the_column(self) -> None:
        # `CASE WHEN cases.priority = ...`, not `CASE cases.priority WHEN ...`.
        # The second form is the one that loses the column's type.
        sql = str(self._compiled())

        assert "CASE WHEN (cases.priority = " in sql
        assert "CASE cases.priority WHEN" not in sql

    def test_every_rank_reaches_the_order_by(self) -> None:
        sql = str(self._compiled(literal=True))

        assert "ORDER BY CASE" in sql
        for priority, rank in PRIORITY_RANK.items():
            assert f"(cases.priority = '{priority.value}') THEN {rank}" in sql


class TestNormalization:
    def test_a_title_is_trimmed_and_its_whitespace_collapsed(self) -> None:
        # Otherwise "Benali  v.  State " and "Benali v. State" are two different
        # titles, and search results depend on how someone typed them.
        assert normalize_text("  Benali   v.  State ") == "Benali v. State"

    def test_a_description_keeps_its_paragraphs(self) -> None:
        # Unlike a title: flattening prose would destroy the author's structure.
        assert normalize_description("  First line.\n\nSecond line.  ") == (
            "First line.\n\nSecond line."
        )


class TestCaseNumbers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("case-2026-0001", "CASE-2026-0001"),
            ("  TC/2026/44  ", "TC/2026/44"),
            # Internal spaces are removed, not collapsed: an identifier is quoted
            # verbatim, and "TC 2026" is the same reference as "TC2026".
            ("TC 2026 44", "TC202644"),
        ],
    )
    def test_a_case_number_is_normalized_and_uppercased(self, raw: str, expected: str) -> None:
        assert normalize_case_number(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "TC 2026!", "<script>", "-leading", "a" * 51])
    def test_an_unusable_case_number_is_rejected(self, raw: str) -> None:
        with pytest.raises(InvalidCaseNumberError):
            normalize_case_number(raw)

    def test_a_generated_number_is_zero_padded(self) -> None:
        # Padding is what makes lexicographic sorting match issue order, so
        # "sort by case number" needs no second numeric column.
        assert build_case_number(2026, 7) == f"{CASE_NUMBER_PREFIX}-2026-0007"
        assert build_case_number(2026, 1) < build_case_number(2026, 12)

    def test_a_large_sequence_widens_rather_than_wraps(self) -> None:
        assert build_case_number(2026, 12345) == f"{CASE_NUMBER_PREFIX}-2026-12345"

    def test_the_sequence_round_trips(self) -> None:
        assert case_number_sequence(build_case_number(2026, 42), year=2026) == 42

    @pytest.mark.parametrize(
        "case_number",
        [
            # Another year's series must not influence this year's.
            "CASE-2025-0009",
            # A registry's own numbering must not reset the platform's.
            "TC/2026/44",
            "CASE-2026-ABC",
            "PREFIX-CASE-2026-0001",
        ],
    )
    def test_a_foreign_number_yields_no_sequence(self, case_number: str) -> None:
        assert case_number_sequence(case_number, year=2026) is None
