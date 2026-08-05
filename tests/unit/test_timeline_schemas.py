"""Unit tests for :mod:`schemas.timeline`.

The query model's validation and the read model's serialization — in particular
that ``metadata`` survives the ORM's ``event_metadata`` attribute name, and that
``category`` is computed rather than accepted from anywhere.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from models.timeline import TimelineEvent, TimelineEventCategory, TimelineEventType
from schemas.case import SortOrder
from schemas.timeline import (
    MAX_PAGE_SIZE,
    TimelineEventPage,
    TimelineEventRead,
    TimelineListQuery,
    TimelineSortField,
)


def _event(**overrides: object) -> TimelineEvent:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "case_id": uuid.uuid4(),
        "event_type": TimelineEventType.DOCUMENT_UPLOADED.value,
        "title": "Document Uploaded",
        "description": 'Amina Benali uploaded "Contract.pdf".',
        "actor_id": uuid.uuid4(),
        "actor_name": "Amina Benali",
        "actor_role": "administrator",
        "event_metadata": {"filename": "Contract.pdf"},
        "created_at": datetime(2026, 7, 31, 14, 32, tzinfo=UTC),
    }
    return TimelineEvent(**(defaults | overrides))  # type: ignore[arg-type]


class TestTimelineEventRead:
    def test_it_reads_the_orm_row_including_metadata(self) -> None:
        payload = TimelineEventRead.model_validate(_event())

        # The ORM attribute is `event_metadata` (SQLAlchemy owns `metadata`);
        # the wire field is `metadata`, as the spec specifies.
        assert payload.metadata == {"filename": "Contract.pdf"}
        assert payload.title == "Document Uploaded"
        assert payload.actor_name == "Amina Benali"

    def test_metadata_serializes_under_the_spec_s_name(self) -> None:
        dumped = TimelineEventRead.model_validate(_event()).model_dump()

        assert "metadata" in dumped
        assert "event_metadata" not in dumped

    def test_category_is_computed_from_the_event_type(self) -> None:
        payload = TimelineEventRead.model_validate(
            _event(event_type=TimelineEventType.STATUS_CHANGED.value)
        )

        assert payload.category is TimelineEventCategory.STATUS
        assert payload.model_dump()["category"] == "status"

    def test_an_unknown_event_type_still_serializes(self) -> None:
        # The registry is an open set; a row written by a later version of the
        # platform must not 500 the endpoint that reads it.
        payload = TimelineEventRead.model_validate(_event(event_type="hearing_scheduled"))

        assert payload.event_type == "hearing_scheduled"
        assert payload.category is TimelineEventCategory.CASE

    def test_metadata_defaults_to_an_empty_object(self) -> None:
        assert TimelineEventRead.model_validate(_event(event_metadata={})).metadata == {}

    def test_it_can_also_be_built_from_the_wire_field_name(self) -> None:
        payload = TimelineEventRead(
            id=uuid.uuid4(),
            case_id=uuid.uuid4(),
            event_type=TimelineEventType.CASE_CREATED.value,
            title="Case Created",
            metadata={"case_number": "CASE-2026-0001"},
            created_at=datetime.now(UTC),
        )

        assert payload.metadata == {"case_number": "CASE-2026-0001"}


class TestTimelineEventPage:
    def test_it_derives_the_page_count(self) -> None:
        page = TimelineEventPage.build([], total=41, page=1, page_size=20)

        assert page.total_pages == 3

    def test_an_empty_result_still_reports_one_page(self) -> None:
        # So a client never renders "page 1 of 0".
        assert TimelineEventPage.build([], total=0, page=1, page_size=20).total_pages == 1


class TestTimelineListQuery:
    def test_it_defaults_to_newest_first(self) -> None:
        query = TimelineListQuery()

        # Reverse chronological is how a timeline is read.
        assert query.sort_order is SortOrder.DESC
        assert query.sort_by is TimelineSortField.CREATED_AT

    def test_a_blank_search_term_becomes_none(self) -> None:
        assert TimelineListQuery(search="   ").search is None

    def test_an_event_type_filter_is_lowercased(self) -> None:
        # Identifiers are stored lowercase, so a filter typed in any case matches.
        assert TimelineListQuery(event_type=" STATUS_CHANGED ").event_type == "status_changed"

    def test_an_unregistered_event_type_is_accepted_as_a_filter(self) -> None:
        # An open registry means a later module's type must stay filterable.
        assert TimelineListQuery(event_type="hearing_scheduled").event_type == "hearing_scheduled"

    def test_an_inverted_date_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TimelineListQuery(date_from="2026-07-31", date_to="2026-07-01")  # type: ignore[arg-type]

    def test_an_equal_date_range_is_accepted(self) -> None:
        assert TimelineListQuery(date_from="2026-07-31", date_to="2026-07-31").date_to is not None  # type: ignore[arg-type]

    def test_the_page_size_is_capped(self) -> None:
        with pytest.raises(ValidationError):
            TimelineListQuery(page_size=MAX_PAGE_SIZE + 1)

    def test_page_zero_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TimelineListQuery(page=0)

    def test_unknown_parameters_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TimelineListQuery(case_id=str(uuid.uuid4()))  # type: ignore[call-arg]

    def test_the_offset_follows_the_page(self) -> None:
        assert TimelineListQuery(page=3, page_size=20).offset == 40
