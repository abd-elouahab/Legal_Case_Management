"""Unit tests for the platform's language vocabulary.

``21-localization.md`` asks for a selection chain, a fallback strategy, RTL, and
graceful handling of an invalid or unsupported locale. All four are properties of
:mod:`core.localization`, which is pure — so they are testable without a database,
a request, a browser, or a translation file.
"""

from __future__ import annotations

import pytest

from core.localization import (
    FALLBACK_LANGUAGE,
    LOCALE_TAGS,
    SUPPORTED_LANGUAGES,
    default_language,
    is_supported,
    locale_tag,
    normalize_language,
    parse_accept_language,
    resolve_language,
    text_direction,
)


class TestSupportedLanguages:
    def test_the_three_the_spec_names_in_its_order(self) -> None:
        """*"English (default), French, Arabic."* The order is the order a language
        selector offers, so it is asserted rather than left to a dict literal."""
        assert SUPPORTED_LANGUAGES == ("en", "fr", "ar")

    def test_english_is_the_shipped_fallback(self) -> None:
        assert FALLBACK_LANGUAGE == "en"

    def test_every_language_has_a_formatting_locale(self) -> None:
        """A language with no BCP-47 tag would format dates and numbers in
        whichever region `Intl` guessed — which for Arabic can mean Eastern Arabic
        numerals, and an unreadable case number for a colleague on the same
        matter."""
        for language in SUPPORTED_LANGUAGES:
            assert language in LOCALE_TAGS
            assert "-" in LOCALE_TAGS[language]


class TestNormalization:
    @pytest.mark.parametrize(
        "value", ["fr", "FR", "  fr  ", "fr-FR", "fr_FR", "FR-ca"]
    )
    def test_every_spelling_of_a_tag_reduces_to_the_code(self, value: str) -> None:
        """A browser, a stored setting, and an API response are three spellings of
        one idea."""
        assert normalize_language(value) == "fr"

    @pytest.mark.parametrize("value", [None, "", "   ", "de", "klingon", "x"])
    def test_anything_unsupported_is_none_rather_than_a_default(
        self, value: str | None
    ) -> None:
        """`None` rather than a default is what makes this composable: a caller
        walking a candidate list has to tell *"did not answer"* apart from *"said
        English"*."""
        assert normalize_language(value) is None

    def test_is_supported_agrees_with_normalization(self) -> None:
        assert is_supported("ar-MA") is True
        assert is_supported("de") is False


class TestResolution:
    def test_the_first_supported_candidate_wins(self) -> None:
        assert resolve_language(None, "de", "ar", "fr") == "ar"

    def test_nothing_resolvable_falls_back_to_the_application_default(self) -> None:
        """The spec's fallback strategy, step 1: *"use the default language"*."""
        assert resolve_language(None, "", "klingon") == default_language()

    def test_no_candidates_at_all_is_the_default(self) -> None:
        assert resolve_language() == default_language()

    def test_an_unsupported_locale_is_skipped_rather_than_refused(self) -> None:
        """*"Handle invalid locale / unsupported language."* A client sending `de`
        gets the default and its request still succeeds — counting that is
        `services.localization_metrics`' job, not this function's."""
        assert resolve_language("de") == default_language()


class TestDirection:
    @pytest.mark.parametrize(
        ("language", "direction"), [("ar", "rtl"), ("fr", "ltr"), ("en", "ltr")]
    )
    def test_arabic_is_right_to_left(self, language: str, direction: str) -> None:
        assert text_direction(language) == direction

    def test_an_unknown_language_reads_left_to_right(self) -> None:
        """The reading that leaves a Latin-script page correct rather than
        mirrored."""
        assert text_direction("de") == "ltr"
        assert text_direction(None) == "ltr"


class TestLocaleTags:
    def test_a_supported_language_gets_its_regional_tag(self) -> None:
        assert locale_tag("ar") == "ar-MA"
        assert locale_tag("fr-FR") == "fr-FR"

    def test_an_unknown_language_formats_as_the_default(self) -> None:
        assert locale_tag("de") == LOCALE_TAGS[default_language()]


class TestAcceptLanguage:
    def test_the_highest_weighted_supported_language_wins(self) -> None:
        assert parse_accept_language("fr;q=0.5, ar;q=0.9") == "ar"

    def test_an_unsupported_first_entry_does_not_end_the_walk(self) -> None:
        """*"de, ar;q=0.8"* resolves to Arabic: the reader named a language this
        platform speaks, and preferring the first *listed* entry over the first
        *supported* one would ignore them."""
        assert parse_accept_language("de, ar;q=0.8") == "ar"

    def test_position_breaks_a_tie(self) -> None:
        assert parse_accept_language("fr, ar") == "fr"

    def test_a_zero_weight_is_a_refusal(self) -> None:
        assert parse_accept_language("fr;q=0, ar") == "ar"

    def test_nothing_supported_is_none_rather_than_a_default(self) -> None:
        """So a caller can fall through to the next candidate rather than being
        handed a default that hides the fact that the browser said nothing
        useful."""
        assert parse_accept_language("de, es;q=0.8") is None
        assert parse_accept_language(None) is None
        assert parse_accept_language("") is None

    def test_a_malformed_quality_is_ignored_rather_than_raising(self) -> None:
        assert parse_accept_language("ar;q=banana, fr") == "fr"
