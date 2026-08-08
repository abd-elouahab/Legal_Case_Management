"""Unit tests for the report request and response schemas.

Validation and serialization, which is what the routes stay thin by delegating
to: the language allow-list, the title normaliser, the list query's bounds, and
the derived values a client renders rather than computing itself.

No database and no HTTP — these are the rules themselves, asserted where they are
written.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from models.report import ReportStatus, ReportType
from schemas.report import (
    MAX_PAGE_SIZE,
    ReportCreate,
    ReportDetailRead,
    ReportListQuery,
    ReportPage,
    ReportRead,
    ReportSortField,
    SortOrder,
)


def read(**overrides: object) -> ReportRead:
    payload: dict[str, object] = {
        "id": uuid.uuid4(),
        "case_id": uuid.uuid4(),
        "report_type": ReportType.CASE_SUMMARY,
        "title": "Synthèse — CASE-2026-0001",
        "language": "fr",
        "status": ReportStatus.COMPLETED,
        "sections_total": 4,
        "sections_completed": 4,
        "created_at": datetime(2026, 8, 7, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 7, tzinfo=UTC),
    }
    payload.update(overrides)
    return ReportRead.model_validate(payload)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class TestReportCreate:
    def test_a_minimal_request_is_the_case_and_the_type(self) -> None:
        """Everything that decides what the report *contains* comes from the
        template, so a request chooses the case, the type, and the language."""
        payload = ReportCreate(case_id=uuid.uuid4(), report_type=ReportType.CASE_SUMMARY)

        assert payload.language is None
        assert payload.title is None

    @pytest.mark.parametrize("language", ["ar", "fr", "en", "AR", "  fr  "])
    def test_a_supported_language_is_accepted_and_normalised(self, language: str) -> None:
        payload = ReportCreate(
            case_id=uuid.uuid4(), report_type=ReportType.CASE_SUMMARY, language=language
        )

        assert payload.language == language.strip().lower()

    def test_an_unsupported_language_is_refused_rather_than_ignored(self) -> None:
        """A caller who asked for German and received French would have no way to
        tell the request was understood and overruled rather than honoured."""
        with pytest.raises(ValidationError) as failure:
            ReportCreate(
                case_id=uuid.uuid4(), report_type=ReportType.CASE_SUMMARY, language="de"
            )

        assert "available in" in str(failure.value)

    def test_a_title_is_collapsed(self) -> None:
        payload = ReportCreate(
            case_id=uuid.uuid4(),
            report_type=ReportType.CASE_SUMMARY,
            title="  Note   d'audience  ",
        )

        assert payload.title == "Note d'audience"

    def test_a_blank_title_is_treated_as_absent(self) -> None:
        payload = ReportCreate(
            case_id=uuid.uuid4(), report_type=ReportType.CASE_SUMMARY, title="   "
        )

        assert payload.title is None

    def test_an_over_long_title_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ReportCreate(
                case_id=uuid.uuid4(),
                report_type=ReportType.CASE_SUMMARY,
                title="x" * 300,
            )

    def test_an_unknown_report_type_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ReportCreate(case_id=uuid.uuid4(), report_type="compliance_review")  # type: ignore[arg-type]

    def test_unknown_fields_are_refused(self) -> None:
        """``extra="forbid"`` throughout, so a client sending a field the API does
        not have is told rather than silently ignored."""
        with pytest.raises(ValidationError):
            ReportCreate(
                case_id=uuid.uuid4(),
                report_type=ReportType.CASE_SUMMARY,
                sections=["overview"],  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


class TestReportListQuery:
    def test_the_default_is_newest_first(self) -> None:
        """A history is read that way."""
        query = ReportListQuery()

        assert query.sort_by is ReportSortField.CREATED_AT
        assert query.sort_order is SortOrder.DESC

    def test_the_page_size_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            ReportListQuery(page_size=MAX_PAGE_SIZE + 1)

    def test_the_page_is_one_based(self) -> None:
        with pytest.raises(ValidationError):
            ReportListQuery(page=0)

    def test_the_offset_is_derived_from_the_page(self) -> None:
        assert ReportListQuery(page=3, page_size=20).offset == 40

    def test_a_blank_search_is_treated_as_absent(self) -> None:
        assert ReportListQuery(search="   ").search is None

    def test_there_is_no_requester_filter(self) -> None:
        """The history is the caller's own by construction, so a filter naming a
        user would either be redundant or be a request the API must refuse — and
        offering it would suggest the second is possible."""
        assert "requested_by" not in ReportListQuery.model_fields


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class TestReportRead:
    def test_a_history_row_carries_no_sections(self) -> None:
        """A page of twenty reports carrying twenty full reports would be
        megabytes of generated legal prose sent to render a list of titles."""
        assert "sections" not in ReportRead.model_fields

    @pytest.mark.parametrize(
        ("status", "terminal", "active"),
        [
            (ReportStatus.PENDING, False, True),
            (ReportStatus.PROCESSING, False, True),
            (ReportStatus.COMPLETED, True, False),
            (ReportStatus.FAILED, True, False),
        ],
    )
    def test_the_polling_flags_are_derived_from_the_status(
        self, status: ReportStatus, terminal: bool, active: bool
    ) -> None:
        row = read(status=status)

        assert row.is_terminal is terminal
        assert row.is_active is active

    def test_a_completed_report_is_a_hundred_percent(self) -> None:
        assert read(sections_completed=3, sections_total=4).progress_percent == 100

    def test_progress_is_derived_from_the_two_counters(self) -> None:
        row = read(status=ReportStatus.PROCESSING, sections_completed=2, sections_total=4)

        assert row.progress_percent == 50

    def test_an_unplanned_run_reports_no_progress_rather_than_dividing_by_nothing(
        self,
    ) -> None:
        row = read(status=ReportStatus.PENDING, sections_total=None, sections_completed=0)

        assert row.progress_percent == 0

    def test_the_duration_is_formatted_once_server_side(self) -> None:
        assert read(duration_ms=12_340).duration_seconds == 12.34

    def test_an_unfinished_run_reports_no_duration(self) -> None:
        assert read(duration_ms=None).duration_seconds is None


class TestReportDetailRead:
    def test_it_carries_the_sections_the_citations_and_the_front_matter(self) -> None:
        detail = ReportDetailRead(
            **read().model_dump(),
            sections=[],
            citations=[],
            references_title="Références",
            disclaimer="Rapport généré automatiquement.",
        )

        assert detail.references_title == "Références"
        assert detail.disclaimer

    def test_distinct_documents_are_counted_rather_than_citations(self) -> None:
        """Three pages of one contract are one source to a lawyer."""
        document = uuid.uuid4()
        citations = [
            {
                "marker": marker,
                "document_id": document,
                "document_name": "bail.pdf",
                "document_version": 1,
                "page_number": marker,
                "case_id": uuid.uuid4(),
                "score": 0.8,
                "excerpt": "…",
            }
            for marker in (1, 2, 3)
        ]

        detail = ReportDetailRead(
            **read().model_dump(),
            sections=[],
            citations=citations,  # type: ignore[arg-type]
            references_title="Références",
            disclaimer="Note.",
        )

        assert detail.citation_count == 3
        assert detail.document_count == 1


class TestReportPage:
    def test_an_empty_result_still_reports_one_page(self) -> None:
        """So a client never renders "page 1 of 0"."""
        page = ReportPage.build([], total=0, page=1, page_size=20)

        assert page.total_pages == 1

    def test_the_page_count_is_derived(self) -> None:
        page = ReportPage.build([], total=41, page=1, page_size=20)

        assert page.total_pages == 3
