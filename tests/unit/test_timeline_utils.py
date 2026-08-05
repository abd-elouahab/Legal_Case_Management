"""Unit tests for :mod:`core.timeline`.

Pure functions: the event registry's presentation and the normalisation applied
to everything recorded. No database, no request, no service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from core.timeline import (
    DEFAULT_TITLES,
    EVENT_CATEGORIES,
    MAX_ACTOR_NAME_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_METADATA_BYTES,
    MAX_TITLE_LENGTH,
    InvalidTimelineMetadataError,
    category_for,
    default_title,
    humanize,
    known_event_types,
    normalize_actor_name,
    normalize_description,
    normalize_metadata,
    normalize_title,
)
from models.case import CaseStatus
from models.document import DocumentCategory
from models.timeline import TimelineEventCategory, TimelineEventType


class TestRegistryCoverage:
    def test_every_event_type_has_a_category(self) -> None:
        # A type with no category would silently render under the wrong icon.
        assert set(EVENT_CATEGORIES) == set(TimelineEventType)

    def test_every_event_type_has_a_default_title(self) -> None:
        assert set(DEFAULT_TITLES) == set(TimelineEventType)

    def test_the_spec_s_fifteen_event_types_are_all_present(self) -> None:
        assert known_event_types() == [
            "case_created",
            "case_updated",
            "case_archived",
            "case_restored",
            "status_changed",
            "priority_changed",
            "lawyer_assigned",
            "lawyer_removed",
            "representative_assigned",
            "representative_removed",
            "document_uploaded",
            "document_updated",
            "document_replaced",
            "document_deleted",
            "document_downloaded",
        ]

    def test_the_five_icon_families_are_all_used(self) -> None:
        # `08-timeline.md` names five icon groups; an unused one would mean an
        # event family nothing can be filed under.
        assert set(EVENT_CATEGORIES.values()) == set(TimelineEventCategory)

    def test_the_registry_cannot_be_widened_at_runtime(self) -> None:
        with pytest.raises(TypeError):
            EVENT_CATEGORIES[TimelineEventType.CASE_CREATED] = (  # type: ignore[index]
                TimelineEventCategory.DOCUMENT
            )


class TestCategoryFor:
    @pytest.mark.parametrize(
        ("event_type", "expected"),
        [
            (TimelineEventType.CASE_CREATED, TimelineEventCategory.CASE),
            (TimelineEventType.STATUS_CHANGED, TimelineEventCategory.STATUS),
            (TimelineEventType.PRIORITY_CHANGED, TimelineEventCategory.PRIORITY),
            (TimelineEventType.LAWYER_ASSIGNED, TimelineEventCategory.ASSIGNMENT),
            (TimelineEventType.REPRESENTATIVE_REMOVED, TimelineEventCategory.ASSIGNMENT),
            (TimelineEventType.DOCUMENT_UPLOADED, TimelineEventCategory.DOCUMENT),
        ],
    )
    def test_it_maps_a_type_onto_its_family(
        self, event_type: TimelineEventType, expected: TimelineEventCategory
    ) -> None:
        assert category_for(event_type.value) is expected

    def test_an_unknown_type_falls_back_rather_than_raising(self) -> None:
        # Read on the way *out* of the database: a row written by a later version
        # of the platform must still render.
        assert category_for("hearing_scheduled") is TimelineEventCategory.CASE


class TestDefaultTitle:
    def test_it_returns_the_registered_headline(self) -> None:
        assert default_title(TimelineEventType.DOCUMENT_UPLOADED.value) == "Document Uploaded"

    def test_an_unregistered_type_reads_as_english(self) -> None:
        assert default_title("court_hearing_added") == "Court Hearing Added"

    def test_an_empty_type_still_produces_a_title(self) -> None:
        assert default_title("") == "Event"


class TestHumanize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("in_progress", "In progress"),
            ("waiting_for_hearing", "Waiting for hearing"),
            ("urgent", "Urgent"),
            ("", ""),
        ],
    )
    def test_it_renders_an_identifier_as_a_phrase(self, value: str, expected: str) -> None:
        assert humanize(value) == expected

    def test_it_capitalises_only_the_first_word(self) -> None:
        # These appear inside sentences, so title-casing would read as a heading.
        assert humanize(CaseStatus.WAITING_FOR_HEARING.value) == "Waiting for hearing"


class TestNormalizeTitle:
    def test_it_collapses_whitespace(self) -> None:
        assert normalize_title("  Document   Uploaded \n") == "Document Uploaded"

    def test_it_truncates_rather_than_rejecting(self) -> None:
        # Losing the event would be far worse than losing the end of its headline.
        assert len(normalize_title("x" * (MAX_TITLE_LENGTH + 50))) == MAX_TITLE_LENGTH


class TestNormalizeDescription:
    def test_none_stays_none(self) -> None:
        assert normalize_description(None) is None

    def test_a_blank_string_becomes_none(self) -> None:
        assert normalize_description("   \n ") is None

    def test_it_preserves_line_breaks(self) -> None:
        assert normalize_description(" first\nsecond ") == "first\nsecond"

    def test_it_truncates_to_the_ceiling(self) -> None:
        assert len(normalize_description("y" * 5_000) or "") == MAX_DESCRIPTION_LENGTH


class TestNormalizeActorName:
    def test_it_collapses_and_trims(self) -> None:
        assert normalize_actor_name("  Amina   Benali  ") == "Amina Benali"

    def test_a_blank_name_becomes_none(self) -> None:
        assert normalize_actor_name("  ") is None

    def test_it_truncates_to_the_column_width(self) -> None:
        assert len(normalize_actor_name("z" * 400) or "") == MAX_ACTOR_NAME_LENGTH


class TestNormalizeMetadata:
    def test_absent_metadata_is_an_empty_object(self) -> None:
        # Never null, so no reader has to handle both.
        assert normalize_metadata(None) == {}
        assert normalize_metadata({}) == {}

    def test_none_values_are_dropped(self) -> None:
        assert normalize_metadata({"from": "open", "to": None}) == {"from": "open"}

    def test_uuids_dates_and_enums_are_rendered_as_text(self) -> None:
        document_id = uuid.uuid4()
        result = normalize_metadata(
            {
                "document_id": document_id,
                "filed_on": date(2026, 7, 31),
                "at": datetime(2026, 7, 31, 14, 32, tzinfo=UTC),
                "category": DocumentCategory.EVIDENCE,
                "status": CaseStatus.IN_PROGRESS,
            }
        )

        assert result == {
            "document_id": str(document_id),
            "filed_on": "2026-07-31",
            "at": "2026-07-31T14:32:00+00:00",
            # StrEnum members must store as their *value*, not as the member.
            "category": "evidence",
            "status": "in_progress",
        }

    def test_scalars_pass_through_unchanged(self) -> None:
        assert normalize_metadata({"version": 3, "size": 1.5, "ok": True, "name": "a"}) == {
            "version": 3,
            "size": 1.5,
            "ok": True,
            "name": "a",
        }

    def test_nested_structures_are_normalised(self) -> None:
        assert normalize_metadata({"fields": ["title", "court_name"], "who": {"id": 1}}) == {
            "fields": ["title", "court_name"],
            "who": {"id": 1},
        }

    def test_keys_are_coerced_to_strings(self) -> None:
        assert normalize_metadata({1: "a"}) == {"1": "a"}  # type: ignore[dict-item]

    def test_deep_nesting_degrades_to_text_rather_than_recursing_forever(self) -> None:
        deep: dict[str, object] = {"a": {"b": {"c": {"d": {"e": "bottom"}}}}}
        result = normalize_metadata(deep)

        # Bounded work per event; the specifics survive as text.
        assert isinstance(result["a"], dict)
        assert "bottom" in str(result["a"])

    def test_oversized_metadata_is_rejected(self) -> None:
        with pytest.raises(InvalidTimelineMetadataError):
            normalize_metadata({"blob": "x" * (MAX_METADATA_BYTES + 1)})

    def test_a_value_at_the_ceiling_is_accepted(self) -> None:
        assert normalize_metadata({"blob": "x" * 100})["blob"] == "x" * 100
