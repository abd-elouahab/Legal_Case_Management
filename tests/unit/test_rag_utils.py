"""Unit tests for :mod:`core.rag`.

Pure functions, tested without a database, a request, a running Qdrant, a
downloaded embedding model, or an API key — which is the whole reason they live
in ``core`` rather than inside a service method.

The two that carry the most weight here are :func:`~core.rag.fit_to_budget`,
because a bug in it silently changes what evidence every answer on the platform
is built from, and :func:`~core.rag.question_fingerprint`, because it is what
stands between an operator's log and a lawyer's question.
"""

from __future__ import annotations

import pytest

from core.rag import (
    CITATION_MARKER_PATTERN,
    FAILURE_MESSAGES,
    INSUFFICIENT_EVIDENCE_MARKER,
    MIN_PASSAGE_CHARACTERS,
    MIN_QUESTION_LENGTH,
    NO_EVIDENCE_MESSAGES,
    SUPPORTED_ANSWER_LANGUAGES,
    RagFailureCode,
    cited_markers,
    clip_passage,
    failure_message,
    fit_to_budget,
    is_answerable_question,
    is_insufficient_evidence,
    is_usable_answer,
    language_name,
    loggable_question,
    no_evidence_message,
    normalize_question,
    question_fingerprint,
    resolve_answer_language,
    unknown_markers,
)


class TestFailureVocabulary:
    def test_every_failure_code_has_a_message(self) -> None:
        for code in RagFailureCode:
            assert code in FAILURE_MESSAGES
            assert failure_message(code).strip()

    def test_no_failure_message_mentions_a_question_or_a_document(self) -> None:
        """A failure says what went wrong, never what was being asked or read."""
        for message in FAILURE_MESSAGES.values():
            lowered = message.lower()
            assert "question:" not in lowered
            assert "document:" not in lowered

    def test_there_is_no_failure_code_for_missing_evidence(self) -> None:
        """Declining to answer is a successful outcome, not a failure of the pipeline."""
        values = {code.value for code in RagFailureCode}
        assert "no_evidence" not in values
        assert "insufficient_evidence" not in values

    def test_one_timeout_code_covers_both_deadlines(self) -> None:
        assert RagFailureCode.TIMEOUT.value == "timeout"
        assert "llm_timeout" not in {code.value for code in RagFailureCode}


class TestQuestionNormalisation:
    def test_whitespace_is_collapsed(self) -> None:
        assert normalize_question("  loyer   commercial \n") == "loyer commercial"

    def test_decomposed_french_normalises_to_the_composed_form(self) -> None:
        """The indexed passages are NFC, so a decomposed question must match them."""
        decomposed = "resilié"
        assert normalize_question(decomposed) == "resilié"

    def test_control_characters_are_dropped(self) -> None:
        assert "\x00" not in normalize_question("loyer\x00commercial")

    @pytest.mark.parametrize("value", ["", " ", "?", "!!", "a"])
    def test_a_question_with_nothing_to_answer_is_refused(self, value: str) -> None:
        assert is_answerable_question(value) is False

    @pytest.mark.parametrize("value", ["ou", "12", "Quand le loyer est-il du ?"])
    def test_a_real_question_is_accepted(self, value: str) -> None:
        assert is_answerable_question(value) is True

    def test_the_floor_is_two_characters(self) -> None:
        assert MIN_QUESTION_LENGTH == 2


class TestFingerprint:
    def test_the_same_question_fingerprints_identically(self) -> None:
        assert question_fingerprint("loyer") == question_fingerprint("loyer")

    def test_normalisation_happens_before_hashing(self) -> None:
        assert question_fingerprint("  loyer  ") == question_fingerprint("loyer")

    def test_different_questions_fingerprint_differently(self) -> None:
        assert question_fingerprint("loyer") != question_fingerprint("preavis")

    def test_the_fingerprint_does_not_contain_the_question(self) -> None:
        assert "loyer" not in question_fingerprint("loyer")

    def test_the_fingerprint_is_salted_with_the_deployment_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the salt, a digest of a common legal term is identical everywhere."""
        from core.config import settings

        first = question_fingerprint("divorce")
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "a-different-deployment-secret")
        assert question_fingerprint("divorce") != first

    def test_a_search_and_a_question_share_one_fingerprint(self) -> None:
        """So an operator can correlate a failing phrase across both surfaces."""
        from core.search import query_fingerprint

        assert question_fingerprint("loyer commercial") == query_fingerprint("loyer commercial")


class TestQuestionLogging:
    def test_the_question_is_withheld_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "RAG_LOG_QUESTIONS", False)
        assert loggable_question("Quand le loyer est-il du ?") is None

    def test_an_operator_can_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "RAG_LOG_QUESTIONS", True)
        assert loggable_question("  loyer  ") == "loyer"

    def test_the_switch_is_separate_from_the_search_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opting into search-query logging must not opt into question logging."""
        from core.config import settings

        monkeypatch.setattr(settings, "SEARCH_LOG_QUERIES", True)
        monkeypatch.setattr(settings, "RAG_LOG_QUESTIONS", False)
        assert loggable_question("loyer") is None


class TestAnswerLanguage:
    def test_an_explicit_request_wins_over_detection(self) -> None:
        """A French interface asking about an Arabic filing wants a French answer."""
        arabic_question = "متى يؤدى الكراء الشهري؟"
        assert resolve_answer_language(arabic_question, "fr") == "fr"

    def test_the_language_is_detected_when_none_is_requested(self) -> None:
        assert resolve_answer_language("متى يؤدى الكراء الشهري؟") == "ar"
        assert resolve_answer_language("Quand le loyer doit-il être payé ?") == "fr"

    def test_an_undetectable_question_falls_back_to_french(self) -> None:
        """`und` is not an instruction; French is the platform's working language."""
        assert resolve_answer_language("2024 / 1187") == "fr"

    def test_an_unsupported_request_falls_through_to_detection(self) -> None:
        assert resolve_answer_language("Quand le loyer est-il dû ?", "de") == "fr"

    def test_supported_languages_are_the_three_the_platform_serves(self) -> None:
        assert frozenset({"ar", "fr", "en"}) == SUPPORTED_ANSWER_LANGUAGES

    def test_a_language_is_named_rather_than_coded_for_the_prompt(self) -> None:
        assert language_name("fr") == "French"
        assert language_name("ar") == "Arabic"
        assert language_name("und") == "English"


class TestNoEvidenceMessages:
    def test_every_supported_language_has_a_message(self) -> None:
        for language in SUPPORTED_ANSWER_LANGUAGES:
            assert no_evidence_message(language).strip()

    def test_an_unknown_language_falls_back_rather_than_failing(self) -> None:
        assert no_evidence_message("de") == NO_EVIDENCE_MESSAGES["en"]

    def test_no_message_speculates(self) -> None:
        """The point of a fixed sentence is that it cannot answer anyway."""
        for message in NO_EVIDENCE_MESSAGES.values():
            assert INSUFFICIENT_EVIDENCE_MARKER not in message


class TestClipping:
    def test_a_short_passage_is_returned_unchanged(self) -> None:
        assert clip_passage("Le loyer.", 100) == "Le loyer."

    def test_a_long_passage_is_clipped_at_a_word_boundary(self) -> None:
        text = "Le loyer mensuel est payable d'avance le premier jour de chaque mois."
        clipped = clip_passage(text, 40)

        assert len(clipped) <= 41  # the ellipsis
        assert clipped.endswith("…")
        assert not clipped[:-1].endswith(" ")

    def test_a_script_without_spaces_is_still_clipped(self) -> None:
        """Arabic in the window may hold no space; the excerpt must not vanish."""
        text = "ا" * 500  # noqa: RUF001 - Arabic alef, deliberately
        clipped = clip_passage(text, 100)

        assert clipped.endswith("…")
        assert len(clipped) == 101


class TestContextBudget:
    def test_everything_fits_when_the_budget_is_generous(self) -> None:
        assert fit_to_budget([300, 400], budget=10_000, max_passage=5_000) == [300, 400]

    def test_a_passage_is_capped_at_the_per_passage_ceiling(self) -> None:
        assert fit_to_budget([9_000], budget=10_000, max_passage=4_000) == [4_000]

    def test_the_last_passage_is_truncated_when_something_usable_remains(self) -> None:
        kept = fit_to_budget([700, 700], budget=1_000, max_passage=5_000)

        assert kept == [700, 300]
        assert kept[1] >= MIN_PASSAGE_CHARACTERS

    def test_a_passage_that_would_leave_a_fragment_is_dropped(self) -> None:
        """A 50-character tail is not evidence, and quoting it as such is worse."""
        assert fit_to_budget([900, 700], budget=1_000, max_passage=5_000) == [900, 0]

    def test_nothing_is_kept_after_the_budget_is_exhausted(self) -> None:
        """Skipping ahead to a shorter passage would reorder evidence by length."""
        assert fit_to_budget([1_000, 900, 10], budget=1_000, max_passage=5_000) == [1_000, 0, 0]

    def test_relevance_order_is_preserved(self) -> None:
        kept = fit_to_budget([500, 500, 500], budget=1_100, max_passage=5_000)

        assert kept[0] == 500
        assert kept[1] == 500
        assert kept[2] == 0

    def test_a_budget_of_zero_keeps_nothing(self) -> None:
        assert fit_to_budget([500], budget=0, max_passage=5_000) == [0]

    def test_the_result_has_one_entry_per_input(self) -> None:
        assert len(fit_to_budget([100, 200, 300, 400], budget=250, max_passage=5_000)) == 4


class TestCitationMarkers:
    def test_markers_are_recognised(self) -> None:
        assert cited_markers("Le loyer est du le 5 [1], sauf preavis [2].") == {1, 2}

    def test_a_bracketed_reference_that_is_not_a_number_is_not_a_marker(self) -> None:
        """A statutory reference quoted from a filing must not read as a citation."""
        assert cited_markers("Voir [Article 12 bis] du bail.") == set()

    def test_the_pattern_is_anchored_to_digits_only(self) -> None:
        assert CITATION_MARKER_PATTERN.findall("[12] [abc] [3]") == ["12", "3"]

    def test_a_marker_beyond_the_supplied_sources_is_unknown(self) -> None:
        assert unknown_markers("Selon le bail [9].", source_count=3) == {9}

    def test_a_valid_marker_is_not_unknown(self) -> None:
        assert unknown_markers("Selon le bail [2].", source_count=3) == set()

    def test_zero_is_unknown_because_markers_are_one_based(self) -> None:
        assert unknown_markers("Selon le bail [0].", source_count=3) == {0}


class TestAnswerInspection:
    def test_the_sentinel_is_recognised(self) -> None:
        assert is_insufficient_evidence(INSUFFICIENT_EVIDENCE_MARKER) is True

    def test_the_sentinel_is_recognised_when_the_model_decorates_it(self) -> None:
        assert is_insufficient_evidence(f'Answer: "{INSUFFICIENT_EVIDENCE_MARKER}"') is True

    def test_the_sentinel_is_recognised_case_insensitively(self) -> None:
        assert is_insufficient_evidence("insufficient_evidence") is True

    def test_an_ordinary_answer_is_not_the_sentinel(self) -> None:
        assert is_insufficient_evidence("Le loyer est payable le 5 [1].") is False

    @pytest.mark.parametrize("value", ["", "   ", "\n\t"])
    def test_an_empty_answer_is_unusable(self, value: str) -> None:
        assert is_usable_answer(value) is False

    def test_any_content_at_all_is_usable(self) -> None:
        """Judging an answer's quality here would be the platform second-guessing the model."""
        assert is_usable_answer("Non.") is True
