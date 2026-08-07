"""Unit tests for :mod:`services.suggestions`.

Against the **real** prompt library — the templates are files under source
control with no network and no model behind them, so substituting one would make
every claim about the shipped prompt an assertion against a fake. Only the
provider is a double.

The load-bearing assertions are the two the spec actually cares about: that an
ungrounded answer is never followed by suggestions, and that no failure here ever
costs the answer it would have followed.
"""

from __future__ import annotations

import uuid

import pytest

from core.config import settings
from schemas.rag import RagCitationRead
from services.llm import LLMTimeoutError, LLMUnavailableError
from services.prompts import JinjaPromptLibrary
from services.suggestions import (
    LlmFollowUpSuggester,
    NullFollowUpSuggester,
    get_follow_up_suggester,
)


@pytest.fixture
def citations() -> list[RagCitationRead]:
    return [
        RagCitationRead(
            marker=1,
            document_id=uuid.uuid4(),
            document_name="bail-commercial.pdf",
            document_version=1,
            page_number=3,
            case_id=uuid.uuid4(),
            score=0.72,
            excerpt="Le loyer est payable le premier jour de chaque mois.",
            referenced=True,
        )
    ]


@pytest.fixture
def suggester(llm_provider):  # type: ignore[no-untyped-def]
    return LlmFollowUpSuggester(JinjaPromptLibrary(), llm_provider)


ANSWER = "Le loyer mensuel est payable d'avance le premier jour de chaque mois [1]."


class TestSuggest:
    def test_it_returns_the_models_questions(self, suggester, llm_provider, citations) -> None:  # type: ignore[no-untyped-def]
        llm_provider.answer = (
            "Quelle est la durée du bail ?\n"
            "Quelle pénalité s'applique en cas de retard ?\n"
            "Qui prend en charge les réparations ?"
        )

        suggestions = suggester.suggest(
            question="Quel est le loyer ?", answer=ANSWER, citations=citations, language="fr"
        )

        assert suggestions == [
            "Quelle est la durée du bail ?",
            "Quelle pénalité s'applique en cas de retard ?",
            "Qui prend en charge les réparations ?",
        ]

    def test_an_ungrounded_answer_costs_no_model_call(
        self, suggester, llm_provider
    ) -> None:  # type: ignore[no-untyped-def]
        """Suggestions must never invent unsupported facts, and an answer that
        found no supporting document supports no follow-up. It is also the
        cheapest correct behaviour: no call is made at all."""
        suggestions = suggester.suggest(
            question="Quel est le loyer ?",
            answer="Je n'ai trouvé aucun document justificatif.",
            citations=[],
            language="fr",
        )

        assert suggestions == []
        assert llm_provider.calls == []

    def test_an_empty_answer_costs_no_model_call(
        self, suggester, llm_provider, citations
    ) -> None:  # type: ignore[no-untyped-def]
        assert (
            suggester.suggest(question="Q ?", answer="   ", citations=citations, language="fr")
            == []
        )
        assert llm_provider.calls == []

    def test_the_question_already_asked_is_never_re_suggested(
        self, suggester, llm_provider, citations
    ) -> None:  # type: ignore[no-untyped-def]
        llm_provider.answer = "Quel est le loyer ?\nQuelle est la durée du bail ?"

        suggestions = suggester.suggest(
            question="Quel est le loyer ?", answer=ANSWER, citations=citations, language="fr"
        )

        assert suggestions == ["Quelle est la durée du bail ?"]

    def test_the_documents_are_named_and_the_passages_are_not(
        self, suggester, llm_provider, citations
    ) -> None:  # type: ignore[no-untyped-def]
        """The answer was already built from those passages, so a question
        grounded in the answer is grounded in them — and sending the full context
        twice would double the cost of every exchange for three short questions."""
        suggester.suggest(
            question="Quel est le loyer ?", answer=ANSWER, citations=citations, language="fr"
        )

        _, prompt = llm_provider.calls[-1]
        assert "bail-commercial.pdf" in prompt
        assert citations[0].excerpt not in prompt

    def test_the_answer_and_the_question_reach_the_model(
        self, suggester, llm_provider, citations
    ) -> None:  # type: ignore[no-untyped-def]
        suggester.suggest(
            question="Quel est le loyer ?", answer=ANSWER, citations=citations, language="fr"
        )

        _, prompt = llm_provider.calls[-1]
        assert "Quel est le loyer ?" in prompt
        assert ANSWER in prompt

    @pytest.mark.parametrize(
        ("language", "expected"), [("fr", "French"), ("ar", "Arabic"), ("en", "English")]
    )
    def test_the_answer_language_is_named_in_the_prompt(
        self, suggester, llm_provider, citations, language: str, expected: str
    ) -> None:  # type: ignore[no-untyped-def]
        suggester.suggest(
            question="Q ?", answer=ANSWER, citations=citations, language=language
        )

        system, _ = llm_provider.calls[-1]
        assert expected.upper() in system


class TestFailure:
    def test_a_provider_failure_returns_nothing_rather_than_raising(
        self, suggester, llm_provider, citations
    ) -> None:  # type: ignore[no-untyped-def]
        """An answer the user is already waiting for must never be lost to the
        convenience that follows it."""
        llm_provider.raises = LLMUnavailableError("no key")

        assert (
            suggester.suggest(
                question="Q ?", answer=ANSWER, citations=citations, language="fr"
            )
            == []
        )

    def test_a_timeout_returns_nothing_rather_than_raising(
        self, suggester, llm_provider, citations
    ) -> None:  # type: ignore[no-untyped-def]
        llm_provider.raises = LLMTimeoutError("too slow")

        assert (
            suggester.suggest(
                question="Q ?", answer=ANSWER, citations=citations, language="fr"
            )
            == []
        )

    def test_a_missing_template_returns_nothing_rather_than_raising(
        self, llm_provider, citations, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(
            settings, "ASSISTANT_SUGGESTION_PROMPT_TEMPLATE", "assistant/nonexistent"
        )
        suggester = LlmFollowUpSuggester(JinjaPromptLibrary(), llm_provider)

        assert (
            suggester.suggest(
                question="Q ?", answer=ANSWER, citations=citations, language="fr"
            )
            == []
        )

    def test_a_reply_cut_off_at_the_output_ceiling_loses_its_last_suggestion(
        self, suggester, llm_provider, citations
    ) -> None:  # type: ignore[no-untyped-def]
        """The provider's own truncation report reaches the parser.

        Regression from a live run: gemini-2.5-flash charges its internal
        thinking against the output budget, so a ceiling sized for three short
        questions produced one cut off mid-word — which is short, unique, and
        indistinguishable from a real suggestion by every other rule.
        """
        llm_provider.answer = (
            "Quelle est la durée du bail ?\nQuel est le domicile du bailleur pour le"
        )
        llm_provider.truncated = True
        llm_provider.finish_reason = "MAX_TOKENS"

        suggestions = suggester.suggest(
            question="Q ?", answer=ANSWER, citations=citations, language="fr"
        )

        assert suggestions == ["Quelle est la durée du bail ?"]

    def test_an_unparseable_reply_returns_nothing(
        self, suggester, llm_provider, citations
    ) -> None:  # type: ignore[no-untyped-def]
        llm_provider.answer = "   \n\n  "

        assert (
            suggester.suggest(
                question="Q ?", answer=ANSWER, citations=citations, language="fr"
            )
            == []
        )


class TestConfiguration:
    def test_the_switch_turns_it_off_completely(
        self, suggester, llm_provider, citations, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(settings, "ASSISTANT_SUGGESTIONS_ENABLED", False)

        assert (
            suggester.suggest(
                question="Q ?", answer=ANSWER, citations=citations, language="fr"
            )
            == []
        )
        assert llm_provider.calls == []

    def test_a_zero_count_turns_it_off_completely(
        self, suggester, llm_provider, citations, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(settings, "ASSISTANT_SUGGESTION_COUNT", 0)

        assert (
            suggester.suggest(
                question="Q ?", answer=ANSWER, citations=citations, language="fr"
            )
            == []
        )
        assert llm_provider.calls == []

    def test_the_factory_returns_the_null_backend_when_disabled(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(settings, "ASSISTANT_SUGGESTIONS_ENABLED", False)

        assert isinstance(get_follow_up_suggester(), NullFollowUpSuggester)

    def test_the_factory_returns_the_llm_backend_when_enabled(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(settings, "ASSISTANT_SUGGESTIONS_ENABLED", True)

        assert isinstance(get_follow_up_suggester(), LlmFollowUpSuggester)

    def test_availability_is_false_when_the_switch_is_off(
        self, suggester, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The monitoring view must be able to tell "turned off" apart from
        "turned on and silently producing nothing"."""
        monkeypatch.setattr(settings, "ASSISTANT_SUGGESTIONS_ENABLED", False)

        assert not suggester.is_available()

    def test_availability_is_false_without_a_provider(
        self, suggester, llm_provider, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(settings, "ASSISTANT_SUGGESTIONS_ENABLED", True)
        llm_provider.available = False

        assert not suggester.is_available()

    def test_the_null_backend_suggests_nothing(self, citations) -> None:  # type: ignore[no-untyped-def]
        backend = NullFollowUpSuggester()

        assert not backend.is_available()
        assert (
            backend.suggest(question="Q ?", answer=ANSWER, citations=citations, language="fr")
            == []
        )


class TestScope:
    def test_the_module_reaches_no_document(self) -> None:
        """It is handed one exchange that has already passed through the
        pipeline's authorization, and holds no way to reach anything else."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "apps"
            / "api"
            / "services"
            / "suggestions.py"
        ).read_text(encoding="utf-8")

        for forbidden in (
            "from services.search import",
            "from services.vector_search import",
            "from repositories",
            "from db.session import",
        ):
            assert forbidden not in source
