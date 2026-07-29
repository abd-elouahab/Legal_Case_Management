"""Tests for the case request and response schemas.

What belongs here is everything the schemas can decide *without* a database:
normalization, required fields, the date rule that needs both values, the
immutable-field guard, and the omitted-versus-null distinction a PATCH rests on.
Rules that need stored state — uniqueness, assignee roles, legal transitions —
are the service's, and are tested there.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from models.case import CasePriority, CaseStatus
from schemas.case import (
    CaseAssignmentUpdate,
    CaseCreate,
    CaseListQuery,
    CasePage,
    CaseRead,
    CaseUpdate,
)


def creation(**overrides: object) -> dict[str, object]:
    return {"title": "Benali v. Société Atlas", **overrides}


class TestCaseCreate:
    def test_only_a_title_is_required(self) -> None:
        payload = CaseCreate.model_validate(creation())

        assert payload.case_number is None
        assert payload.status is CaseStatus.DRAFT
        assert payload.priority is CasePriority.MEDIUM

    def test_a_blank_title_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CaseCreate.model_validate(creation(title="   "))

    def test_text_fields_are_normalized(self) -> None:
        payload = CaseCreate.model_validate(
            creation(
                title="  Benali   v.  Atlas ",
                category="  Commercial  ",
                court_name="  Tribunal   de Commerce ",
                description="  Summary.\n\nMore.  ",
            )
        )

        assert payload.title == "Benali v. Atlas"
        assert payload.category == "Commercial"
        assert payload.court_name == "Tribunal de Commerce"
        # Prose keeps its paragraphs; only the surrounding whitespace goes.
        assert payload.description == "Summary.\n\nMore."

    @pytest.mark.parametrize("field", ["description", "category", "court_name"])
    def test_a_blank_optional_field_means_absent(self, field: str) -> None:
        # An empty form field means "not recorded", not "a value of length zero".
        payload = CaseCreate.model_validate(creation(**{field: "   "}))

        assert getattr(payload, field) is None

    def test_a_blank_case_number_asks_for_a_generated_one(self) -> None:
        assert CaseCreate.model_validate(creation(case_number="  ")).case_number is None

    def test_a_supplied_case_number_is_uppercased(self) -> None:
        # So the same reference cannot be filed twice in different casings.
        payload = CaseCreate.model_validate(creation(case_number="tc/2026/44"))

        assert payload.case_number == "TC/2026/44"

    def test_a_malformed_case_number_names_the_field(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            CaseCreate.model_validate(creation(case_number="TC 2026!"))

        assert excinfo.value.errors()[0]["loc"] == ("case_number",)

    def test_a_hearing_before_the_filing_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CaseCreate.model_validate(
                creation(filing_date="2026-05-10", next_hearing_date="2026-05-09")
            )

    def test_a_hearing_on_the_filing_date_is_allowed(self) -> None:
        payload = CaseCreate.model_validate(
            creation(filing_date="2026-05-10", next_hearing_date="2026-05-10")
        )

        assert payload.next_hearing_date == date(2026, 5, 10)

    def test_a_past_hearing_with_no_filing_date_is_allowed(self) -> None:
        # A case can be recorded after the fact; only the *relative* order of the
        # two dates is knowable here.
        payload = CaseCreate.model_validate(creation(next_hearing_date="2020-01-01"))

        assert payload.next_hearing_date == date(2020, 1, 1)

    @pytest.mark.parametrize("field", ["created_by", "updated_by", "id", "created_at"])
    def test_audit_and_identity_fields_cannot_be_supplied(self, field: str) -> None:
        # `extra="forbid"`: a client must not be able to claim someone else filed
        # the case, or to choose its identifier.
        with pytest.raises(ValidationError):
            CaseCreate.model_validate(creation(**{field: str(uuid.uuid4())}))

    def test_an_unknown_status_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CaseCreate.model_validate(creation(status="settled"))


class TestCaseUpdate:
    def test_an_empty_patch_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CaseUpdate.model_validate({})

    def test_only_the_supplied_fields_are_reported(self) -> None:
        # The whole PATCH contract: an omitted field must be left alone, which is
        # indistinguishable from "cleared" without `exclude_unset`.
        changes = CaseUpdate.model_validate({"priority": "urgent"}).provided_fields()

        assert changes == {"priority": CasePriority.URGENT}

    def test_an_explicit_null_is_distinguishable_from_omission(self) -> None:
        changes = CaseUpdate.model_validate({"court_name": None}).provided_fields()

        assert changes == {"court_name": None}

    def test_removing_an_assignment_is_an_explicit_null(self) -> None:
        changes = CaseUpdate.model_validate({"assigned_lawyer_id": None}).provided_fields()

        assert changes == {"assigned_lawyer_id": None}

    @pytest.mark.parametrize("field", ["case_number", "id", "created_by", "created_at"])
    def test_immutable_fields_are_not_part_of_the_schema(self, field: str) -> None:
        # They are absent rather than validated and rejected, so there is no
        # field here to forget to guard.
        assert field not in CaseUpdate.model_fields

        with pytest.raises(ValidationError):
            CaseUpdate.model_validate({field: "anything"})

    def test_both_dates_together_are_checked(self) -> None:
        with pytest.raises(ValidationError):
            CaseUpdate.model_validate(
                {"filing_date": "2026-05-10", "next_hearing_date": "2026-05-09"}
            )

    def test_one_date_alone_is_left_to_the_service(self) -> None:
        # The other value is only knowable from the stored case, so the schema
        # cannot decide this and must not pretend to.
        payload = CaseUpdate.model_validate({"next_hearing_date": "2020-01-01"})

        assert payload.next_hearing_date == date(2020, 1, 1)


class TestCaseAssignmentUpdate:
    def test_an_empty_body_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CaseAssignmentUpdate.model_validate({})

    def test_it_becomes_a_partial_case_update(self) -> None:
        lawyer_id = uuid.uuid4()

        changes = (
            CaseAssignmentUpdate.model_validate({"assigned_lawyer_id": str(lawyer_id)})
            .to_case_update()
            .provided_fields()
        )

        # Only the field that was sent — so changing the lawyer cannot silently
        # unassign the court representative.
        assert changes == {"assigned_lawyer_id": lawyer_id}

    def test_a_null_survives_the_conversion(self) -> None:
        changes = (
            CaseAssignmentUpdate.model_validate({"assigned_court_representative_id": None})
            .to_case_update()
            .provided_fields()
        )

        assert changes == {"assigned_court_representative_id": None}


class TestCaseListQuery:
    def test_it_defaults_to_the_first_page_newest_first(self) -> None:
        query = CaseListQuery()

        assert (query.page, query.offset) == (1, 0)
        assert query.sort_by.value == "created_at"
        assert query.sort_order.value == "desc"

    def test_the_offset_follows_the_page(self) -> None:
        assert CaseListQuery(page=3, page_size=20).offset == 40

    def test_a_page_size_beyond_the_ceiling_is_rejected(self) -> None:
        # The ceiling is what stops a single request dumping the whole caseload.
        with pytest.raises(ValidationError):
            CaseListQuery(page_size=1_000)

    def test_a_blank_search_term_means_no_search(self) -> None:
        assert CaseListQuery(search="   ").search is None

    @pytest.mark.parametrize(
        ("earlier", "later"),
        [("filing_date_from", "filing_date_to"), ("hearing_date_from", "hearing_date_to")],
    )
    def test_an_inverted_date_range_is_rejected(self, earlier: str, later: str) -> None:
        # An inverted range matches nothing, which reads as a broken filter
        # rather than as the input error it is.
        with pytest.raises(ValidationError):
            CaseListQuery.model_validate({earlier: "2026-06-01", later: "2026-05-01"})

    def test_an_unknown_sort_column_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CaseListQuery.model_validate({"sort_by": "title"})


class TestCaseRead:
    def test_allowed_transitions_are_derived_from_the_status(self) -> None:
        payload = CaseRead.model_validate(
            {
                "id": uuid.uuid4(),
                "case_number": "CASE-2026-0001",
                "title": "Benali v. Atlas",
                "status": CaseStatus.DRAFT,
                "priority": CasePriority.MEDIUM,
                "created_at": "2026-07-01T09:00:00Z",
                "updated_at": "2026-07-01T09:00:00Z",
                "is_archived": False,
            }
        )

        # Computed rather than stored, so the payload cannot drift from the
        # lifecycle rules — and served in lifecycle order, not set order.
        assert payload.allowed_transitions == [CaseStatus.OPEN, CaseStatus.ARCHIVED]

    def test_a_closed_case_offers_reopening(self) -> None:
        payload = CaseRead.model_validate(
            {
                "id": uuid.uuid4(),
                "case_number": "CASE-2026-0002",
                "title": "Closed matter",
                "status": CaseStatus.CLOSED,
                "priority": CasePriority.LOW,
                "created_at": "2026-07-01T09:00:00Z",
                "updated_at": "2026-07-01T09:00:00Z",
                "is_archived": False,
            }
        )

        assert CaseStatus.OPEN in payload.allowed_transitions


class TestCasePage:
    def test_an_empty_result_still_reports_one_page(self) -> None:
        # So a client never renders "page 1 of 0".
        page = CasePage.build([], total=0, page=1, page_size=20)

        assert page.total_pages == 1

    def test_the_page_count_rounds_up(self) -> None:
        assert CasePage.build([], total=41, page=1, page_size=20).total_pages == 3
