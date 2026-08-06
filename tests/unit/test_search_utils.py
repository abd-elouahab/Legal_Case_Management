"""Unit tests for :mod:`core.search`.

Pure functions, so these need no database, no request, no running Qdrant, and no
downloaded model — which is the whole reason the module exists separately from
the service.

The tests that matter most are the ones about the **query**: normalisation is
what makes an Arabic or French search find the passage containing the word, and
the fingerprint is what lets a search be logged without the query being logged.
"""

from __future__ import annotations

import unicodedata

import pytest

from core.search import (
    MIN_QUERY_LENGTH,
    QUERY_FINGERPRINT_LENGTH,
    SearchFailureCode,
    average_score,
    failure_message,
    is_searchable_query,
    loggable_query,
    normalize_query,
    query_fingerprint,
    round_score,
    top_score,
)


class TestNormalizeQuery:
    def test_whitespace_is_collapsed(self) -> None:
        assert normalize_query("  loyer   commercial \n") == "loyer commercial"

    def test_an_empty_query_normalizes_to_empty(self) -> None:
        assert normalize_query("") == ""
        assert normalize_query("   ") == ""

    def test_control_characters_are_dropped(self) -> None:
        assert normalize_query("bail\x00commercial") == "bailcommercial"

    def test_decomposed_and_composed_french_normalize_identically(self) -> None:
        """The reason NFC is applied at all.

        The indexed passages were NFC-normalised by OCR. A query typed on a
        keyboard that emits the decomposed form would otherwise embed to a
        different vector than the identical word in the document, and the search
        would miss the page containing it.
        """
        composed = "résiliation"
        decomposed = unicodedata.normalize("NFD", composed)

        assert decomposed != composed
        assert normalize_query(decomposed) == normalize_query(composed)

    def test_decomposed_and_composed_arabic_normalize_identically(self) -> None:
        composed = unicodedata.normalize("NFC", "إيجار")
        decomposed = unicodedata.normalize("NFD", composed)

        assert normalize_query(decomposed) == normalize_query(composed)

    def test_the_query_is_not_truncated(self) -> None:
        """Length is a validation concern, not a normalisation one.

        Silently shortening a query would answer a question nobody asked; the
        schema rejects an over-long one instead.
        """
        long_query = "a" * 5_000
        assert len(normalize_query(long_query)) == 5_000


class TestIsSearchableQuery:
    @pytest.mark.parametrize("query", ["contrat de bail", "2024", "AR", "عقد"])
    def test_a_query_with_content_is_searchable(self, query: str) -> None:
        assert is_searchable_query(query) is True

    @pytest.mark.parametrize("query", ["", " ", "?", "!!", "a", "-"])
    def test_a_query_without_content_is_not(self, query: str) -> None:
        assert is_searchable_query(query) is False

    def test_the_minimum_length_is_two(self) -> None:
        assert MIN_QUERY_LENGTH == 2
        assert is_searchable_query("a" * MIN_QUERY_LENGTH) is True
        assert is_searchable_query("a" * (MIN_QUERY_LENGTH - 1)) is False

    def test_digits_count_as_content(self) -> None:
        """A case or article number is a perfectly good thing to search for."""
        assert is_searchable_query("2024") is True


class TestQueryFingerprint:
    def test_the_same_query_produces_the_same_fingerprint(self) -> None:
        assert query_fingerprint("bail commercial") == query_fingerprint("bail commercial")

    def test_normalisation_is_applied_first(self) -> None:
        """So a query typed with stray spacing correlates with the same query."""
        assert query_fingerprint("  bail   commercial  ") == query_fingerprint(
            "bail commercial"
        )

    def test_different_queries_produce_different_fingerprints(self) -> None:
        assert query_fingerprint("bail commercial") != query_fingerprint("bail d'habitation")

    def test_the_fingerprint_contains_no_fragment_of_the_query(self) -> None:
        """The whole point: a log line must not carry what was searched for."""
        query = "divorce Benali contre Alaoui"
        fingerprint = query_fingerprint(query)

        for word in query.split():
            assert word.lower() not in fingerprint.lower()

    def test_the_fingerprint_is_short_and_hexadecimal(self) -> None:
        fingerprint = query_fingerprint("bail")

        assert len(fingerprint) == QUERY_FINGERPRINT_LENGTH
        assert all(character in "0123456789abcdef" for character in fingerprint)

    def test_the_digest_is_salted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two deployments must not produce the same digest for the same query.

        Without the salt, a twelve-character SHA-256 prefix of a common legal
        term is identical everywhere, and a rainbow table of a few thousand
        phrases would undo the whole point of not logging the text.
        """
        from core.config import settings

        first = query_fingerprint("expulsion")
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "a-different-deployment-secret")
        second = query_fingerprint("expulsion")

        assert first != second


class TestLoggableQuery:
    def test_the_query_is_withheld_by_default(self) -> None:
        assert loggable_query("bail commercial") is None

    def test_it_is_returned_when_the_deployment_opts_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "SEARCH_LOG_QUERIES", True)
        assert loggable_query("  bail   commercial ") == "bail commercial"


class TestScores:
    def test_a_score_is_rounded_for_transport(self) -> None:
        assert round_score(0.123456789) == 0.1235

    def test_the_average_of_no_scores_is_none(self) -> None:
        """Undefined, not zero — zero would read as "we return irrelevant results"."""
        assert average_score([]) is None

    def test_the_average_is_rounded(self) -> None:
        assert average_score([0.5, 0.75]) == 0.625

    def test_the_top_of_no_scores_is_none(self) -> None:
        assert top_score([]) is None

    def test_the_top_score_is_the_maximum(self) -> None:
        assert top_score([0.2, 0.91, 0.44]) == 0.91


class TestFailureMessages:
    @pytest.mark.parametrize("code", list(SearchFailureCode))
    def test_every_code_has_a_message(self, code: SearchFailureCode) -> None:
        assert failure_message(code)

    @pytest.mark.parametrize("code", list(SearchFailureCode))
    def test_no_message_blames_the_query(self, code: SearchFailureCode) -> None:
        """Every failure this feature can have is a dependency outage.

        A message implying the query was at fault would send the user to rewrite
        a question that was fine.
        """
        message = failure_message(code)
        assert "query" not in message.lower() or "process this query" in message.lower()

    def test_an_unknown_code_falls_back_to_the_generic_message(self) -> None:
        assert failure_message(SearchFailureCode.UNKNOWN) == "The search could not be completed."
