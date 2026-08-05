"""Unit tests for :mod:`schemas.ocr`.

The computed fields carry most of the weight here: they are what keeps a client
from re-deriving a rule and getting a different answer from the server.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.ocr import PAGE_SEPARATOR, split_pages
from models.ocr import OcrStatus
from schemas.ocr import (
    MAX_PAGE_SIZE,
    OcrListQuery,
    OcrMetricsQuery,
    OcrMetricsRead,
    OcrPageRead,
    OcrResultPage,
    OcrResultRead,
    OcrTextRead,
)


def a_result(**overrides: object) -> OcrResultRead:
    payload: dict[str, object] = {
        "id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "document_version": 1,
        "status": OcrStatus.COMPLETED,
        "attempt_count": 1,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    payload.update(overrides)
    return OcrResultRead(**payload)  # type: ignore[arg-type]


class TestOcrResultRead:
    @pytest.mark.parametrize(
        ("status", "terminal", "active", "retry"),
        [
            (OcrStatus.PENDING, False, True, False),
            (OcrStatus.PROCESSING, False, True, False),
            (OcrStatus.COMPLETED, True, False, True),
            (OcrStatus.FAILED, True, False, True),
        ],
    )
    def test_the_lifecycle_flags_follow_the_status(
        self, status: OcrStatus, terminal: bool, active: bool, retry: bool
    ) -> None:
        result = a_result(status=status)

        assert result.is_terminal is terminal
        assert result.is_active is active
        # Computed from the same transition table the service enforces, so a
        # client never offers a Retry the API would answer with 409.
        assert result.can_retry is retry

    def test_duration_is_rendered_in_seconds(self) -> None:
        assert a_result(duration_ms=1250).duration_seconds == 1.25

    def test_a_run_with_no_duration_reports_none(self) -> None:
        assert a_result().duration_seconds is None

    def test_it_carries_no_text(self) -> None:
        # The status endpoint is what a client polls; dragging a hundred pages of
        # prose across the wire on every tick is exactly what this shape avoids.
        assert "text" not in a_result().model_dump()
        assert "pages" not in a_result().model_dump()

    def test_it_serialises_the_computed_fields(self) -> None:
        payload = a_result(duration_ms=500).model_dump()

        assert payload["is_terminal"] is True
        assert payload["can_retry"] is True
        assert payload["duration_seconds"] == 0.5


class TestOcrTextRead:
    def make(self, *texts: str) -> OcrTextRead:
        return OcrTextRead(
            ocr_result_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_version=1,
            status=OcrStatus.COMPLETED,
            pages=[
                OcrPageRead(page_number=index, text=text, confidence=None)
                for index, text in enumerate(texts, start=1)
            ],
        )

    def test_full_text_joins_with_the_published_separator(self) -> None:
        text = self.make("one", "two")

        assert text.full_text == f"one{PAGE_SEPARATOR}two"
        assert text.page_separator == PAGE_SEPARATOR

    def test_splitting_full_text_recovers_the_pages(self) -> None:
        # The joined form must lose no boundary — that is what makes offering
        # both shapes safe rather than a second source of truth.
        text = self.make("one", "", "three")

        assert split_pages(text.full_text) == ["one", "", "three"]

    def test_the_counts_are_derived(self) -> None:
        text = self.make("abcd", "ef")

        assert text.page_count == 2
        assert text.character_count == 6

    def test_an_unfinished_run_carries_no_pages(self) -> None:
        text = OcrTextRead(
            ocr_result_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_version=1,
            status=OcrStatus.PROCESSING,
        )

        assert text.pages == []
        assert text.full_text == ""


class TestOcrPageRead:
    def test_the_character_count_is_derived(self) -> None:
        assert OcrPageRead(page_number=1, text="abc").character_count == 3

    @pytest.mark.parametrize(("text", "empty"), [("", True), ("   \n ", True), ("a", False)])
    def test_emptiness_ignores_whitespace(self, text: str, empty: bool) -> None:
        assert OcrPageRead(page_number=1, text=text).is_empty is empty


class TestOcrListQuery:
    def test_it_defaults_to_the_newest_first(self) -> None:
        query = OcrListQuery()

        assert query.page == 1
        assert query.sort_by.value == "created_at"
        assert query.sort_order.value == "desc"

    def test_it_rejects_an_unknown_parameter(self) -> None:
        with pytest.raises(ValidationError):
            OcrListQuery(unknown="x")  # type: ignore[call-arg]

    def test_it_rejects_an_oversized_page(self) -> None:
        with pytest.raises(ValidationError):
            OcrListQuery(page_size=MAX_PAGE_SIZE + 1)

    def test_it_rejects_a_page_below_one(self) -> None:
        with pytest.raises(ValidationError):
            OcrListQuery(page=0)

    def test_it_rejects_an_unknown_status(self) -> None:
        with pytest.raises(ValidationError):
            OcrListQuery(status="halfway")  # type: ignore[arg-type]

    def test_the_offset_follows_the_page(self) -> None:
        assert OcrListQuery(page=3, page_size=20).offset == 40


class TestOcrMetricsQuery:
    def test_the_window_is_optional(self) -> None:
        assert OcrMetricsQuery().window_days is None

    @pytest.mark.parametrize("value", [0, 366])
    def test_it_bounds_the_window(self, value: int) -> None:
        with pytest.raises(ValidationError):
            OcrMetricsQuery(window_days=value)


class TestOcrResultPage:
    def test_an_empty_result_still_reports_one_page(self) -> None:
        # So a client never renders "page 1 of 0".
        page = OcrResultPage.build([], total=0, page=1, page_size=20)

        assert page.total_pages == 1

    def test_it_derives_the_page_count(self) -> None:
        page = OcrResultPage.build([], total=45, page=1, page_size=20)

        assert page.total_pages == 3


class TestOcrMetricsRead:
    def make(self, **overrides: object) -> OcrMetricsRead:
        payload: dict[str, object] = {
            "total_runs": 10,
            "pending": 1,
            "processing": 1,
            "completed": 6,
            "failed": 2,
            "success_rate": 75.0,
            "failure_rate": 25.0,
            "engine": "tesseract",
            "engine_available": True,
            "enabled": True,
        }
        payload.update(overrides)
        return OcrMetricsRead(**payload)  # type: ignore[arg-type]

    def test_finished_runs_is_the_denominator(self) -> None:
        assert self.make().finished_runs == 8

    def test_the_average_is_rendered_in_seconds(self) -> None:
        assert self.make(average_duration_ms=2500).average_duration_seconds == 2.5

    def test_no_average_reports_none(self) -> None:
        assert self.make().average_duration_seconds is None

    def test_it_publishes_the_supported_formats(self) -> None:
        assert self.make().supported_extensions == ["jpeg", "jpg", "pdf", "png"]
