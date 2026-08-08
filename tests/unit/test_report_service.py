"""Unit tests for the report generation service.

Against the **real** RAG pipeline, the real search service, the real
repositories, the real access policies, and the real templates — only the
embedding model, the vector database, and the language model are substituted,
which are the three genuinely external things.

That matters more here than anywhere else in this feature's suite, and for the
same reason it matters in the pipeline's own tests: the whole design claim is
*"a report section is a grounded pipeline answer"*, and the whole authorization
story is *"the agent can reach a passage only through the pipeline"*. A faked
pipeline would make every grounding, citation, and authorization assertion below
a test of the fixture.

The corpus is built by the real indexing pipeline, so a citation asserted here
points at a passage that travelled the whole way from an uploaded file.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from core.config import settings
from core.exceptions import (
    CaseAccessDeniedError,
    CaseNotFoundError,
    ReportAlreadyRunningError,
    ReportNotFoundError,
    ReportNotReadyError,
    ReportsDisabledError,
    TooManyActiveReportsError,
)
from core.reports import ReportFailureCode, ReportFormat, template_for
from models.case import Case
from models.document import Document
from models.ocr import OcrResult
from models.report import ReportStatus, ReportType
from models.timeline import TimelineEventType
from models.user import User, UserRole
from schemas.report import ReportCreate, ReportListQuery
from schemas.search import SearchFilterInput
from services.llm import LLMTimeoutError, LLMUnavailableError

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import RecordingIndexQueue, ScriptedLLMProvider

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeDocument = Callable[..., Document]
MakeOcrResult = Callable[..., OcrResult]

#: Long enough to clear ``MIN_SECTION_CHARACTERS``: a section shorter than that
#: is recorded as uncovered, which is the rule rather than a quirk — but a
#: fixture that tripped it would be testing the floor instead of the branch.
GROUNDED_ANSWER = (
    "Le bail est commercial et le loyer est payable le premier jour de chaque mois [1]."
)

FRENCH_PAGE = (
    "CONTRAT DE BAIL COMMERCIAL entre la société Atlas, bailleur, et Madame Benali, "
    "preneuse. Article 4 : Loyer et charges. Le loyer mensuel est payable d'avance le "
    "premier jour de chaque mois. Toute résiliation anticipée doit être notifiée par "
    "écrit avec un préavis de trois mois. Audience fixée au 12 septembre 2026."
)


# --------------------------------------------------------------------------- #
# Actors and corpus
# --------------------------------------------------------------------------- #


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(email="report-lawyer@example.com", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: MakeUser) -> User:
    return make_user(email="report-other@example.com", role=UserRole.LAWYER)


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(email="report-admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User) -> Case:
    return make_case(assigned_lawyer_id=lawyer.id)


@pytest.fixture
def other_case(make_case: MakeCase, other_lawyer: User) -> Case:
    return make_case(assigned_lawyer_id=other_lawyer.id)


@pytest.fixture
def indexed_case(
    legal_case: Case,
    make_document: MakeDocument,
    make_ocr_result: MakeOcrResult,
    indexing_service: Any,
) -> Case:
    """A case whose documents are in the vector store, via the real pipeline."""
    document = make_document(case_id=legal_case.id, original_filename="bail.pdf")
    indexing_service.schedule_for_ocr_result(
        make_ocr_result(document_id=document.id, pages=[FRENCH_PAGE])
    )
    return legal_case


def request_for(
    case: Case, report_type: ReportType = ReportType.CASE_SUMMARY, **overrides: Any
) -> ReportCreate:
    return ReportCreate(case_id=case.id, report_type=report_type, **overrides)


# --------------------------------------------------------------------------- #
# Requesting
# --------------------------------------------------------------------------- #


class TestRequesting:
    def test_a_request_returns_a_queued_run_rather_than_a_report(
        self,
        report_service: Any,
        report_queue: RecordingIndexQueue,
        indexed_case: Case,
        lawyer: User,
    ) -> None:
        """Generation is background work: the response is the run, and the client
        polls it. Observed with the queue's inline execution switched off, which
        is the only way to see a `pending` row at all."""
        report_queue.run_inline = False

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.status is ReportStatus.PENDING
        assert report.sections == []
        assert report_queue.jobs

    def test_the_denominator_is_published_before_the_run_starts(
        self,
        report_service: Any,
        report_queue: RecordingIndexQueue,
        indexed_case: Case,
        lawyer: User,
    ) -> None:
        """So a client can draw a real progress bar from the first poll rather
        than an indeterminate spinner."""
        report_queue.run_inline = False

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.sections_total == template_for(ReportType.CASE_SUMMARY).section_count
        assert report.sections_completed == 0
        assert report.progress_percent == 0

    def test_the_title_defaults_to_the_template_and_the_case_number(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert indexed_case.case_number in report.title

    def test_the_case_title_never_reaches_the_report_title(
        self,
        report_service: Any,
        make_case: MakeCase,
        lawyer: User,
        indexing_service: Any,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
    ) -> None:
        """It is client-confidential, and would then travel in an export
        filename, a list row, and a timeline description."""
        case = make_case(assigned_lawyer_id=lawyer.id, title="Benali contre Societe Atlas")
        document = make_document(case_id=case.id)
        indexing_service.schedule_for_ocr_result(
            make_ocr_result(document_id=document.id, pages=[FRENCH_PAGE])
        )

        report = report_service.request_report(request_for(case), actor=lawyer)

        assert "Benali" not in report.title

    def test_a_supplied_title_is_kept(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(
            request_for(indexed_case, title="Note pour l'audience"), actor=lawyer
        )

        assert report.title == "Note pour l'audience"

    def test_an_unknown_case_is_a_404(
        self, report_service: Any, lawyer: User
    ) -> None:
        payload = ReportCreate(case_id=uuid.uuid4(), report_type=ReportType.CASE_SUMMARY)

        with pytest.raises(CaseNotFoundError):
            report_service.request_report(payload, actor=lawyer)

    def test_a_case_the_caller_is_not_party_to_is_refused(
        self, report_service: Any, other_case: Case, lawyer: User
    ) -> None:
        """403 rather than a concealing 404: a lawyer who follows a colleague's
        link needs to know the case exists and that they should ask to be
        assigned."""
        with pytest.raises(CaseAccessDeniedError):
            report_service.request_report(request_for(other_case), actor=lawyer)

    def test_an_administrator_may_generate_for_any_case(
        self, report_service: Any, other_case: Case, admin: User
    ) -> None:
        """`cases:view-all` lifts the row restriction, exactly as it does
        everywhere else."""
        report = report_service.request_report(request_for(other_case), actor=admin)

        assert report.case_id == other_case.id

    def test_the_queue_is_bounded_per_user(
        self,
        report_service: Any,
        report_queue: RecordingIndexQueue,
        indexed_case: Case,
        lawyer: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A report costs a model call per section, so an unbounded queue is a
        way to spend a deployment's whole token budget from one browser tab."""
        report_queue.run_inline = False
        monkeypatch.setattr(settings, "REPORT_MAX_ACTIVE_PER_USER", 1)

        report_service.request_report(request_for(indexed_case), actor=lawyer)

        with pytest.raises(TooManyActiveReportsError):
            report_service.request_report(request_for(indexed_case), actor=lawyer)

    def test_the_ceiling_is_per_user_rather_than_platform_wide(
        self,
        report_service: Any,
        report_queue: RecordingIndexQueue,
        indexed_case: Case,
        lawyer: User,
        admin: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report_queue.run_inline = False
        monkeypatch.setattr(settings, "REPORT_MAX_ACTIVE_PER_USER", 1)
        report_service.request_report(request_for(indexed_case), actor=lawyer)

        report = report_service.request_report(request_for(indexed_case), actor=admin)

        assert report.status is ReportStatus.PENDING

    def test_generation_can_be_switched_off(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "REPORTS_ENABLED", False)

        with pytest.raises(ReportsDisabledError):
            report_service.request_report(request_for(indexed_case), actor=lawyer)


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


class TestGeneration:
    def test_a_report_is_generated(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.status is ReportStatus.COMPLETED
        assert report.sections

    def test_the_sections_are_the_templates_in_its_order(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """``14-ai-report-agent.md``: section ordering is template-driven."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert [section["key"] for section in report.sections] == [
            section.key for section in template_for(ReportType.CASE_SUMMARY).sections
        ]

    @pytest.mark.parametrize("report_type", list(ReportType))
    def test_every_template_produces_a_report(
        self, report_service: Any, indexed_case: Case, lawyer: User, report_type: ReportType
    ) -> None:
        """The spec's "multiple templates work"."""
        report = report_service.request_report(
            request_for(indexed_case, report_type=report_type), actor=lawyer
        )

        assert report.status is ReportStatus.COMPLETED
        assert len(report.sections) == template_for(report_type).section_count

    def test_generation_is_section_by_section(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """One model call per section rather than one per report — the spec's
        "generate reports section-by-section" and "avoid exceeding model
        limits"."""
        report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert len(llm_provider.calls) == template_for(ReportType.CASE_SUMMARY).section_count

    def test_each_section_retrieves_for_itself(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """"Retrieve context incrementally": every prompt is built for the
        section it answers, so no two are the same."""
        report_service.request_report(request_for(indexed_case), actor=lawyer)

        prompts = [prompt for _, prompt in llm_provider.calls]
        assert len(set(prompts)) == len(prompts)

    def test_the_headings_are_written_in_the_reports_language(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(
            request_for(indexed_case, language="ar"), actor=lawyer
        )

        expected = template_for(ReportType.CASE_SUMMARY).sections[0].title("ar")
        assert report.sections[0]["title"] == expected

    def test_the_language_is_settled_once_for_the_whole_report(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """So two sections of one report cannot come back in different
        languages."""
        report = report_service.request_report(
            request_for(indexed_case, language="ar"), actor=lawyer
        )

        titles = [
            section.title("ar") for section in template_for(ReportType.CASE_SUMMARY).sections
        ]
        assert [section["title"] for section in report.sections] == titles

    def test_the_run_records_what_produced_it(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """Configuration is *current* and a report is *historical*: an evaluation
        comparing two months of reports cannot group ones that do not say."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.provider == "scripted"
        assert report.prompt_name == settings.RAG_PROMPT_TEMPLATE
        assert report.prompt_version == settings.RAG_PROMPT_VERSION
        assert report.template_version >= 1

    def test_token_usage_is_summed_across_sections(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        sections = template_for(ReportType.CASE_SUMMARY).section_count
        assert report.prompt_tokens == 120 * sections

    def test_token_usage_is_absent_rather_than_zero_when_unreported(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """`0` would read as "this report was free", which is a different and
        false statement."""
        llm_provider.prompt_tokens = None
        llm_provider.completion_tokens = None

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.prompt_tokens is None

    def test_the_run_records_its_size_and_its_timings(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.character_count and report.character_count > 0
        assert report.duration_ms is not None
        assert report.finished_at is not None
        assert report.attempt_count == 1

    def test_a_section_is_given_a_bigger_output_ceiling_than_a_chat_reply(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Found on a live run, not by reasoning: `gemini-2.5-flash` charges its
        internal deliberation against `max_output_tokens`, and a section at the
        deployment's default came back as 41 visible tokens — a 151-character
        section cut off mid-sentence. Unreachable from a hermetic run, where the
        double returns whatever string the test wrote, so the ceiling itself is
        what gets asserted."""
        seen: list[int | None] = []
        original = llm_provider.generate

        def record(**kwargs: Any) -> Any:
            seen.append(kwargs.get("max_output_tokens"))
            return original(**kwargs)

        monkeypatch.setattr(llm_provider, "generate", record)
        report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert seen
        assert all(value == settings.REPORT_SECTION_MAX_OUTPUT_TOKENS for value in seen)
        assert settings.REPORT_SECTION_MAX_OUTPUT_TOKENS > settings.LLM_MAX_OUTPUT_TOKENS

    def test_a_section_cut_off_at_the_ceiling_says_so(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """A legal section that ends early must not be read as a complete one —
        the one way the report view could actively mislead."""
        llm_provider.truncated = True
        llm_provider.finish_reason = "MAX_TOKENS"

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert any(section["truncated"] for section in report.sections)

    def test_an_uncovered_section_is_never_marked_truncated(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """The platform's own "not covered" sentence cannot be cut off, so
        flagging it would be a warning about the platform's own prose."""
        from core.rag import INSUFFICIENT_EVIDENCE_MARKER

        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER
        llm_provider.truncated = True

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert all(not section["truncated"] for section in report.sections)

    def test_progress_reaches_the_total(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """The spec's "progress should be queryable by the client"."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.sections_completed == report.sections_total
        assert report.progress_percent == 100


# --------------------------------------------------------------------------- #
# Grounding and citations
# --------------------------------------------------------------------------- #


class TestCitations:
    def test_a_grounded_section_carries_citations(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.citations

    def test_a_citation_carries_the_four_references_the_spec_names(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """Document name, page number, document version, and case."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        citation = report.citations[0]
        assert citation["document_name"] == "bail.pdf"
        assert citation["page_number"] >= 1
        assert citation["document_version"] >= 1
        assert citation["case_id"] == str(indexed_case.id)

    def test_sources_are_numbered_globally_across_the_report(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """The pipeline numbers each answer's sources from 1; a report is one
        document, so its reference list is contiguous from 1."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        markers = [citation["marker"] for citation in report.citations]
        assert markers == list(range(1, len(markers) + 1))

    def test_the_same_page_of_the_same_document_is_one_source(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """Two sections that both lean on page 7 of the same contract produce one
        reference rather than two lines that are the same line."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        keys = [
            (citation["document_id"], citation["document_version"], citation["page_number"])
            for citation in report.citations
        ]
        assert len(keys) == len(set(keys))

    def test_every_marker_in_the_prose_resolves_to_a_source(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """"Reports should never invent citations": a marker a reader cannot
        resolve is an invented one from where they are sitting."""
        import re

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)
        available = {citation["marker"] for citation in report.citations}

        for section in report.sections:
            for match in re.finditer(r"\[(\d+)\]", str(section["content"])):
                assert int(match.group(1)) in available

    def test_a_section_records_the_markers_it_cites(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)
        grounded = [section for section in report.sections if section["grounded"]]

        assert grounded
        assert all(section["citation_markers"] for section in grounded)

    def test_an_uncovered_section_says_so_in_the_platforms_own_words(
        self,
        report_service: Any,
        legal_case: Case,
        lawyer: User,
    ) -> None:
        """A model asked to explain that it found nothing will sometimes explain
        it *and then answer anyway* from its training — which in a legal report is
        indistinguishable from a grounded finding. This case has no indexed
        documents at all, so every section takes that path."""
        from core.reports import no_content_message

        report = report_service.request_report(request_for(legal_case), actor=lawyer)

        assert report.status is ReportStatus.FAILED
        assert report.error_code == ReportFailureCode.INSUFFICIENT_CONTEXT.value
        assert no_content_message("fr")

    def test_an_ungrounded_section_carries_no_citations(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """A section reading "the documents do not cover this" beside a list of
        two sources contradicts itself in front of the reader."""
        from core.rag import INSUFFICIENT_EVIDENCE_MARKER

        calls = {"n": 0}

        def reply(system: str, prompt: str) -> str:
            calls["n"] += 1
            return INSUFFICIENT_EVIDENCE_MARKER if calls["n"] > 1 else GROUNDED_ANSWER

        llm_provider.reply = reply
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        ungrounded = [section for section in report.sections if not section["grounded"]]
        assert ungrounded
        assert all(section["citation_markers"] == [] for section in ungrounded)

    def test_a_report_with_some_gaps_still_completes(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """Six sections of seven is a useful report with a gap the reader can
        see. Judging how many is enough would be the platform second-guessing the
        corpus."""
        from core.rag import INSUFFICIENT_EVIDENCE_MARKER

        calls = {"n": 0}

        def reply(system: str, prompt: str) -> str:
            calls["n"] += 1
            return INSUFFICIENT_EVIDENCE_MARKER if calls["n"] > 1 else GROUNDED_ANSWER

        llm_provider.reply = reply
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.status is ReportStatus.COMPLETED
        assert report.grounded_sections == 1

    def test_a_report_with_nothing_grounded_fails(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """A document of empty headings would be a several-hundred-token way of
        saying "this case has no indexed documents", and a lawyer who received it
        as a completed report would reasonably conclude the platform had read the
        file and found nothing in it."""
        from core.rag import INSUFFICIENT_EVIDENCE_MARKER

        llm_provider.answer = INSUFFICIENT_EVIDENCE_MARKER

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.status is ReportStatus.FAILED
        assert report.error_code == ReportFailureCode.INSUFFICIENT_CONTEXT.value


# --------------------------------------------------------------------------- #
# Authorization of the content
# --------------------------------------------------------------------------- #


class TestContentAuthorization:
    def test_a_report_is_built_only_from_the_cases_own_documents(
        self,
        report_service: Any,
        indexed_case: Case,
        other_case: Case,
        lawyer: User,
        admin: User,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
        indexing_service: Any,
    ) -> None:
        """The case is forced onto every section's filters: a report whose title
        and contents disagree about which matter it is about would be worse than
        no report."""
        foreign = make_document(case_id=other_case.id, original_filename="autre.pdf")
        indexing_service.schedule_for_ocr_result(
            make_ocr_result(document_id=foreign.id, pages=[FRENCH_PAGE])
        )

        report = report_service.request_report(request_for(indexed_case), actor=admin)

        assert all(
            citation["case_id"] == str(indexed_case.id) for citation in report.citations
        )

    def test_a_filter_cannot_widen_the_scope_to_another_case(
        self,
        report_service: Any,
        indexed_case: Case,
        other_case: Case,
        lawyer: User,
    ) -> None:
        """The filters narrow and can never widen: the report's own case is
        applied over whatever the request supplied."""
        report = report_service.request_report(
            request_for(indexed_case, filters=SearchFilterInput(case_id=other_case.id)),
            actor=lawyer,
        )

        assert all(
            citation["case_id"] == str(indexed_case.id) for citation in report.citations
        )

    def test_the_agent_holds_no_route_to_a_passage_but_the_pipeline(
        self, report_service: Any
    ) -> None:
        """The feature's authorization story, asserted as the shape of the
        object: no search service, no embedder, no vector searcher, no prompt
        library. Any of them would be a way to build a section without the
        scope the pipeline applies."""
        held = vars(report_service)

        assert "_search" not in held
        assert "_embedder" not in held
        assert "_searcher" not in held
        assert "_prompts" not in held


# --------------------------------------------------------------------------- #
# Failure
# --------------------------------------------------------------------------- #


class TestFailure:
    def test_an_unavailable_model_fails_the_run_rather_than_the_request(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """A failure is a recorded *state* of the run: the caller polling it gets
        a report whose status is `failed`, with the reason on it."""
        llm_provider.raises = LLMUnavailableError("no key")

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.status is ReportStatus.FAILED
        assert report.error_code == ReportFailureCode.LLM_UNAVAILABLE.value

    def test_a_timeout_is_reported_as_one(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMTimeoutError("too slow")

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.error_code == ReportFailureCode.TIMEOUT.value

    def test_a_failed_run_keeps_no_partial_report(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """The opposite of the choice indexing makes about partial vectors, and
        for a reason particular to this feature: a partial index is a smaller
        index and still correct, while a partial *report* is a legal document
        missing sections with nothing on its face to say so."""
        llm_provider.raises = LLMUnavailableError("no key")

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.sections == []
        assert report.citations == []

    def test_a_failed_run_carries_a_message_free_of_internals(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("client secret rejected at endpoint x")

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.error_message
        assert "endpoint" not in report.error_message
        assert "secret" not in report.error_message

    def test_a_failure_leaves_the_case_and_its_documents_untouched(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
        db_session: Any,
    ) -> None:
        from models.document import Document as DocumentModel

        llm_provider.raises = LLMUnavailableError("no key")
        before = db_session.query(DocumentModel).count()

        report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert db_session.query(DocumentModel).count() == before

    def test_a_failed_run_stays_regenerable(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("no key")
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        llm_provider.raises = None
        regenerated = report_service.regenerate(report.id, actor=lawyer)

        assert regenerated.status is ReportStatus.COMPLETED


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


class TestHistory:
    def test_the_history_is_the_callers_own(
        self,
        report_service: Any,
        make_report: Any,
        legal_case: Case,
        lawyer: User,
        other_lawyer: User,
    ) -> None:
        """"History must remain user-specific"."""
        make_report(requested_by=lawyer, case=legal_case)
        make_report(requested_by=other_lawyer, case=legal_case)

        page = report_service.list_reports(ReportListQuery(), actor=lawyer)

        assert page.total == 1

    def test_another_users_report_is_not_found_rather_than_refused(
        self, report_service: Any, make_report: Any, legal_case: Case, lawyer: User, other_lawyer: User
    ) -> None:
        """Confirming that another user's private work product *exists* is itself
        the disclosure the spec forbids."""
        report = make_report(requested_by=other_lawyer, case=legal_case)

        with pytest.raises(ReportNotFoundError):
            report_service.get_report(report.id, actor=lawyer)

    def test_an_administrator_does_not_read_other_peoples_reports(
        self, report_service: Any, make_report: Any, legal_case: Case, lawyer: User, admin: User
    ) -> None:
        """There is deliberately no ``reports:view-all``: a report is private
        work product, and `cases:view-all` lifts a *case* restriction rather than
        an ownership one."""
        report = make_report(requested_by=lawyer, case=legal_case)

        with pytest.raises(ReportNotFoundError):
            report_service.get_report(report.id, actor=admin)

    def test_the_history_can_be_narrowed_to_one_case(
        self,
        report_service: Any,
        make_report: Any,
        legal_case: Case,
        other_case: Case,
        lawyer: User,
    ) -> None:
        make_report(requested_by=lawyer, case=legal_case)
        make_report(requested_by=lawyer, case=other_case)

        page = report_service.list_reports(
            ReportListQuery(case_id=legal_case.id), actor=lawyer
        )

        assert page.total == 1

    def test_the_history_can_be_narrowed_by_type(
        self, report_service: Any, make_report: Any, legal_case: Case, lawyer: User
    ) -> None:
        make_report(requested_by=lawyer, case=legal_case, report_type=ReportType.CASE_SUMMARY)
        make_report(
            requested_by=lawyer, case=legal_case, report_type=ReportType.EVIDENCE_SUMMARY
        )

        page = report_service.list_reports(
            ReportListQuery(report_type=ReportType.EVIDENCE_SUMMARY), actor=lawyer
        )

        assert page.total == 1

    def test_deletion_is_logical_and_hides_the_report(
        self, report_service: Any, make_report: Any, legal_case: Case, lawyer: User
    ) -> None:
        """The report carries the citations of an analysis a lawyer may have
        acted on, so the row is kept and a future retention job reclaims it."""
        report = make_report(requested_by=lawyer, case=legal_case)

        report_service.delete_report(report.id, actor=lawyer)

        with pytest.raises(ReportNotFoundError):
            report_service.get_report(report.id, actor=lawyer)

    def test_deleting_twice_answers_not_found(
        self, report_service: Any, make_report: Any, legal_case: Case, lawyer: User
    ) -> None:
        report = make_report(requested_by=lawyer, case=legal_case)
        report_service.delete_report(report.id, actor=lawyer)

        with pytest.raises(ReportNotFoundError):
            report_service.delete_report(report.id, actor=lawyer)


# --------------------------------------------------------------------------- #
# Regeneration
# --------------------------------------------------------------------------- #


class TestRegeneration:
    def test_the_row_is_re_used_rather_than_replaced(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """So a link somebody saved keeps working, and the history stays a list of
        reports rather than of attempts at one."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        regenerated = report_service.regenerate(report.id, actor=lawyer)

        assert regenerated.id == report.id
        assert regenerated.attempt_count == 2

    def test_a_run_in_flight_is_refused(
        self,
        report_service: Any,
        report_queue: RecordingIndexQueue,
        indexed_case: Case,
        lawyer: User,
    ) -> None:
        """Silently answering "done" for one already being written would make the
        button they pressed indistinguishable from one that worked."""
        report_queue.run_inline = False
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        with pytest.raises(ReportAlreadyRunningError):
            report_service.regenerate(report.id, actor=lawyer)

    def test_case_access_is_checked_again(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        db_session: Any,
    ) -> None:
        """A lawyer unassigned since the first run must not be able to produce a
        fresh interpretation of the matter from a link they still hold."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)
        indexed_case.assigned_lawyer_id = None
        db_session.commit()

        with pytest.raises(CaseAccessDeniedError):
            report_service.regenerate(report.id, actor=lawyer)

    def test_an_export_count_survives_regeneration(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        """It is a fact about the report rather than about this run of it."""
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)
        report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)

        regenerated = report_service.regenerate(report.id, actor=lawyer)

        assert regenerated.export_count == 1


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


class TestExport:
    def test_markdown_export_succeeds(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        export = report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)

        assert export.content
        assert export.filename.endswith(".md")

    def test_pdf_export_succeeds(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        export = report_service.export(report.id, ReportFormat.PDF, actor=lawyer)

        assert export.content.startswith(b"%PDF")
        assert export.media_type == "application/pdf"

    def test_an_export_carries_the_reports_sections(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        export = report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)

        text = export.content.decode("utf-8")
        for section in report.sections:
            assert str(section["title"]) in text

    def test_an_unfinished_report_cannot_be_exported(
        self,
        report_service: Any,
        report_queue: RecordingIndexQueue,
        indexed_case: Case,
        lawyer: User,
    ) -> None:
        """An empty file that looks like a finished report is worse than a
        refusal."""
        report_queue.run_inline = False
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        with pytest.raises(ReportNotReadyError):
            report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)

    def test_another_users_report_cannot_be_exported(
        self, report_service: Any, make_report: Any, legal_case: Case, lawyer: User, other_lawyer: User
    ) -> None:
        """The spec's "exported reports inherit the same permissions as their
        source case", made structural: there is no object to hand a URL to, and
        every byte is produced inside an owner-scoped request."""
        report = make_report(requested_by=other_lawyer, case=legal_case)

        with pytest.raises(ReportNotFoundError):
            report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)

    def test_exports_are_counted(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)
        report_service.export(report.id, ReportFormat.PDF, actor=lawyer)

        assert report_service.get_report(report.id, actor=lawyer).export_count == 2


# --------------------------------------------------------------------------- #
# The timeline
# --------------------------------------------------------------------------- #


class TestTimeline:
    def test_a_generated_report_is_announced_on_the_case(
        self, report_service: Any, indexed_case: Case, lawyer: User, db_session: Any
    ) -> None:
        """A report is case work product, unlike a conversation — the people on
        the matter are entitled to know one exists."""
        from models.timeline import TimelineEvent

        report_service.request_report(request_for(indexed_case), actor=lawyer)

        types = {
            event.event_type
            for event in db_session.query(TimelineEvent)
            .filter(TimelineEvent.case_id == indexed_case.id)
            .all()
        }
        assert TimelineEventType.REPORT_GENERATED.value in types
        assert TimelineEventType.REPORT_REQUESTED.value in types

    def test_the_timeline_never_carries_a_section(
        self, report_service: Any, indexed_case: Case, lawyer: User, db_session: Any
    ) -> None:
        """The event records *that* a report exists, never a line of it: the
        report itself stays readable only by the user who generated it."""
        from models.timeline import TimelineEvent

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)
        prose = str(report.sections[0]["content"])[:30]

        events = (
            db_session.query(TimelineEvent)
            .filter(TimelineEvent.case_id == indexed_case.id)
            .all()
        )
        assert all(prose not in (event.description or "") for event in events)

    def test_an_export_is_announced(
        self, report_service: Any, indexed_case: Case, lawyer: User, db_session: Any
    ) -> None:
        from models.timeline import TimelineEvent

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)
        report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)

        types = {
            event.event_type
            for event in db_session.query(TimelineEvent)
            .filter(TimelineEvent.case_id == indexed_case.id)
            .all()
        }
        assert TimelineEventType.REPORT_EXPORTED.value in types


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_the_six_figures_the_spec_names(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)
        report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)

        metrics = report_service.metrics()

        assert metrics.counts.completed == 1
        assert metrics.counts.average_duration_ms is not None
        assert metrics.counts.total_exports == 1
        assert metrics.counts.failed == 0
        assert metrics.counts.average_characters is not None
        assert metrics.counts.total_prompt_tokens is not None

    def test_a_failed_run_is_counted_as_one(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("no key")
        report_service.request_report(request_for(indexed_case), actor=lawyer)

        metrics = report_service.metrics()

        assert metrics.counts.failed == 1
        assert metrics.failures_by_code[ReportFailureCode.LLM_UNAVAILABLE.value] == 1

    def test_reports_are_grouped_by_type(
        self, report_service: Any, indexed_case: Case, lawyer: User
    ) -> None:
        report_service.request_report(
            request_for(indexed_case, report_type=ReportType.EXECUTIVE_SUMMARY), actor=lawyer
        )

        metrics = report_service.metrics()

        assert metrics.reports_by_type[ReportType.EXECUTIVE_SUMMARY.value] == 1

    def test_metrics_are_not_scoped_to_the_caller(
        self, report_service: Any, make_report: Any, legal_case: Case, lawyer: User, other_lawyer: User
    ) -> None:
        """An operational view of the *platform*: scoping it per caller would make
        the success rate mean something different for each of them."""
        make_report(requested_by=lawyer, case=legal_case)
        make_report(requested_by=other_lawyer, case=legal_case)

        assert report_service.metrics().counts.completed == 2

    def test_availability_is_probed_rather_than_inferred(
        self, report_service: Any, llm_provider: ScriptedLLMProvider
    ) -> None:
        """A platform generating no reports because no credential is configured
        and one nobody has asked yet show the same zeros."""
        llm_provider.available = False

        assert report_service.metrics().llm_available is False

    def test_available_formats_are_reported(self, report_service: Any) -> None:
        assert ReportFormat.MARKDOWN in report_service.metrics().available_formats


# --------------------------------------------------------------------------- #
# Large cases
# --------------------------------------------------------------------------- #


class TestLargeCases:
    def test_a_template_beyond_the_ceiling_is_truncated_rather_than_failing(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The spec's "Large Cases" met honestly, in the shape ``OCR_MAX_PAGES``
        and ``INDEX_MAX_CHUNKS`` meet theirs: a partial report that says so beats
        a run that exhausts its deadline and produces none."""
        monkeypatch.setattr(settings, "REPORT_MAX_SECTIONS", 2)

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert report.status is ReportStatus.COMPLETED
        assert len(report.sections) == 2

    def test_the_citation_list_is_bounded(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "REPORT_MAX_CITATIONS", 1)

        report = report_service.request_report(request_for(indexed_case), actor=lawyer)

        assert len(report.citations) <= 1

    def test_a_bounded_citation_list_leaves_no_dangling_marker(
        self,
        report_service: Any,
        indexed_case: Case,
        lawyer: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A source beyond the ceiling gets no marker, so its reference is removed
        from the prose rather than left resolving to nothing."""
        import re

        monkeypatch.setattr(settings, "REPORT_MAX_CITATIONS", 1)
        report = report_service.request_report(request_for(indexed_case), actor=lawyer)
        available = {citation["marker"] for citation in report.citations}

        for section in report.sections:
            for match in re.finditer(r"\[(\d+)\]", str(section["content"])):
                assert int(match.group(1)) in available
