"""Live validation of the Report Generation Agent against the **real** model.

Everything else in this feature's suite drives a scripted provider,
deliberately — see ``tests/ai/test_rag_live.py`` for the full argument, which
applies here unchanged. What that leaves uncovered is the one question only a
real model can answer, and for a *report* it is not quite the pipeline's:

    **do the section instructions actually produce sections?**

``core/reports.py`` calls those instructions domain data rather than prompts, and
they are — but the distinction is about *where a change is reviewed*, not about
whether the strings work. A section question that a model answers with a
paraphrase of the question, or with two words, or by declining because it read
"Write the Parties section" as an instruction it was not given the material for,
would produce a report that is structurally perfect and useless. Nothing in a
hermetic suite would notice: the double returns whatever string the test wrote.

So this module generates **one real report, once**, and asserts the properties
that make it a report rather than seven answers: every section has prose, the
prose is in the requested language, at least one section is grounded, the
citations resolve, and the whole thing exports.

**One report is four model calls** (the executive summary is the shortest
template), which matters: Gemini's free tier allows **5 requests per minute and
20 per day** for ``gemini-2.5-flash``, so a single case-summary run would be
seven of the twenty. The pacing fixture handles the per-minute ceiling; nothing
can handle the per-day one except running this module sparingly or using a paid
key. A 429 here says something about the *account*, never about the platform.

Run it with both switches::

    LLM_API_KEY=...  RUN_LIVE_AI_TESTS=1  pytest tests/ai/test_reports_live.py -q

The embedder is the **real BAAI/bge-m3**, so a passage reaching the model got
there by being semantically near the section's question — which is the whole
premise a grounded section rests on.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

import pytest

from core.config import settings
from core.reports import ReportFormat, template_for
from models.case import Case
from models.document import Document, DocumentCategory
from models.report import ReportStatus, ReportType
from models.user import User, UserRole
from schemas.report import ReportCreate

pytestmark = pytest.mark.skipif(
    not (settings.LLM_API_KEY and os.getenv("RUN_LIVE_AI_TESTS")),
    reason="live AI checks need LLM_API_KEY and RUN_LIVE_AI_TESTS=1",
)

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeDocument = Callable[..., Document]

#: A case file with enough in it to report on: parties, dates, money, and a
#: procedural posture. Deliberately richer than the pipeline's live fixture,
#: because a report asks a dozen *different* questions of the same corpus and a
#: single clause would leave most of them honestly uncovered.
FRENCH_FILING = (
    "TRIBUNAL DE COMMERCE DE CASABLANCA — Dossier n° 2026/1147. "
    "Demanderesse : Madame Amina Benali, commerçante, représentée par Maître Idrissi. "
    "Défenderesse : la société ATLAS IMMOBILIER SARL, représentée par Maître Alaoui. "
    "Requête déposée le 3 février 2026. Audience de mise en état fixée au 12 mars 2026. "
    "OBJET : résiliation d'un bail commercial et paiement d'arriérés. "
    "La demanderesse expose qu'elle occupe le local depuis le 1er janvier 2021 en vertu "
    "d'un bail commercial, que le loyer mensuel de 8 500 dirhams est payable d'avance le "
    "premier jour de chaque mois, et que la bailleresse a cessé d'assurer les réparations "
    "structurelles depuis juin 2025 malgré trois mises en demeure. "
    "La défenderesse conclut au rejet et soutient que trois échéances demeurent impayées."
)

EVIDENCE_PAGE = (
    "PIÈCE N° 4 — Contrat de bail commercial du 1er janvier 2021, signé par les deux "
    "parties. Article 4 : le loyer mensuel est payable d'avance le premier jour de chaque "
    "mois. Article 9 : les réparations structurelles incombent au bailleur. "
    "PIÈCE N° 7 — Mise en demeure recommandée du 14 juillet 2025, avec accusé de réception "
    "signé le 16 juillet 2025. "
    "PIÈCE N° 11 — Relevé bancaire attestant le paiement des loyers de janvier à mai 2025."
)


@pytest.fixture(autouse=True)
def pace() -> Any:
    """Keep the module inside the provider's requests-per-minute allowance.

    A rate limit is a fact about the *account*, not about the platform, and a
    suite that reports it as a failure teaches its reader to ignore red. The same
    fixture ``test_rag_live.py`` uses, and for the same reasons — except that
    here one *test* is several calls, so the pause is per test and the report's
    own sections are paced by the provider's retry policy if they need to be.
    """
    import time

    delay = float(os.getenv("LIVE_AI_PACE_SECONDS", "13"))
    if delay > 0:
        time.sleep(delay)


@pytest.fixture(scope="session")
def embedder() -> Any:
    """The **real** BAAI/bge-m3, overriding the suite's deterministic double.

    Session-scoped because the model is roughly two gigabytes and its load is
    slow; it is stateless once loaded, so sharing it is safe as well as
    necessary.
    """
    from services.embedding import get_embedder

    real = get_embedder()
    if not real.is_available():  # pragma: no cover - environment dependent
        pytest.skip("BAAI/bge-m3 is not available on this host")
    return real


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(email="live-report-lawyer@example.com", role=UserRole.LAWYER)


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User) -> Case:
    return make_case(assigned_lawyer_id=lawyer.id)


@pytest.fixture
def indexed_case(
    legal_case: Case,
    make_document: MakeDocument,
    make_ocr_result: Any,
    indexing_service: Any,
) -> Case:
    """A case file pushed through the **real** indexing pipeline."""
    filing = make_document(
        case_id=legal_case.id,
        original_filename="requete-introductive.pdf",
        category=DocumentCategory.PLEADING,
    )
    indexing_service.schedule_for_ocr_result(
        make_ocr_result(document_id=filing.id, pages=[FRENCH_FILING])
    )

    exhibits = make_document(
        case_id=legal_case.id,
        original_filename="bordereau-de-pieces.pdf",
        category=DocumentCategory.EVIDENCE,
    )
    indexing_service.schedule_for_ocr_result(
        make_ocr_result(document_id=exhibits.id, pages=[EVIDENCE_PAGE])
    )
    return legal_case


@pytest.fixture
def live_report_service(db_session: Any, search_service: Any, prompt_library: Any, rag_metrics: Any) -> Any:
    """The agent with the **real** provider and the shipped pipeline.

    Only the provider differs from the hermetic fixture: the search service, the
    access policy, the ranker, the graph, the templates, and the section
    instructions are all the application's own, so what runs here is the
    production agent.

    The queue is inline, so the report is generated before the call returns —
    exactly as the hermetic fixture does, and for the same reason: the assertion
    is about the *outcome*, and a test that waits for a thread is a test that
    will eventually be flaky.
    """
    from repositories.case import CaseRepository
    from repositories.report import ReportRepository
    from repositories.timeline import TimelineRepository
    from services.job_queue import NullJobQueue
    from services.llm import GeminiProvider
    from services.rag import RagService
    from services.report import ReportJob, ReportService
    from services.timeline import TimelineService

    rag = RagService(search_service, prompt_library, GeminiProvider(), metrics=rag_metrics)

    def build(queue: Any) -> ReportService:
        return ReportService(
            ReportRepository(db_session),
            CaseRepository(db_session),
            rag,
            queue,
            timeline=TimelineService(TimelineRepository(db_session), CaseRepository(db_session)),
        )

    class InlineQueue:
        def enqueue(self, job: ReportJob) -> None:
            build(NullJobQueue(name="reports")).process(job)

    return build(InlineQueue())


def test_a_real_model_produces_a_usable_report(
    live_report_service: Any, indexed_case: Case, lawyer: User
) -> None:
    """One report, generated once, asserted from every angle that matters.

    **One test rather than eight**, and the reason is quota rather than style:
    the report below is four model calls, ``db_session`` is function-scoped so a
    module-scoped report is not available to share, and eight tests would be
    thirty-two calls — the free tier's entire daily allowance spent on eight
    assertions about the same document. Splitting them would make the module
    unrunnable on the key most contributors have.

    Every assertion is written against a property the platform makes
    *structural* — a length floor, a script, a resolvable marker, a file
    signature — and never against particular wording. A language model is not a
    pure function, so an assertion on phrasing would be a flake waiting for a
    model upgrade.
    """
    report = live_report_service.request_report(
        ReportCreate(
            case_id=indexed_case.id,
            report_type=ReportType.EXECUTIVE_SUMMARY,
            language="fr",
        ),
        actor=lawyer,
    )

    # The whole point of the module: a failure here means the *section
    # instructions* did not work against the live model. Every other failure mode
    # is covered hermetically.
    assert report.status is ReportStatus.COMPLETED, report.error_message
    assert len(report.sections) == template_for(ReportType.EXECUTIVE_SUMMARY).section_count

    # Prose rather than a fragment. The platform enforces a floor; this checks
    # the model clears it comfortably rather than by one character.
    for section in report.sections:
        assert len(str(section["content"]).strip()) >= 40, section["key"]

    # The corpus plainly supports an overview and key findings. A run where
    # *nothing* is grounded would mean retrieval or the instructions are not
    # reaching the material — and the platform would have failed the run, which
    # is why this is an assertion about a completed one.
    assert (report.grounded_sections or 0) >= 1

    # Written in the requested language, asserted against the *script*.
    prose = " ".join(str(section["content"]) for section in report.sections)
    assert not re.search(r"[؀-ۿ]", prose), "Arabic script in a French report"

    # "Reports should never invent citations." The platform removes a marker it
    # cannot place; this proves the renumbering across four independently
    # numbered answers actually lands.
    available = {citation["marker"] for citation in report.citations}
    for section in report.sections:
        for match in re.finditer(r"\[(\d+)\]", str(section["content"])):
            assert int(match.group(1)) in available

    for citation in report.citations:
        assert citation["document_name"] in {
            "requete-introductive.pdf",
            "bordereau-de-pieces.pdf",
        }
        assert citation["page_number"] >= 1

    # Both export formats, against real generated prose — which is what an
    # accent, an ampersand, or a bracketed statutory reference in a model's
    # output can break and a fixture's tidy sentence cannot.
    markdown = live_report_service.export(report.id, ReportFormat.MARKDOWN, actor=lawyer)
    pdf = live_report_service.export(report.id, ReportFormat.PDF, actor=lawyer)
    assert markdown.content.decode("utf-8")
    assert pdf.content.startswith(b"%PDF")

    # Provenance, so an evaluation run can group two months of reports.
    assert report.provider == "gemini"
    assert report.model
    assert report.prompt_name == settings.RAG_PROMPT_TEMPLATE
