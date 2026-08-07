"""Unit tests for :mod:`core.assistant`.

Pure functions: titling, previews, follow-up resolution, and suggestion parsing.
No database, no request, no model — which is the point of the module existing.
"""

from __future__ import annotations

import pytest

from core.assistant import (
    CONTEXT_PREAMBLE,
    FOLLOWUP_QUESTION_MAX_LENGTH,
    PREVIEW_LENGTH,
    ConversationRole,
    derive_title,
    history_questions,
    is_followup_question,
    message_preview,
    normalize_title,
    parse_suggestions,
    resolve_question,
    untitled_conversation,
)
from core.config import settings

# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


class TestConversationRole:
    def test_there_are_exactly_two_roles(self) -> None:
        """A ``system`` role would be the platform's own instructions, which live
        in a versioned prompt template rather than in a row a user reads back."""
        assert {role.value for role in ConversationRole} == {"user", "assistant"}

    def test_it_is_the_persisted_enum_re_exported(self) -> None:
        """Re-exported from :mod:`models.conversation` exactly as
        :mod:`core.roles` re-exports ``UserRole`` — one definition, not two."""
        from models.conversation import ConversationRole as Persisted

        assert ConversationRole is Persisted


# --------------------------------------------------------------------------- #
# Titles
# --------------------------------------------------------------------------- #


class TestNormalizeTitle:
    def test_it_collapses_whitespace(self) -> None:
        assert normalize_title("  Bail   commercial \n") == "Bail commercial"

    def test_it_clips_to_the_configured_ceiling(self) -> None:
        long = "mot " * 200
        assert len(normalize_title(long)) <= settings.ASSISTANT_TITLE_MAX_LENGTH

    def test_it_clips_at_a_word_boundary(self) -> None:
        """A cut mid-word reads as a transcription error in a list of names."""
        long = "obligation " * 40
        assert not normalize_title(long).endswith("obligat")

    def test_it_normalises_the_same_way_a_question_is(self) -> None:
        """Titles sit beside Arabic and French text, and a decomposed form would
        render inconsistently next to the composed one."""
        decomposed = "Bail commercial é"
        assert "é" in normalize_title(decomposed)

    def test_an_empty_title_stays_empty(self) -> None:
        assert normalize_title("   ") == ""


class TestDeriveTitle:
    def test_it_names_a_conversation_after_its_first_question(self) -> None:
        title = derive_title("Quand le loyer est-il payable ?", language="fr")

        assert title == "Quand le loyer est-il payable ?"

    def test_a_long_question_becomes_a_short_title(self) -> None:
        question = "Quelles sont les obligations du locataire " * 10
        title = derive_title(question, language="fr")

        assert len(title) <= settings.ASSISTANT_TITLE_MAX_LENGTH

    def test_an_unusable_question_falls_back_to_the_placeholder(self) -> None:
        assert derive_title("   ", language="fr") == untitled_conversation("fr")

    @pytest.mark.parametrize("language", ["fr", "ar", "en"])
    def test_the_placeholder_exists_in_every_supported_language(self, language: str) -> None:
        assert untitled_conversation(language).strip()

    def test_an_unknown_language_falls_back_to_french(self) -> None:
        """`project-overview.md` names Arabic and French as the platform's
        languages, and the RAG pipeline already falls back to French."""
        assert untitled_conversation("de") == untitled_conversation("fr")

    def test_an_arabic_question_produces_an_arabic_title(self) -> None:
        title = derive_title("ما هي مدة عقد الإيجار؟", language="ar")

        assert "الإيجار" in title


class TestMessagePreview:
    def test_it_clips_to_the_preview_length(self) -> None:
        assert len(message_preview("a " * 400)) <= PREVIEW_LENGTH

    def test_a_short_message_is_unchanged(self) -> None:
        assert message_preview("Le loyer est payable le 1er.") == "Le loyer est payable le 1er."

    def test_it_collapses_newlines(self) -> None:
        """A list row is one line; an embedded newline would break the layout."""
        assert "\n" not in message_preview("Une question\nsur deux lignes")


# --------------------------------------------------------------------------- #
# Follow-up resolution
# --------------------------------------------------------------------------- #


class TestIsFollowupQuestion:
    def test_a_short_question_is_treated_as_a_follow_up(self) -> None:
        assert is_followup_question("Et le délai ?")

    def test_a_long_question_stands_on_its_own(self) -> None:
        assert not is_followup_question("x" * (FOLLOWUP_QUESTION_MAX_LENGTH + 1))

    def test_the_boundary_is_inclusive(self) -> None:
        assert is_followup_question("x" * FOLLOWUP_QUESTION_MAX_LENGTH)


class TestResolveQuestion:
    def test_a_self_contained_question_is_returned_unchanged(self) -> None:
        question = "Quelles sont les obligations du locataire en matière d'entretien courant ?" * 2
        resolved = resolve_question(question, history=["Ancienne question"], language="fr")

        assert resolved.text == question
        assert resolved.turns == 0
        assert not resolved.used_history

    def test_a_follow_up_carries_the_earlier_question(self) -> None:
        resolved = resolve_question(
            "Et quand est-il dû ?",
            history=["Quel est le loyer mensuel ?"],
            language="fr",
        )

        assert "Quel est le loyer mensuel ?" in resolved.text
        assert "Et quand est-il dû ?" in resolved.text
        assert resolved.turns == 1
        assert resolved.used_history

    def test_the_users_own_question_is_last(self) -> None:
        """The model must answer *this* question, with the earlier one as
        context — not the other way round."""
        resolved = resolve_question(
            "Et le délai ?", history=["Quel est le loyer ?"], language="fr"
        )

        assert resolved.text.rstrip().endswith("Et le délai ?")

    def test_history_is_carried_newest_first_under_a_tight_budget(self) -> None:
        resolved = resolve_question(
            "Et après ?",
            history=["Première question très ancienne", "Question la plus récente"],
            language="fr",
            max_characters=len("Question la plus récente") + 2,
        )

        assert "Question la plus récente" in resolved.text
        assert "Première question très ancienne" not in resolved.text
        assert resolved.turns == 1

    def test_carried_history_reads_chronologically(self) -> None:
        resolved = resolve_question(
            "Et ensuite ?",
            history=["Première", "Deuxième"],
            language="fr",
            max_characters=200,
        )

        assert resolved.text.index("Première") < resolved.text.index("Deuxième")

    def test_the_turn_limit_bounds_how_far_back_it_reaches(self) -> None:
        resolved = resolve_question(
            "Et ?",
            history=["Un", "Deux", "Trois", "Quatre", "Cinq"],
            language="fr",
            max_turns=2,
            max_characters=500,
        )

        assert resolved.turns == 2
        assert "Un" not in resolved.text.split(":", 1)[1] or "Quatre" in resolved.text

    def test_no_history_returns_the_question_unchanged(self) -> None:
        resolved = resolve_question("Et le délai ?", history=[], language="fr")

        assert resolved.text == "Et le délai ?"
        assert resolved.turns == 0

    def test_a_zero_turn_budget_disables_resolution(self) -> None:
        """The switch an operator turns off — and it has to be honoured
        completely, not partially."""
        resolved = resolve_question(
            "Et ?", history=["Quelque chose"], language="fr", max_turns=0
        )

        assert resolved.text == "Et ?"
        assert resolved.turns == 0

    def test_a_zero_character_budget_disables_resolution(self) -> None:
        resolved = resolve_question(
            "Et ?", history=["Quelque chose"], language="fr", max_characters=0
        )

        assert resolved.turns == 0

    @pytest.mark.parametrize("language", ["fr", "ar", "en"])
    def test_the_preamble_is_written_in_the_answer_language(self, language: str) -> None:
        resolved = resolve_question(
            "Encore ?", history=["Une question"], language=language, max_characters=200
        )

        assert CONTEXT_PREAMBLE[language] in resolved.text

    def test_an_unknown_language_falls_back_to_french(self) -> None:
        resolved = resolve_question(
            "Encore ?", history=["Une question"], language="de", max_characters=200
        )

        assert CONTEXT_PREAMBLE["fr"] in resolved.text


class TestHistoryQuestions:
    def test_it_keeps_only_the_users_own_turns(self) -> None:
        """An answer is a paragraph: carrying one would dominate both the
        embedding and the prompt, and it is the pipeline's output rather than
        anything the user asked for."""
        pairs = [
            ("user", "Quel est le loyer ?"),
            ("assistant", "Le loyer est de 8000 MAD [1]."),
            ("user", "Et les charges ?"),
        ]

        assert history_questions(pairs) == ["Quel est le loyer ?", "Et les charges ?"]

    def test_it_drops_empty_turns(self) -> None:
        assert history_questions([("user", "   ")]) == []


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


class TestParseSuggestions:
    def test_it_reads_one_question_per_line(self) -> None:
        parsed = parse_suggestions("Première ?\nDeuxième ?\nTroisième ?", limit=3)

        assert parsed == ["Première ?", "Deuxième ?", "Troisième ?"]

    @pytest.mark.parametrize("prefix", ["- ", "* ", "• ", "1. ", "2) "])
    def test_it_strips_bullets_and_numbering(self, prefix: str) -> None:
        assert parse_suggestions(f"{prefix}Quelle est la durée ?", limit=1) == [
            "Quelle est la durée ?"
        ]

    def test_it_strips_surrounding_quotation_marks(self) -> None:
        assert parse_suggestions('"Quelle est la durée ?"', limit=1) == [
            "Quelle est la durée ?"
        ]

    def test_it_caps_the_list(self) -> None:
        parsed = parse_suggestions("A ?\nB ?\nC ?\nD ?\nE ?", limit=2)

        assert len(parsed) == 2

    def test_a_zero_limit_returns_nothing(self) -> None:
        assert parse_suggestions("A ?\nB ?", limit=0) == []

    def test_an_over_long_suggestion_is_dropped_not_clipped(self) -> None:
        """A truncated question changes meaning, and offering one would be worse
        than offering none."""
        parsed = parse_suggestions("court ?\n" + "x" * 300, limit=5, max_length=50)

        assert parsed == ["court ?"]

    def test_duplicates_are_dropped_case_insensitively(self) -> None:
        parsed = parse_suggestions("Quelle durée ?\nQUELLE DURÉE ?", limit=5)

        assert len(parsed) == 1

    def test_it_never_re_suggests_a_question_already_asked(self) -> None:
        """The most common and most annoying failure of this feature."""
        parsed = parse_suggestions(
            "Quel est le loyer ?\nQuelle est la durée ?",
            limit=5,
            exclude=["Quel est le loyer ?"],
        )

        assert parsed == ["Quelle est la durée ?"]

    def test_a_truncated_reply_loses_its_last_line(self) -> None:
        """Regression, and it was found by a **live** run rather than here.

        A reply cut off at the model's output ceiling ends mid-line, so its final
        entry is half a question — short, unique, and otherwise indistinguishable
        from a real one. Offering that as something to send is exactly what the
        "dropped, never clipped" rule exists to prevent, arriving by a route that
        rule cannot see.
        """
        parsed = parse_suggestions(
            "Quelle est la durée du bail ?\nQuel est le domicile du bailleur pour le",
            limit=5,
            truncated=True,
        )

        assert parsed == ["Quelle est la durée du bail ?"]

    def test_a_truncated_reply_with_one_line_yields_nothing(self) -> None:
        """The case the live run actually produced: the ceiling was small enough
        that the *first* suggestion was the one cut off."""
        assert parse_suggestions("Quel est le domicile du bailleur pour le", truncated=True) == []

    def test_a_complete_reply_keeps_its_last_line(self) -> None:
        """The rule must not cost a suggestion when nothing was truncated."""
        parsed = parse_suggestions("Première ?\nDeuxième ?", limit=5, truncated=False)

        assert parsed == ["Première ?", "Deuxième ?"]

    def test_lines_with_no_letters_or_digits_are_dropped(self) -> None:
        assert parse_suggestions("---\n***\nQuelle durée ?", limit=5) == ["Quelle durée ?"]

    def test_blank_output_produces_nothing(self) -> None:
        assert parse_suggestions("\n\n   \n", limit=3) == []

    def test_arabic_suggestions_survive(self) -> None:
        parsed = parse_suggestions("- ما هي مدة العقد؟", limit=1)

        assert parsed == ["ما هي مدة العقد؟"]
