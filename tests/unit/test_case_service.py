"""Unit tests for :class:`~services.case.CaseService`.

Cover the business rules the service owns — case-number generation and
uniqueness, legal status transitions, assignment validation, the date rule that
needs the stored case, soft-delete archiving, and the audit trail — against a
real (SQLite in-memory) repository, so the query layer is exercised alongside
them.

Per-resource authorization is covered separately in
``tests/unit/test_case_access.py``; what these assert is that the service *asks*.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from core.cases import build_case_number
from core.exceptions import (
    CaseAccessDeniedError,
    CaseNotFoundError,
    DuplicateCaseNumberError,
    InvalidAssignmentError,
    InvalidCaseDatesError,
    InvalidCaseTransitionError,
)
from models.case import Case, CasePriority, CaseStatus
from models.user import User, UserRole, UserStatus
from repositories.case import CaseRepository
from repositories.user import UserRepository
from schemas.case import CaseCreate, CaseListQuery, CaseSortField, CaseUpdate, SortOrder
from services.case import CaseService

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]

PASSWORD = "correct-horse-battery"


@pytest.fixture
def cases(db_session: Session) -> CaseService:
    return CaseService(CaseRepository(db_session), UserRepository(db_session))


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(
        email="lawyer@example.com",
        password=PASSWORD,
        first_name="Karim",
        last_name="Zahra",
        role=UserRole.LAWYER,
    )


@pytest.fixture
def representative(make_user: MakeUser) -> User:
    return make_user(
        email="court@example.com",
        password=PASSWORD,
        first_name="Nadia",
        last_name="Alami",
        role=UserRole.COURT_REPRESENTATIVE,
    )


def creation(**overrides: object) -> CaseCreate:
    return CaseCreate.model_validate({"title": "Benali v. Societe Atlas", **overrides})


def update(**fields: object) -> CaseUpdate:
    return CaseUpdate.model_validate(fields)


class TestCreateCase:
    def test_generates_a_case_number_when_none_is_supplied(
        self, cases: CaseService, admin: User
    ) -> None:
        created = cases.create_case(creation(), actor=admin)

        assert created.case_number == build_case_number(datetime.now(UTC).year, 1)

    def test_the_generated_series_increments(self, cases: CaseService, admin: User) -> None:
        first = cases.create_case(creation(), actor=admin)
        second = cases.create_case(creation(title="Second matter"), actor=admin)

        assert first.case_number != second.case_number
        assert second.case_number == build_case_number(datetime.now(UTC).year, 2)

    def test_a_registry_number_does_not_disturb_the_series(
        self, cases: CaseService, admin: User
    ) -> None:
        # One court's numbering scheme must not reset or advance the platform's.
        cases.create_case(creation(case_number="TC/2026/9999"), actor=admin)
        generated = cases.create_case(creation(title="Second"), actor=admin)

        assert generated.case_number == build_case_number(datetime.now(UTC).year, 1)

    def test_a_supplied_case_number_is_used_verbatim(
        self, cases: CaseService, admin: User
    ) -> None:
        created = cases.create_case(creation(case_number="tc/2026/44"), actor=admin)

        assert created.case_number == "TC/2026/44"

    def test_a_duplicate_case_number_is_refused(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        make_case(case_number="TC/2026/44")

        with pytest.raises(DuplicateCaseNumberError):
            cases.create_case(creation(case_number="TC/2026/44"), actor=admin)

    def test_a_duplicate_is_detected_regardless_of_casing(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        make_case(case_number="TC/2026/44")

        with pytest.raises(DuplicateCaseNumberError):
            cases.create_case(creation(case_number="tc/2026/44"), actor=admin)

    def test_audit_fields_come_from_the_caller(self, cases: CaseService, admin: User) -> None:
        created = cases.create_case(creation(), actor=admin)

        assert created.created_by == admin.id
        assert created.updated_by == admin.id

    def test_a_new_case_defaults_to_draft_at_medium_priority(
        self, cases: CaseService, admin: User
    ) -> None:
        created = cases.create_case(creation(), actor=admin)

        assert created.status is CaseStatus.DRAFT
        assert created.priority is CasePriority.MEDIUM

    def test_assignments_are_stored(
        self, cases: CaseService, admin: User, lawyer: User, representative: User
    ) -> None:
        created = cases.create_case(
            creation(
                assigned_lawyer_id=str(lawyer.id),
                assigned_court_representative_id=str(representative.id),
            ),
            actor=admin,
        )

        assert created.assigned_lawyer_id == lawyer.id
        assert created.assigned_court_representative_id == representative.id

    def test_an_unknown_assignee_is_refused(self, cases: CaseService, admin: User) -> None:
        with pytest.raises(InvalidAssignmentError):
            cases.create_case(creation(assigned_lawyer_id=str(uuid.uuid4())), actor=admin)

    def test_the_lawyer_position_refuses_a_court_representative(
        self, cases: CaseService, admin: User, representative: User
    ) -> None:
        # Otherwise a court representative would hold the lawyer's access to the
        # case without the role that is supposed to carry it.
        with pytest.raises(InvalidAssignmentError):
            cases.create_case(creation(assigned_lawyer_id=str(representative.id)), actor=admin)

    def test_the_representative_position_refuses_a_lawyer(
        self, cases: CaseService, admin: User, lawyer: User
    ) -> None:
        with pytest.raises(InvalidAssignmentError):
            cases.create_case(
                creation(assigned_court_representative_id=str(lawyer.id)), actor=admin
            )

    def test_a_disabled_account_cannot_be_assigned(
        self, cases: CaseService, admin: User, make_user: MakeUser
    ) -> None:
        disabled = make_user(
            email="former.lawyer@example.com",
            password=PASSWORD,
            role=UserRole.LAWYER,
            status=UserStatus.INACTIVE,
        )

        with pytest.raises(InvalidAssignmentError):
            cases.create_case(creation(assigned_lawyer_id=str(disabled.id)), actor=admin)

    def test_a_rejected_assignment_names_the_field(self, cases: CaseService, admin: User) -> None:
        with pytest.raises(InvalidAssignmentError) as excinfo:
            cases.create_case(creation(assigned_lawyer_id=str(uuid.uuid4())), actor=admin)

        assert [detail.field for detail in excinfo.value.details] == ["assigned_lawyer_id"]


class TestGetCase:
    def test_returns_the_case(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case()

        assert cases.get_case(stored.id, actor=admin).id == stored.id

    def test_an_unknown_id_is_a_not_found(self, cases: CaseService, admin: User) -> None:
        with pytest.raises(CaseNotFoundError):
            cases.get_case(uuid.uuid4(), actor=admin)

    def test_an_unassigned_lawyer_is_refused(
        self, cases: CaseService, lawyer: User, make_case: MakeCase
    ) -> None:
        stored = make_case()

        with pytest.raises(CaseAccessDeniedError):
            cases.get_case(stored.id, actor=lawyer)

    def test_an_assigned_lawyer_may_read_it(
        self, cases: CaseService, lawyer: User, make_case: MakeCase
    ) -> None:
        stored = make_case(assigned_lawyer_id=lawyer.id)

        assert cases.get_case(stored.id, actor=lawyer).id == stored.id


class TestListCases:
    def test_an_administrator_sees_every_case(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        make_case()
        make_case()

        result = cases.list_cases(CaseListQuery(), actor=admin)

        assert result.total == 2

    def test_a_lawyer_sees_only_their_own(
        self, cases: CaseService, lawyer: User, make_case: MakeCase
    ) -> None:
        mine = make_case(assigned_lawyer_id=lawyer.id)
        make_case()

        result = cases.list_cases(CaseListQuery(), actor=lawyer)

        assert [legal_case.id for legal_case in result.cases] == [mine.id]

    def test_the_total_counts_only_what_the_caller_may_see(
        self, cases: CaseService, lawyer: User, make_case: MakeCase
    ) -> None:
        # Otherwise the pagination footer would tell a lawyer how many cases
        # exist that they are not entitled to know about.
        make_case(assigned_lawyer_id=lawyer.id)
        make_case()
        make_case()

        assert cases.list_cases(CaseListQuery(), actor=lawyer).total == 1

    def test_a_representative_sees_the_cases_they_cover(
        self, cases: CaseService, representative: User, make_case: MakeCase
    ) -> None:
        mine = make_case(assigned_court_representative_id=representative.id)
        make_case()

        result = cases.list_cases(CaseListQuery(), actor=representative)

        assert [legal_case.id for legal_case in result.cases] == [mine.id]

    def test_a_filter_cannot_widen_the_scope(
        self, cases: CaseService, lawyer: User, make_case: MakeCase
    ) -> None:
        # The scope is one more AND, so no combination of filters escapes it.
        other = make_case(status=CaseStatus.OPEN)

        result = cases.list_cases(
            CaseListQuery(status=CaseStatus.OPEN, assigned_lawyer_id=None), actor=lawyer
        )

        assert other.id not in {legal_case.id for legal_case in result.cases}
        assert result.total == 0

    @pytest.mark.parametrize("term", ["atlas", "ATLAS", "AtLaS"])
    def test_search_is_case_insensitive(
        self, cases: CaseService, admin: User, make_case: MakeCase, term: str
    ) -> None:
        make_case(title="Benali v. Atlas")
        make_case(title="Unrelated")

        result = cases.list_cases(CaseListQuery(search=term), actor=admin)

        assert result.total == 1

    @pytest.mark.parametrize(
        ("field", "value", "term"),
        [
            ("case_number", "TC/2026/44", "2026/44"),
            ("title", "Benali v. Atlas", "benali"),
            ("description", "Breach of a supply contract", "supply"),
            ("court_name", "Tribunal de Commerce de Casablanca", "casablanca"),
        ],
    )
    def test_search_covers_the_four_specified_fields(
        self,
        cases: CaseService,
        admin: User,
        make_case: MakeCase,
        field: str,
        value: str,
        term: str,
    ) -> None:
        make_case(**{field: value})

        assert cases.list_cases(CaseListQuery(search=term), actor=admin).total == 1

    def test_a_wildcard_in_a_search_term_is_literal(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        # An unescaped `%` matches every case, which reads as a broken filter
        # rather than as the injection-shaped bug it is.
        make_case(title="Benali v. Atlas")

        assert cases.list_cases(CaseListQuery(search="%"), actor=admin).total == 0

    def test_filters_combine(
        self, cases: CaseService, admin: User, lawyer: User, make_case: MakeCase
    ) -> None:
        wanted = make_case(
            status=CaseStatus.OPEN, priority=CasePriority.URGENT, assigned_lawyer_id=lawyer.id
        )
        make_case(status=CaseStatus.OPEN, priority=CasePriority.LOW, assigned_lawyer_id=lawyer.id)
        make_case(status=CaseStatus.CLOSED, priority=CasePriority.URGENT)

        result = cases.list_cases(
            CaseListQuery(
                status=CaseStatus.OPEN,
                priority=CasePriority.URGENT,
                assigned_lawyer_id=lawyer.id,
            ),
            actor=admin,
        )

        assert [legal_case.id for legal_case in result.cases] == [wanted.id]

    def test_a_court_filter_matches_a_substring(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        # Courts are free text, so a full name must be findable by its city.
        make_case(court_name="Tribunal de Commerce de Casablanca")
        make_case(court_name="Tribunal de Premiere Instance de Rabat")

        result = cases.list_cases(CaseListQuery(court_name="casablanca"), actor=admin)

        assert result.total == 1

    def test_a_date_range_filter_is_inclusive(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        inside = make_case(filing_date=date(2026, 5, 1))
        make_case(filing_date=date(2026, 4, 30))

        result = cases.list_cases(
            CaseListQuery(filing_date_from=date(2026, 5, 1), filing_date_to=date(2026, 5, 31)),
            actor=admin,
        )

        assert [legal_case.id for legal_case in result.cases] == [inside.id]

    def test_a_hearing_range_filter_excludes_unscheduled_cases(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        scheduled = make_case(next_hearing_date=date(2026, 6, 10))
        make_case(next_hearing_date=None)

        result = cases.list_cases(
            CaseListQuery(hearing_date_from=date(2026, 6, 1)), actor=admin
        )

        assert [legal_case.id for legal_case in result.cases] == [scheduled.id]

    def test_archived_cases_remain_listed_and_searchable(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        # The spec is explicit: archiving takes a case out of the working set, not
        # out of the record.
        make_case(title="Shelved matter", status=CaseStatus.ARCHIVED)

        assert cases.list_cases(CaseListQuery(), actor=admin).total == 1
        assert cases.list_cases(CaseListQuery(search="shelved"), actor=admin).total == 1

    def test_priority_sorts_by_urgency_not_alphabetically(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        # Alphabetically this would be high, low, medium, urgent — meaningless.
        for priority in (CasePriority.MEDIUM, CasePriority.URGENT, CasePriority.LOW):
            make_case(priority=priority)

        result = cases.list_cases(
            CaseListQuery(sort_by=CaseSortField.PRIORITY, sort_order=SortOrder.DESC),
            actor=admin,
        )

        assert [legal_case.priority for legal_case in result.cases] == [
            CasePriority.URGENT,
            CasePriority.MEDIUM,
            CasePriority.LOW,
        ]

    def test_case_numbers_sort_in_issue_order(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        # Zero padding is what makes the lexicographic sort a chronological one.
        for sequence in (12, 2, 1):
            make_case(case_number=build_case_number(2026, sequence))

        result = cases.list_cases(
            CaseListQuery(sort_by=CaseSortField.CASE_NUMBER, sort_order=SortOrder.ASC),
            actor=admin,
        )

        assert [legal_case.case_number for legal_case in result.cases] == [
            "CASE-2026-0001",
            "CASE-2026-0002",
            "CASE-2026-0012",
        ]

    def test_pagination_splits_the_result(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        for index in range(5):
            make_case(created_at=datetime(2026, 5, index + 1, tzinfo=UTC))

        first = cases.list_cases(CaseListQuery(page=1, page_size=2), actor=admin)
        second = cases.list_cases(CaseListQuery(page=2, page_size=2), actor=admin)

        assert (first.total, len(first.cases), len(second.cases)) == (5, 2, 2)
        # Disjoint pages: without the primary-key tiebreaker, rows sharing a sort
        # value could appear on both.
        assert not {c.id for c in first.cases} & {c.id for c in second.cases}

    def test_a_page_beyond_the_end_is_empty_but_still_counts(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        make_case()

        result = cases.list_cases(CaseListQuery(page=9, page_size=20), actor=admin)

        assert (result.cases, result.total) == ([], 1)


class TestUpdateCase:
    def test_only_the_supplied_fields_change(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case(title="Original", court_name="Tribunal de Rabat")

        updated = cases.update_case(stored.id, update(title="Renamed"), actor=admin)

        assert updated.title == "Renamed"
        assert updated.court_name == "Tribunal de Rabat"

    def test_an_explicit_null_clears_a_field(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case(court_name="Tribunal de Rabat")

        updated = cases.update_case(stored.id, update(court_name=None), actor=admin)

        assert updated.court_name is None

    def test_the_updater_is_recorded(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case()

        updated = cases.update_case(stored.id, update(priority="high"), actor=admin)

        assert updated.updated_by == admin.id

    def test_an_unknown_case_is_a_not_found(self, cases: CaseService, admin: User) -> None:
        with pytest.raises(CaseNotFoundError):
            cases.update_case(uuid.uuid4(), update(priority="high"), actor=admin)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (CaseStatus.DRAFT, CaseStatus.OPEN),
            (CaseStatus.OPEN, CaseStatus.IN_PROGRESS),
            (CaseStatus.IN_PROGRESS, CaseStatus.WAITING_FOR_HEARING),
            (CaseStatus.CLOSED, CaseStatus.OPEN),
            (CaseStatus.ARCHIVED, CaseStatus.OPEN),
        ],
    )
    def test_a_legal_transition_is_applied(
        self,
        cases: CaseService,
        admin: User,
        make_case: MakeCase,
        current: CaseStatus,
        target: CaseStatus,
    ) -> None:
        stored = make_case(status=current)

        updated = cases.update_case(stored.id, update(status=target.value), actor=admin)

        assert updated.status is target

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (CaseStatus.DRAFT, CaseStatus.CLOSED),
            (CaseStatus.OPEN, CaseStatus.DRAFT),
            (CaseStatus.ARCHIVED, CaseStatus.CLOSED),
        ],
    )
    def test_an_illegal_transition_is_refused(
        self,
        cases: CaseService,
        admin: User,
        make_case: MakeCase,
        current: CaseStatus,
        target: CaseStatus,
    ) -> None:
        stored = make_case(status=current)

        with pytest.raises(InvalidCaseTransitionError) as excinfo:
            cases.update_case(stored.id, update(status=target.value), actor=admin)

        # Both statuses are named: the caller chose them and can see them.
        assert current.value in excinfo.value.message
        assert target.value in excinfo.value.message

    def test_resubmitting_the_current_status_is_not_a_transition(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        # So a form that round-trips every field still saves an unrelated change.
        stored = make_case(status=CaseStatus.CLOSED)

        updated = cases.update_case(
            stored.id, update(status="closed", priority="high"), actor=admin
        )

        assert (updated.status, updated.priority) == (CaseStatus.CLOSED, CasePriority.HIGH)

    def test_a_hearing_moved_before_the_stored_filing_date_is_refused(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        # The schema cannot see this: only one of the two dates is in the request.
        stored = make_case(filing_date=date(2026, 5, 10))

        with pytest.raises(InvalidCaseDatesError):
            cases.update_case(stored.id, update(next_hearing_date="2026-05-09"), actor=admin)

    def test_a_filing_date_moved_after_the_stored_hearing_is_refused(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case(next_hearing_date=date(2026, 5, 10))

        with pytest.raises(InvalidCaseDatesError):
            cases.update_case(stored.id, update(filing_date="2026-05-11"), actor=admin)

    def test_clearing_the_filing_date_lifts_the_constraint(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case(filing_date=date(2026, 5, 10), next_hearing_date=date(2026, 6, 1))

        updated = cases.update_case(
            stored.id, update(filing_date=None, next_hearing_date="2020-01-01"), actor=admin
        )

        assert updated.filing_date is None

    def test_an_unassigned_lawyer_cannot_update_the_case(
        self, cases: CaseService, lawyer: User, make_case: MakeCase
    ) -> None:
        stored = make_case()

        with pytest.raises(CaseAccessDeniedError):
            cases.update_case(stored.id, update(priority="high"), actor=lawyer)

    def test_an_assigned_lawyer_may_update_their_case(
        self, cases: CaseService, lawyer: User, make_case: MakeCase
    ) -> None:
        stored = make_case(assigned_lawyer_id=lawyer.id)

        updated = cases.update_case(stored.id, update(priority="high"), actor=lawyer)

        assert updated.priority is CasePriority.HIGH

    def test_a_representative_may_record_the_hearing(
        self, cases: CaseService, representative: User, make_case: MakeCase
    ) -> None:
        stored = make_case(
            status=CaseStatus.OPEN, assigned_court_representative_id=representative.id
        )

        updated = cases.update_case(
            stored.id,
            update(next_hearing_date="2026-06-10", court_name="Tribunal de Rabat"),
            actor=representative,
        )

        assert updated.next_hearing_date == date(2026, 6, 10)
        assert updated.court_name == "Tribunal de Rabat"

    def test_a_representative_may_trigger_a_status_change(
        self, cases: CaseService, representative: User, make_case: MakeCase
    ) -> None:
        # "Trigger case status updates" is part of their role description.
        stored = make_case(
            status=CaseStatus.OPEN, assigned_court_representative_id=representative.id
        )

        updated = cases.update_case(
            stored.id, update(status="waiting_for_hearing"), actor=representative
        )

        assert updated.status is CaseStatus.WAITING_FOR_HEARING

    def test_a_representative_cannot_rewrite_the_case(
        self, cases: CaseService, representative: User, make_case: MakeCase
    ) -> None:
        # They hold `cases:update-hearing`, not `cases:update`.
        stored = make_case(assigned_court_representative_id=representative.id)

        with pytest.raises(CaseAccessDeniedError):
            cases.update_case(stored.id, update(title="Rewritten"), actor=representative)

    def test_a_partially_permitted_update_is_refused_in_full(
        self, cases: CaseService, representative: User, make_case: MakeCase
    ) -> None:
        # A court representative who submits a whole case form must not have half
        # of it silently applied.
        stored = make_case(
            title="Original",
            court_name="Tribunal de Rabat",
            assigned_court_representative_id=representative.id,
        )

        with pytest.raises(CaseAccessDeniedError):
            cases.update_case(
                stored.id,
                update(title="Rewritten", court_name="Tribunal de Casablanca"),
                actor=representative,
            )

        assert (stored.title, stored.court_name) == ("Original", "Tribunal de Rabat")

    def test_a_lawyer_cannot_reassign_their_own_case(
        self, cases: CaseService, lawyer: User, representative: User, make_case: MakeCase
    ) -> None:
        # Assignment is `cases:assign`, which lawyers do not hold — otherwise a
        # lawyer could hand their case to someone else, or take another's.
        stored = make_case(assigned_lawyer_id=lawyer.id)

        with pytest.raises(CaseAccessDeniedError):
            cases.update_case(
                stored.id,
                update(assigned_court_representative_id=str(representative.id)),
                actor=lawyer,
            )

    def test_an_assignment_can_be_changed(
        self, cases: CaseService, admin: User, lawyer: User, make_case: MakeCase
    ) -> None:
        stored = make_case()

        updated = cases.update_case(
            stored.id, update(assigned_lawyer_id=str(lawyer.id)), actor=admin
        )

        assert updated.assigned_lawyer_id == lawyer.id

    def test_an_assignment_can_be_removed(
        self, cases: CaseService, admin: User, lawyer: User, make_case: MakeCase
    ) -> None:
        stored = make_case(assigned_lawyer_id=lawyer.id)

        updated = cases.update_case(stored.id, update(assigned_lawyer_id=None), actor=admin)

        assert updated.assigned_lawyer_id is None

    def test_a_wrongly_rolled_assignee_is_refused(
        self, cases: CaseService, admin: User, representative: User, make_case: MakeCase
    ) -> None:
        stored = make_case()

        with pytest.raises(InvalidAssignmentError):
            cases.update_case(
                stored.id, update(assigned_lawyer_id=str(representative.id)), actor=admin
            )

    def test_an_unchanged_assignment_is_not_revalidated(
        self, cases: CaseService, admin: User, make_user: MakeUser, make_case: MakeCase
    ) -> None:
        # A case whose lawyer was later deactivated must still be editable in
        # every other respect; refusing that would make an unrelated field
        # impossible to correct.
        former = make_user(
            email="former@example.com", password=PASSWORD, role=UserRole.LAWYER
        )
        stored = make_case(assigned_lawyer_id=former.id)
        former.status = UserStatus.INACTIVE

        updated = cases.update_case(
            stored.id,
            update(assigned_lawyer_id=str(former.id), priority="high"),
            actor=admin,
        )

        assert updated.priority is CasePriority.HIGH


class TestArchiveCase:
    def test_archiving_keeps_the_row(
        self, cases: CaseService, admin: User, make_case: MakeCase, db_session: Session
    ) -> None:
        stored = make_case()

        archived = cases.archive_case(stored.id, actor=admin)

        assert archived.status is CaseStatus.ARCHIVED
        assert archived.is_archived is True
        assert db_session.get(Case, stored.id) is not None

    def test_archiving_records_the_actor(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case()

        assert cases.archive_case(stored.id, actor=admin).updated_by == admin.id

    def test_archiving_is_idempotent(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case(status=CaseStatus.ARCHIVED)

        assert cases.archive_case(stored.id, actor=admin).status is CaseStatus.ARCHIVED

    def test_a_draft_can_be_archived(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        # Archiving is the soft delete; it must never be blocked by the state a
        # case happens to be in.
        stored = make_case(status=CaseStatus.DRAFT)

        assert cases.archive_case(stored.id, actor=admin).status is CaseStatus.ARCHIVED

    def test_an_unknown_case_is_a_not_found(self, cases: CaseService, admin: User) -> None:
        with pytest.raises(CaseNotFoundError):
            cases.archive_case(uuid.uuid4(), actor=admin)

    def test_an_unassigned_caller_is_refused(
        self, cases: CaseService, lawyer: User, make_case: MakeCase
    ) -> None:
        stored = make_case()

        with pytest.raises(CaseAccessDeniedError):
            cases.archive_case(stored.id, actor=lawyer)

    def test_an_archived_case_can_be_restored(
        self, cases: CaseService, admin: User, make_case: MakeCase
    ) -> None:
        stored = make_case()
        cases.archive_case(stored.id, actor=admin)

        restored = cases.update_case(stored.id, update(status="open"), actor=admin)

        assert restored.status is CaseStatus.OPEN
