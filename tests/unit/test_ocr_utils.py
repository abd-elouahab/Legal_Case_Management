"""Unit tests for :mod:`core.ocr`.

Pure functions, so no database, no request, no MinIO, and no installed
Tesseract — which is exactly why the lifecycle policy and the text normalisation
live there rather than inside the service.
"""

from __future__ import annotations

import pytest

from core.ocr import (
    MAX_ERROR_MESSAGE_LENGTH,
    MAX_PAGE_TEXT_CHARS,
    PAGE_SEPARATOR,
    RETRYABLE_STATUSES,
    STATUS_TRANSITIONS,
    OcrFailureCode,
    can_retry,
    can_transition,
    failure_message,
    is_paged,
    is_supported,
    join_pages,
    mean_confidence,
    normalize_confidence,
    normalize_error_message,
    normalize_extracted_text,
    normalize_language,
    sorted_supported_extensions,
    split_pages,
    success_rate,
)
from models.ocr import OcrStatus


class TestLifecycle:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (OcrStatus.PENDING, OcrStatus.PROCESSING),
            (OcrStatus.PENDING, OcrStatus.FAILED),
            (OcrStatus.PROCESSING, OcrStatus.COMPLETED),
            (OcrStatus.PROCESSING, OcrStatus.FAILED),
            (OcrStatus.COMPLETED, OcrStatus.PENDING),
            (OcrStatus.FAILED, OcrStatus.PENDING),
        ],
    )
    def test_the_legal_moves_are_allowed(self, current: OcrStatus, target: OcrStatus) -> None:
        assert can_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            # A run must be *claimed* to enter processing; jumping there from a
            # terminal state would bypass the concurrency guarantee entirely.
            (OcrStatus.COMPLETED, OcrStatus.PROCESSING),
            (OcrStatus.FAILED, OcrStatus.PROCESSING),
            # Nothing completes without having processed: the duration and the
            # start time would both be lies.
            (OcrStatus.PENDING, OcrStatus.COMPLETED),
            (OcrStatus.FAILED, OcrStatus.COMPLETED),
            (OcrStatus.COMPLETED, OcrStatus.FAILED),
        ],
    )
    def test_the_illegal_moves_are_refused(self, current: OcrStatus, target: OcrStatus) -> None:
        assert not can_transition(current, target)

    @pytest.mark.parametrize("status", list(OcrStatus))
    def test_a_status_cannot_transition_to_itself(self, status: OcrStatus) -> None:
        # "Start processing" arriving twice is a concurrency bug, not a no-op.
        assert not can_transition(status, status)

    def test_every_status_has_a_transition_entry(self) -> None:
        # A status with no entry would be a dead end no retry could recover.
        assert set(STATUS_TRANSITIONS) == set(OcrStatus)

    def test_the_transition_table_cannot_be_mutated(self) -> None:
        with pytest.raises(TypeError):
            STATUS_TRANSITIONS[OcrStatus.COMPLETED] = frozenset()  # type: ignore[index]

    def test_retryable_statuses_are_exactly_the_terminal_ones(self) -> None:
        assert {OcrStatus.COMPLETED, OcrStatus.FAILED} == RETRYABLE_STATUSES

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (OcrStatus.COMPLETED, True),
            (OcrStatus.FAILED, True),
            (OcrStatus.PENDING, False),
            (OcrStatus.PROCESSING, False),
        ],
    )
    def test_can_retry_matches_the_transition_table(
        self, status: OcrStatus, expected: bool
    ) -> None:
        assert can_retry(status) is expected
        # Derived, not restated: the two must agree by construction.
        assert can_retry(status) is can_transition(status, OcrStatus.PENDING)


class TestFormatPolicy:
    @pytest.mark.parametrize("extension", ["pdf", "png", "jpg", "jpeg"])
    def test_the_specs_formats_are_supported(self, extension: str) -> None:
        assert is_supported(extension)

    @pytest.mark.parametrize("extension", ["docx", "doc", "txt", "xlsx", ""])
    def test_other_formats_are_not(self, extension: str) -> None:
        assert not is_supported(extension)

    @pytest.mark.parametrize("value", ["PDF", " Pdf ", "JPEG"])
    def test_it_is_case_and_whitespace_insensitive(self, value: str) -> None:
        assert is_supported(value)

    def test_only_pdf_is_paged(self) -> None:
        assert is_paged("pdf")
        assert not is_paged("png")
        assert not is_paged("jpeg")

    def test_the_sorted_list_is_stable(self) -> None:
        assert sorted_supported_extensions() == ["jpeg", "jpg", "pdf", "png"]


class TestFailureCodes:
    @pytest.mark.parametrize("code", list(OcrFailureCode))
    def test_every_code_has_a_message(self, code: OcrFailureCode) -> None:
        message = failure_message(code)

        assert message
        assert message[0].isupper()
        assert message.endswith(".")

    def test_no_message_quotes_the_document(self) -> None:
        # Failure messages reach a client and a stored column. They may describe
        # what went wrong with the *file*, never what was in it.
        for code in OcrFailureCode:
            assert "text:" not in failure_message(code).lower()


class TestTextNormalisation:
    def test_it_normalises_unicode_to_nfc(self) -> None:
        # Written as escapes rather than as literals, because the two forms are
        # indistinguishable on screen — which is the whole reason this matters:
        # without NFC a future search index would treat them as different words.
        decomposed = "Procés-verbal"  # e + COMBINING ACUTE
        composed = "Procés-verbal"  # LATIN SMALL LETTER E WITH ACUTE

        assert decomposed != composed
        assert normalize_extracted_text(decomposed) == composed

    def test_it_preserves_arabic(self) -> None:
        arabic = "محضر الجلسة"

        assert normalize_extracted_text(arabic) == arabic

    def test_it_unifies_line_endings(self) -> None:
        assert normalize_extracted_text("a\r\nb\rc") == "a\nb\nc"

    def test_it_strips_control_characters_but_keeps_layout(self) -> None:
        assert normalize_extracted_text("a\x00b\x07c\td\ne") == "abc\td\ne"

    def test_it_replaces_a_stray_page_separator(self) -> None:
        # A form feed inside a page's own text would make the joined form split
        # back into the wrong pages.
        normalized = normalize_extracted_text(f"a{PAGE_SEPARATOR}b")

        assert PAGE_SEPARATOR not in normalized
        assert normalized == "a\nb"

    def test_it_collapses_runs_of_blank_lines(self) -> None:
        assert normalize_extracted_text("a\n\n\n\n\nb") == "a\n\nb"

    def test_it_keeps_a_single_paragraph_break(self) -> None:
        assert normalize_extracted_text("a\n\nb") == "a\n\nb"

    def test_it_removes_trailing_spaces_per_line(self) -> None:
        assert normalize_extracted_text("a   \nb\t\n") == "a\nb"

    def test_it_caps_the_page_length(self) -> None:
        normalized = normalize_extracted_text("x" * (MAX_PAGE_TEXT_CHARS + 500))

        assert len(normalized) == MAX_PAGE_TEXT_CHARS

    def test_empty_input_yields_empty_text(self) -> None:
        assert normalize_extracted_text("") == ""
        assert normalize_extracted_text("   \n  ") == ""


class TestPageJoining:
    def test_pages_join_with_a_form_feed(self) -> None:
        assert join_pages(["one", "two"]) == f"one{PAGE_SEPARATOR}two"

    def test_joining_and_splitting_round_trips(self) -> None:
        pages = ["one", "", "three\nlines"]

        assert split_pages(join_pages(pages)) == pages

    def test_empty_pages_are_kept(self) -> None:
        # Dropping a blank page 2 would renumber every page after it.
        assert len(split_pages(join_pages(["a", "", "c"]))) == 3

    def test_splitting_nothing_yields_no_pages(self) -> None:
        assert split_pages("") == []


class TestLanguage:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("eng", "eng"),
            ("  ENG  ", "eng"),
            ("fra+ara", "fra+ara"),
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_it_normalises(self, value: str | None, expected: str | None) -> None:
        assert normalize_language(value) == expected

    def test_it_truncates_to_the_column_width(self) -> None:
        normalized = normalize_language("eng+" * 40)

        assert normalized is not None
        assert len(normalized) == 50


class TestConfidence:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (91.456, 91.46),
            (0.0, 0.0),
            (100.0, 100.0),
            # Tesseract writes -1 for a region it could not score; that is not a
            # confidence, and reporting it as zero would be misleading.
            (-1.0, None),
            (120.0, 100.0),
            (None, None),
        ],
    )
    def test_it_clamps_and_rounds(self, value: float | None, expected: float | None) -> None:
        assert normalize_confidence(value) == expected

    def test_the_mean_ignores_unscored_pages(self) -> None:
        # A photograph page must not halve a text page's confidence.
        assert mean_confidence([90.0, None, 80.0]) == 85.0

    def test_the_mean_of_nothing_is_none(self) -> None:
        assert mean_confidence([]) is None
        assert mean_confidence([None, None]) is None


class TestErrorMessages:
    def test_it_collapses_whitespace(self) -> None:
        assert normalize_error_message("  a \n  b  ") == "a b"

    def test_a_blank_message_is_absent(self) -> None:
        assert normalize_error_message("   ") is None
        assert normalize_error_message(None) is None

    def test_it_truncates(self) -> None:
        message = normalize_error_message("x" * (MAX_ERROR_MESSAGE_LENGTH + 100))

        assert message is not None
        assert len(message) == MAX_ERROR_MESSAGE_LENGTH


class TestSuccessRate:
    def test_it_counts_only_finished_runs(self) -> None:
        assert success_rate(completed=3, failed=1) == 75.0

    def test_no_finished_runs_is_zero(self) -> None:
        # Not 100: there is nothing to have been successful at yet.
        assert success_rate(completed=0, failed=0) == 0.0

    def test_it_rounds_to_two_decimals(self) -> None:
        assert success_rate(completed=1, failed=2) == 33.33
