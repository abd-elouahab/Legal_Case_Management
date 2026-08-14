"""Unit tests for the report-domain utilities.

Pure functions and one body of data: the template catalogue, the lifecycle's
legal moves, the language fallback, title derivation, and the citation
renumbering a report needs because it is made of several independently-numbered
answers.

No database, no request, no vector store, no model — which is the point of the
module being separate from the service, and the reason these can assert the rules
themselves rather than a service's use of them.
"""

from __future__ import annotations

import uuid

import pytest

from core.localization import default_language
from core.reports import (
    MIN_SECTION_CHARACTERS,
    REPORT_TEMPLATE_VERSION,
    SECTION_CATALOG,
    ReportFailureCode,
    ReportFormat,
    can_regenerate,
    can_transition,
    citation_key,
    default_report_title,
    failure_message,
    is_usable_section,
    no_content_message,
    normalize_error_message,
    normalize_report_title,
    references_title,
    remap_markers,
    report_disclaimer,
    resolve_report_language,
    template_for,
)
from models.report import ReportStatus, ReportType

LANGUAGES = ("fr", "ar", "en")


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


class TestTemplates:
    def test_every_report_type_has_a_template(self) -> None:
        """The five ``14-ai-report-agent.md`` requires *"at minimum"*, and a
        member with no template would be a type the API accepts and the agent
        cannot produce."""
        for report_type in ReportType:
            assert template_for(report_type).sections

    def test_the_five_required_types_exist(self) -> None:
        assert {report_type.value for report_type in ReportType} >= {
            "case_summary",
            "hearing_preparation",
            "evidence_summary",
            "chronological_timeline",
            "executive_summary",
        }

    @pytest.mark.parametrize("report_type", list(ReportType))
    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_template_is_labelled_in_every_language(
        self, report_type: ReportType, language: str
    ) -> None:
        """A picker in Arabic that falls back to a French label is a picker that
        looks broken. Both languages `project-overview.md` names, plus English."""
        template = template_for(report_type)

        assert template.title(language).strip()
        assert template.description(language).strip()

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_section_is_labelled_and_asked_in_every_language(
        self, language: str
    ) -> None:
        for section in SECTION_CATALOG.values():
            assert section.title(language).strip()
            assert section.question(language).strip()

    def test_section_questions_fit_the_pipelines_question_budget(self) -> None:
        """A section is asked as a *question*, so an instruction longer than
        ``RAG_QUESTION_MAX_LENGTH`` would be refused by the very endpoint the
        agent hands it to — and only ever at generation time, in a background
        thread, on one template."""
        from core.config import settings

        for section in SECTION_CATALOG.values():
            for language in LANGUAGES:
                assert len(section.question(language)) <= settings.RAG_QUESTION_MAX_LENGTH

    def test_section_keys_are_unique_within_a_template(self) -> None:
        """Two sections sharing a key would collide in storage and in exports."""
        for report_type in ReportType:
            keys = [section.key for section in template_for(report_type).sections]
            assert len(keys) == len(set(keys))

    def test_no_template_asks_a_model_to_write_the_reference_list(self) -> None:
        """References are *derived* from the citations the pipeline attached.
        Asking a model to write them would invite it to invent one, which is
        exactly what the spec forbids."""
        for report_type in ReportType:
            keys = {section.key for section in template_for(report_type).sections}
            assert "references" not in keys

    def test_templates_are_versioned(self) -> None:
        assert REPORT_TEMPLATE_VERSION >= 1


# --------------------------------------------------------------------------- #
# The lifecycle
# --------------------------------------------------------------------------- #


class TestTransitions:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (ReportStatus.PENDING, ReportStatus.PROCESSING),
            (ReportStatus.PROCESSING, ReportStatus.COMPLETED),
            (ReportStatus.PROCESSING, ReportStatus.FAILED),
            (ReportStatus.COMPLETED, ReportStatus.PENDING),
            (ReportStatus.FAILED, ReportStatus.PENDING),
        ],
    )
    def test_legal_moves(self, current: ReportStatus, target: ReportStatus) -> None:
        assert can_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (ReportStatus.PENDING, ReportStatus.COMPLETED),
            (ReportStatus.COMPLETED, ReportStatus.FAILED),
            (ReportStatus.FAILED, ReportStatus.COMPLETED),
        ],
    )
    def test_illegal_moves(self, current: ReportStatus, target: ReportStatus) -> None:
        """A run that reached ``completed`` without passing through
        ``processing`` would make its duration and its start time a lie."""
        assert not can_transition(current, target)

    def test_a_stranded_run_can_be_recovered(self) -> None:
        """``processing`` leads back to ``pending`` so an ungraceful shutdown
        does not leave the one state nothing can leave."""
        assert can_transition(ReportStatus.PROCESSING, ReportStatus.PENDING)

    @pytest.mark.parametrize(
        ("status", "allowed"),
        [
            (ReportStatus.COMPLETED, True),
            (ReportStatus.FAILED, True),
            (ReportStatus.PENDING, False),
            (ReportStatus.PROCESSING, False),
        ],
    )
    def test_only_a_finished_run_may_be_regenerated(
        self, status: ReportStatus, allowed: bool
    ) -> None:
        assert can_regenerate(status) is allowed


# --------------------------------------------------------------------------- #
# Language and titles
# --------------------------------------------------------------------------- #


class TestLanguage:
    @pytest.mark.parametrize("language", LANGUAGES)
    def test_an_explicit_choice_is_honoured(self, language: str) -> None:
        assert resolve_report_language(language) == language

    def test_case_and_whitespace_are_forgiven(self) -> None:
        assert resolve_report_language("  AR  ") == "ar"

    @pytest.mark.parametrize("requested", [None, "", "de", "klingon"])
    def test_anything_else_falls_back_to_the_application_default(
        self, requested: str | None
    ) -> None:
        """There is no question to detect from, so there is nothing to detect —
        only a choice to honour, and the application default to fall back to. The
        requester's own preference is applied one layer up, by `ReportService`,
        because a preference is a fact about an account and `core.reports` has no
        account to read one from."""
        assert resolve_report_language(requested) == default_language()

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_language_has_the_platforms_own_sentences(self, language: str) -> None:
        """The three strings the platform writes rather than generates."""
        assert no_content_message(language).strip()
        assert report_disclaimer(language).strip()
        assert references_title(language).strip()

    def test_an_unknown_language_still_produces_a_sentence(self) -> None:
        assert no_content_message("de").strip()
        assert report_disclaimer("de").strip()


class TestTitles:
    def test_a_default_title_names_the_template_and_the_case_number(self) -> None:
        title = default_report_title(
            ReportType.CASE_SUMMARY, case_number="CASE-2026-0007", language="fr"
        )

        assert "CASE-2026-0007" in title
        assert template_for(ReportType.CASE_SUMMARY).title("fr") in title

    def test_a_default_title_is_written_in_the_reports_language(self) -> None:
        arabic = default_report_title(
            ReportType.EVIDENCE_SUMMARY, case_number="CASE-2026-0001", language="ar"
        )

        assert template_for(ReportType.EVIDENCE_SUMMARY).title("ar") in arabic

    def test_a_title_is_collapsed_and_truncated_rather_than_rejected(self) -> None:
        """A title is a label; refusing to store a report because its heading was
        verbose would lose the expensive thing."""
        assert normalize_report_title("  a   b  ") == "a b"
        assert len(normalize_report_title("x" * 500)) == 255


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #


class TestMarkerRemapping:
    def test_markers_are_rewritten_to_their_global_numbers(self) -> None:
        text = "Le loyer est mensuel [1] et le préavis de trois mois [2]."

        assert remap_markers(text, {1: 4, 2: 5}) == (
            "Le loyer est mensuel [4] et le préavis de trois mois [5]."
        )

    def test_a_swap_is_applied_in_one_pass(self) -> None:
        """Rewriting 1→2 and then 2→1 sequentially would swap a marker back onto
        itself, and the bug would only appear when two sections shared a source."""
        assert remap_markers("[1] puis [2]", {1: 2, 2: 1}) == "[2] puis [1]"

    def test_an_unmapped_marker_is_removed_rather_than_left_dangling(self) -> None:
        """A reference a reader cannot resolve is an invented one from where they
        are sitting."""
        assert "[9]" not in remap_markers("Le loyer [1] et autre chose [9].", {1: 1})

    def test_removing_a_marker_tidies_the_whitespace_it_leaves(self) -> None:
        assert remap_markers("Le loyer est mensuel [9].", {}) == "Le loyer est mensuel."

    def test_arabic_punctuation_is_tidied_too(self) -> None:
        assert remap_markers("الكراء شهري [9]،", {}) == "الكراء شهري،"

    def test_text_without_markers_is_untouched(self) -> None:
        assert remap_markers("Aucune référence ici.", {1: 2}) == "Aucune référence ici."

    def test_a_bracketed_statutory_reference_is_not_a_marker(self) -> None:
        """The pattern is deliberately narrow — digits only — so a citation in the
        source text is not mistaken for one the model made."""
        text = "Voir [Article 12 bis] du dahir."

        assert remap_markers(text, {}) == text


class TestCitationKeys:
    def test_the_same_page_of_the_same_version_is_one_source(self) -> None:
        document = uuid.uuid4()

        assert citation_key(document, 2, 7) == citation_key(document, 2, 7)

    def test_a_different_page_or_version_is_a_different_source(self) -> None:
        document = uuid.uuid4()

        assert citation_key(document, 2, 7) != citation_key(document, 2, 8)
        assert citation_key(document, 2, 7) != citation_key(document, 3, 7)


# --------------------------------------------------------------------------- #
# Failures and validation
# --------------------------------------------------------------------------- #


class TestFailures:
    @pytest.mark.parametrize("code", list(ReportFailureCode))
    def test_every_code_has_a_user_facing_message(self, code: ReportFailureCode) -> None:
        message = failure_message(code)

        assert message.strip()
        # Never an internal detail — the spec's "never expose internal
        # implementation details".
        assert "Traceback" not in message
        assert "Qdrant" not in message

    def test_there_is_no_code_for_a_section_with_no_evidence(self) -> None:
        """A section the documents do not cover is a recorded outcome of a
        *successful* report, not a failure of one."""
        assert not any("evidence" in code.value for code in ReportFailureCode)

    def test_an_error_message_is_reduced_to_one_line(self) -> None:
        assert "\n" not in normalize_error_message("a\nb\nc")
        assert len(normalize_error_message("x" * 900)) == 500


class TestSectionValidation:
    def test_a_fragment_is_not_a_section(self) -> None:
        assert not is_usable_section("Oui.")

    def test_prose_at_the_floor_is_a_section(self) -> None:
        assert is_usable_section("x" * MIN_SECTION_CHARACTERS)

    def test_whitespace_is_not_a_section(self) -> None:
        assert not is_usable_section("   \n  ")


class TestFormats:
    def test_the_two_required_formats_exist(self) -> None:
        assert {export_format.value for export_format in ReportFormat} == {"markdown", "pdf"}
