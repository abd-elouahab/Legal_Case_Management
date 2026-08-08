"""Integration tests for the AI report generation API.

Exercise the endpoints over real HTTP: the request contract, progress polling,
the report a client renders, authorization (401 vs 403 for every route and every
role, plus the per-case scope and the per-user history scope), export, the
failure envelopes, and the monitoring view.

The corpus is built by the *real* indexing pipeline: a document is uploaded,
extracted, chunked, embedded, and stored, and only then reported on. So a
citation returned here points at a passage that travelled the whole way from an
uploaded file, which is what makes these tests about the platform rather than
about a fixture.

The service-level rules are unit-tested in ``tests/unit/test_report_service.py``;
what these add is the wire contract — status codes, the response shape a client
renders, error envelopes, and three assurances that can only be checked from the
outside:

* **no field on the wire carries a prompt, a vector, a chunk number, or a passage
  the caller may not read**;
* a report belonging to another user is **404 while an inaccessible case is
  403**, and the asymmetry is the feature's whole privacy posture;
* **the API exposes no compliance, translation, or scheduling surface** —
  ``14-ai-report-agent.md`` puts all three out of scope, and this is where a
  stray endpoint would show up.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from core.reports import ReportFailureCode, template_for
from models.case import Case
from models.document import Document
from models.ocr import OcrResult
from models.report import ReportType
from models.user import User, UserRole
from services.llm import LLMUnavailableError

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import RecordingIndexQueue, ScriptedLLMProvider

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
REPORTS_URL = f"{settings.API_V1_PREFIX}/reports"
TEMPLATES_URL = f"{REPORTS_URL}/templates"
METRICS_URL = f"{REPORTS_URL}/metrics"

FRENCH_PAGE = (
    "CONTRAT DE BAIL COMMERCIAL entre la société Atlas, bailleur, et Madame Benali, "
    "preneuse. Article 4 : Loyer et charges. Le loyer mensuel est payable d'avance le "
    "premier jour de chaque mois. Toute résiliation anticipée doit être notifiée par "
    "écrit avec un préavis de trois mois. Audience fixée au 12 septembre 2026."
)

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeDocument = Callable[..., Document]
MakeOcrResult = Callable[..., OcrResult]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == status.HTTP_200_OK, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def generate(
    client: TestClient,
    token: str,
    case: Case,
    report_type: ReportType = ReportType.CASE_SUMMARY,
    **body: Any,
) -> Any:
    return client.post(
        REPORTS_URL,
        json={"case_id": str(case.id), "report_type": report_type.value, **body},
        headers=bearer(token),
    )


# --------------------------------------------------------------------------- #
# Actors and corpus
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(
        email="reports-admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR
    )


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(
        email="reports-lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER
    )


@pytest.fixture
def outsider(make_user: MakeUser) -> User:
    return make_user(
        email="reports-outsider@example.com", password=PASSWORD, role=UserRole.LAWYER
    )


@pytest.fixture
def court(make_user: MakeUser) -> User:
    return make_user(
        email="reports-court@example.com",
        password=PASSWORD,
        role=UserRole.COURT_REPRESENTATIVE,
    )


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User, court: User) -> Case:
    return make_case(
        assigned_lawyer_id=lawyer.id, assigned_court_representative_id=court.id
    )


@pytest.fixture
def other_case(make_case: MakeCase, outsider: User) -> Case:
    return make_case(assigned_lawyer_id=outsider.id)


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


# --------------------------------------------------------------------------- #
# Authentication and capability
# --------------------------------------------------------------------------- #


class TestAuthentication:
    def test_generating_requires_authentication(
        self, api_client: TestClient, legal_case: Case
    ) -> None:
        response = api_client.post(
            REPORTS_URL,
            json={"case_id": str(legal_case.id), "report_type": "case_summary"},
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize("url", [REPORTS_URL, TEMPLATES_URL, METRICS_URL])
    def test_every_read_requires_authentication(
        self, api_client: TestClient, url: str
    ) -> None:
        assert api_client.get(url).status_code == status.HTTP_401_UNAUTHORIZED

    def test_a_lawyer_may_generate(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        response = generate(api_client, token_for(api_client, lawyer.email), indexed_case)

        assert response.status_code == status.HTTP_201_CREATED

    def test_a_court_representative_may_not_generate(
        self, api_client: TestClient, court: User, indexed_case: Case
    ) -> None:
        """The role descriptions in `project-overview.md` and `architecture.md`
        give court representatives no AI capabilities at all, and
        `ai:generate-report` has been withheld from them since Authorization
        shipped."""
        response = generate(api_client, token_for(api_client, court.email), indexed_case)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_court_representative_may_not_read_reports(
        self, api_client: TestClient, court: User
    ) -> None:
        response = api_client.get(REPORTS_URL, headers=bearer(token_for(api_client, court.email)))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_lawyer_may_not_read_platform_metrics(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        """``reports:monitor`` is administrative and is not scoped to a case —
        exactly like ``ocr:monitor``, ``indexing:monitor``, ``search:monitor``,
        and ``ai:monitor``."""
        response = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, lawyer.email))
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_an_administrator_may_read_platform_metrics(
        self, api_client: TestClient, admin: User
    ) -> None:
        response = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        )

        assert response.status_code == status.HTTP_200_OK

    def test_a_denial_names_neither_permission_nor_role(
        self, api_client: TestClient, court: User, indexed_case: Case
    ) -> None:
        """A 403 body must not hand an attacker a map of the capability model."""
        body = generate(
            api_client, token_for(api_client, court.email), indexed_case
        ).json()

        assert "reports:generate" not in body["message"]
        assert "lawyer" not in body["message"].lower()


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


class TestTemplates:
    def test_it_lists_every_report_type(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        """Served so a client never hard-codes the catalogue: a sixth template is
        a server-side entry, and the picker follows without a frontend change."""
        body = api_client.get(
            TEMPLATES_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()

        assert {template["report_type"] for template in body} == {
            report_type.value for report_type in ReportType
        }

    def test_each_template_advertises_its_sections_in_order(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        body = api_client.get(
            TEMPLATES_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()
        summary = next(
            template for template in body if template["report_type"] == "case_summary"
        )

        assert [section["key"] for section in summary["sections"]] == [
            section.key for section in template_for(ReportType.CASE_SUMMARY).sections
        ]
        assert summary["section_count"] == len(summary["sections"])

    def test_the_catalogue_is_labelled_in_the_requested_language(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        """A picker offering "Synthèse de l'affaire" that produces a report headed
        "Case Summary" is a small inconsistency that reads as a bug."""
        body = api_client.get(
            f"{TEMPLATES_URL}?language=ar",
            headers=bearer(token_for(api_client, lawyer.email)),
        ).json()
        summary = next(
            template for template in body if template["report_type"] == "case_summary"
        )

        assert summary["title"] == template_for(ReportType.CASE_SUMMARY).title("ar")

    def test_templates_is_not_parsed_as_a_report_identifier(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        """FastAPI resolves routes in declaration order; registered after
        ``/{report_id}`` this URL would answer 422."""
        response = api_client.get(
            TEMPLATES_URL, headers=bearer(token_for(api_client, lawyer.email))
        )

        assert response.status_code == status.HTTP_200_OK


# --------------------------------------------------------------------------- #
# Generating
# --------------------------------------------------------------------------- #


class TestGenerating:
    def test_a_request_answers_201_with_the_run(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        report_queue: RecordingIndexQueue,
    ) -> None:
        """201 rather than 202: a report *resource* is created, it has a URL, and
        201 with that resource in the body is what lets a client render the new
        row without a second request."""
        report_queue.run_inline = False

        response = generate(api_client, token_for(api_client, lawyer.email), indexed_case)

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert body["status"] == "pending"
        assert body["is_active"] is True

    def test_the_run_carries_a_progress_denominator_immediately(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        report_queue: RecordingIndexQueue,
    ) -> None:
        report_queue.run_inline = False

        body = generate(
            api_client, token_for(api_client, lawyer.email), indexed_case
        ).json()

        assert body["sections_total"] == template_for(ReportType.CASE_SUMMARY).section_count
        assert body["progress_percent"] == 0

    def test_a_generated_report_is_readable(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        body = api_client.get(f"{REPORTS_URL}/{report_id}", headers=bearer(token)).json()

        assert body["status"] == "completed"
        assert body["is_active"] is False
        assert body["progress_percent"] == 100
        assert body["sections"]

    def test_the_detail_response_is_the_progress_endpoint_too(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        """One endpoint rather than two, because a client polling a separate
        status URL would need a third request the moment the run finished."""
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        body = api_client.get(f"{REPORTS_URL}/{report_id}", headers=bearer(token)).json()

        assert body["sections_completed"] == body["sections_total"]

    def test_a_section_carries_its_heading_prose_and_grounding(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        section = api_client.get(
            f"{REPORTS_URL}/{report_id}", headers=bearer(token)
        ).json()["sections"][0]

        assert section["key"]
        assert section["title"]
        assert section["content"]
        assert isinstance(section["grounded"], bool)

    def test_the_report_carries_its_disclaimer(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        """`ai-workflow-rules.md`: AI features are assistants, not
        decision-makers. Sent with the report rather than added by each client,
        so it survives every surface and every export."""
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        body = api_client.get(f"{REPORTS_URL}/{report_id}", headers=bearer(token)).json()

        assert body["disclaimer"]
        assert body["references_title"]

    def test_citations_resolve_and_carry_the_four_references(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        body = api_client.get(f"{REPORTS_URL}/{report_id}", headers=bearer(token)).json()
        markers = {citation["marker"] for citation in body["citations"]}

        assert body["citations"]
        for citation in body["citations"]:
            assert citation["document_name"]
            assert citation["page_number"] >= 1
            assert citation["document_version"] >= 1
            assert citation["case_id"] == str(indexed_case.id)
        for section in body["sections"]:
            for match in re.finditer(r"\[(\d+)\]", section["content"]):
                assert int(match.group(1)) in markers

    @pytest.mark.parametrize("report_type", list(ReportType))
    def test_every_report_type_can_be_generated_over_http(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        report_type: ReportType,
    ) -> None:
        response = generate(
            api_client, token_for(api_client, lawyer.email), indexed_case, report_type
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["report_type"] == report_type.value

    def test_an_unknown_case_answers_404(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        response = api_client.post(
            REPORTS_URL,
            json={"case_id": str(uuid.uuid4()), "report_type": "case_summary"},
            headers=bearer(token_for(api_client, lawyer.email)),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_an_inaccessible_case_answers_403(
        self, api_client: TestClient, lawyer: User, other_case: Case
    ) -> None:
        """403 rather than a concealing 404 — the *opposite* of what a report
        belonging to another user gets, and the asymmetry is deliberate."""
        response = generate(api_client, token_for(api_client, lawyer.email), other_case)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_the_report_endpoint_answers_like_the_case_endpoint(
        self, api_client: TestClient, lawyer: User, other_case: Case
    ) -> None:
        """So report generation cannot be used to probe for matters."""
        token = token_for(api_client, lawyer.email)

        case_response = api_client.get(
            f"{settings.API_V1_PREFIX}/cases/{other_case.id}", headers=bearer(token)
        )
        report_response = generate(api_client, token, other_case)

        assert report_response.status_code == case_response.status_code

    def test_an_unknown_report_type_answers_422(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        response = api_client.post(
            REPORTS_URL,
            json={"case_id": str(indexed_case.id), "report_type": "compliance_review"},
            headers=bearer(token_for(api_client, lawyer.email)),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_the_queue_is_bounded_per_user_with_a_429(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        report_queue: RecordingIndexQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """429 rather than 409: nothing about the request is wrong, and the
        identical request succeeds once one of the caller's own runs finishes."""
        report_queue.run_inline = False
        monkeypatch.setattr(settings, "REPORT_MAX_ACTIVE_PER_USER", 1)
        token = token_for(api_client, lawyer.email)
        generate(api_client, token, indexed_case)

        response = generate(api_client, token, indexed_case)

        assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS

    def test_generation_can_be_switched_off(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "REPORTS_ENABLED", False)

        response = generate(api_client, token_for(api_client, lawyer.email), indexed_case)

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "reports_disabled"


# --------------------------------------------------------------------------- #
# Failure
# --------------------------------------------------------------------------- #


class TestFailure:
    def test_a_failed_run_is_a_created_report_rather_than_a_failed_request(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        """A failure is a recorded *state*: answering 5xx would say the platform
        is broken when what happened is that one case file had nothing to build a
        report from."""
        llm_provider.raises = LLMUnavailableError("no key")

        response = generate(api_client, token_for(api_client, lawyer.email), indexed_case)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["status"] == "failed"

    def test_a_failed_run_names_its_cause_machine_readably(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        llm_provider: ScriptedLLMProvider,
    ) -> None:
        llm_provider.raises = LLMUnavailableError("no key")

        body = generate(
            api_client, token_for(api_client, lawyer.email), indexed_case
        ).json()

        assert body["error_code"] == ReportFailureCode.LLM_UNAVAILABLE.value
        assert body["error_message"]

    def test_a_case_with_no_indexed_documents_reports_insufficient_context(
        self, api_client: TestClient, lawyer: User, legal_case: Case
    ) -> None:
        """A document of empty headings would look like a working report that
        says nothing."""
        body = generate(
            api_client, token_for(api_client, lawyer.email), legal_case
        ).json()

        assert body["error_code"] == ReportFailureCode.INSUFFICIENT_CONTEXT.value


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


class TestHistory:
    def test_the_list_is_the_callers_own(
        self,
        api_client: TestClient,
        make_report: Any,
        legal_case: Case,
        lawyer: User,
        outsider: User,
    ) -> None:
        make_report(requested_by=lawyer, case=legal_case)
        make_report(requested_by=outsider, case=legal_case)

        body = api_client.get(
            REPORTS_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()

        assert body["total_records"] == 1

    def test_another_users_report_answers_404_rather_than_403(
        self,
        api_client: TestClient,
        make_report: Any,
        legal_case: Case,
        lawyer: User,
        outsider: User,
    ) -> None:
        """Confirming that another user's private work product *exists* is itself
        the disclosure this feature must not make."""
        report = make_report(requested_by=outsider, case=legal_case)

        response = api_client.get(
            f"{REPORTS_URL}/{report.id}",
            headers=bearer(token_for(api_client, lawyer.email)),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_an_administrator_does_not_read_other_peoples_reports(
        self,
        api_client: TestClient,
        make_report: Any,
        legal_case: Case,
        lawyer: User,
        admin: User,
    ) -> None:
        """There is deliberately no ``reports:view-all``."""
        report = make_report(requested_by=lawyer, case=legal_case)

        response = api_client.get(
            f"{REPORTS_URL}/{report.id}",
            headers=bearer(token_for(api_client, admin.email)),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_the_list_can_be_pinned_to_one_case(
        self,
        api_client: TestClient,
        make_report: Any,
        legal_case: Case,
        other_case: Case,
        lawyer: User,
    ) -> None:
        """What the case workspace sends."""
        make_report(requested_by=lawyer, case=legal_case)
        make_report(requested_by=lawyer, case=other_case)

        body = api_client.get(
            f"{REPORTS_URL}?case_id={legal_case.id}",
            headers=bearer(token_for(api_client, lawyer.email)),
        ).json()

        assert body["total_records"] == 1

    def test_a_list_row_carries_no_sections(
        self, api_client: TestClient, make_report: Any, legal_case: Case, lawyer: User
    ) -> None:
        make_report(requested_by=lawyer, case=legal_case)

        row = api_client.get(
            REPORTS_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()["items"][0]

        assert "sections" not in row
        assert "citations" not in row

    def test_a_report_can_be_deleted(
        self, api_client: TestClient, make_report: Any, legal_case: Case, lawyer: User
    ) -> None:
        report = make_report(requested_by=lawyer, case=legal_case)
        token = token_for(api_client, lawyer.email)

        deleted = api_client.delete(f"{REPORTS_URL}/{report.id}", headers=bearer(token))

        assert deleted.status_code == status.HTTP_204_NO_CONTENT
        assert (
            api_client.get(f"{REPORTS_URL}/{report.id}", headers=bearer(token)).status_code
            == status.HTTP_404_NOT_FOUND
        )

    def test_deleting_twice_answers_404(
        self, api_client: TestClient, make_report: Any, legal_case: Case, lawyer: User
    ) -> None:
        report = make_report(requested_by=lawyer, case=legal_case)
        token = token_for(api_client, lawyer.email)
        api_client.delete(f"{REPORTS_URL}/{report.id}", headers=bearer(token))

        second = api_client.delete(f"{REPORTS_URL}/{report.id}", headers=bearer(token))

        assert second.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# Regeneration
# --------------------------------------------------------------------------- #


class TestRegeneration:
    def test_it_answers_202_and_keeps_the_identifier(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        """202 rather than 201: no resource is created, the existing one has been
        accepted for reprocessing."""
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        response = api_client.post(
            f"{REPORTS_URL}/{report_id}/regenerate", headers=bearer(token)
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json()["id"] == report_id
        assert response.json()["attempt_count"] == 2

    def test_a_run_in_flight_answers_409(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        report_queue: RecordingIndexQueue,
    ) -> None:
        report_queue.run_inline = False
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        response = api_client.post(
            f"{REPORTS_URL}/{report_id}/regenerate", headers=bearer(token)
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_regenerating_someone_elses_report_answers_404(
        self,
        api_client: TestClient,
        make_report: Any,
        legal_case: Case,
        lawyer: User,
        outsider: User,
    ) -> None:
        report = make_report(requested_by=outsider, case=legal_case)

        response = api_client.post(
            f"{REPORTS_URL}/{report.id}/regenerate",
            headers=bearer(token_for(api_client, lawyer.email)),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


class TestExport:
    def test_markdown_export_is_served_as_an_attachment(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        response = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=markdown", headers=bearer(token)
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("text/markdown")
        assert "attachment" in response.headers["content-disposition"]
        assert response.content

    def test_pdf_export_is_served_as_a_pdf(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        response = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=pdf", headers=bearer(token)
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    def test_an_export_is_never_cached(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        """A generated report is one case's analysis; a shared cache in front of
        the API would be exactly what must not keep a copy."""
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        response = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=markdown", headers=bearer(token)
        )

        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_the_filename_is_safe_for_a_header(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(
            api_client, token, indexed_case, title='Note "urgente"\r\nX: y'
        ).json()["id"]

        disposition = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=markdown", headers=bearer(token)
        ).headers["content-disposition"]

        assert disposition.count('"') == 2
        assert "\r" not in disposition

    def test_an_unfinished_report_answers_409(
        self,
        api_client: TestClient,
        lawyer: User,
        indexed_case: Case,
        report_queue: RecordingIndexQueue,
    ) -> None:
        report_queue.run_inline = False
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        response = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=markdown", headers=bearer(token)
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_another_users_report_cannot_be_exported(
        self,
        api_client: TestClient,
        make_report: Any,
        legal_case: Case,
        lawyer: User,
        outsider: User,
    ) -> None:
        """"Exported reports inherit the same permissions as their source case",
        made structural: a shared link is a link to *this endpoint*, which
        refuses whoever follows it."""
        report = make_report(requested_by=outsider, case=legal_case)

        response = api_client.get(
            f"{REPORTS_URL}/{report.id}/export?format=markdown",
            headers=bearer(token_for(api_client, lawyer.email)),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_an_unknown_format_answers_422(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        response = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=docx", headers=bearer(token)
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_arabic_report_exports_as_pdf(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        """`project-overview.md` names Arabic as one of the platform's two
        languages, so this is the common case rather than an edge one. It works
        without configuration because the exporter finds and verifies a font with
        Arabic coverage itself — see `services/report_export.py`."""
        from services.report_export import find_arabic_font

        if find_arabic_font() is None:  # pragma: no cover - environment dependent
            pytest.skip("no font with Arabic coverage on this host")

        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case, language="ar").json()["id"]

        response = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=pdf", headers=bearer(token)
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.content.startswith(b"%PDF")

    def test_an_arabic_report_is_refused_as_pdf_when_no_font_exists(
        self, api_client: TestClient, lawyer: User, indexed_case: Case, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a host with no Arabic font at all, refusing is still the only
        honest answer: ReportLab's built-in fonts are Latin-only, so the
        alternative is a page of empty boxes that looks like a working export.
        The message names Markdown *and* how to fix it."""
        from services import report_export

        report_export.reset_report_renderer_cache()
        monkeypatch.setattr(report_export, "ARABIC_FONT_CANDIDATES", ())
        monkeypatch.setattr(settings, "REPORT_PDF_FONT_PATH", None)

        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case, language="ar").json()["id"]

        response = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=pdf", headers=bearer(token)
        )
        report_export.reset_report_renderer_cache()

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "Markdown" in response.json()["message"]

    def test_an_arabic_report_always_exports_as_markdown(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        """Which is what makes the refusal above a workaround rather than a dead
        end — Markdown needs no font and no library at all."""
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case, language="ar").json()["id"]

        response = api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=markdown", headers=bearer(token)
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.content.decode("utf-8")


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_it_reports_the_six_figures_the_spec_names(
        self, api_client: TestClient, lawyer: User, admin: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]
        api_client.get(
            f"{REPORTS_URL}/{report_id}/export?format=markdown", headers=bearer(token)
        )

        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert body["completed"] == 1
        assert body["average_duration_ms"] is not None
        assert body["total_exports"] == 1
        assert body["failed"] == 0
        assert body["average_characters"] is not None
        assert body["total_prompt_tokens"] is not None

    def test_it_carries_no_since_caveat(
        self, api_client: TestClient, admin: User
    ) -> None:
        """Unlike search, RAG, and the assistant: a report *is* a persisted run,
        so every figure is an exact SQL aggregate that survives a restart."""
        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert "since" not in body

    def test_it_reports_which_export_formats_work_here(
        self, api_client: TestClient, admin: User
    ) -> None:
        """So a client never offers an export the API will refuse."""
        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert "markdown" in body["available_formats"]

    def test_it_reports_nothing_about_any_particular_report(
        self, api_client: TestClient, lawyer: User, admin: User, indexed_case: Case
    ) -> None:
        """An operational view: counts, durations, sizes, and configuration only —
        never a report, a title, a section, a citation, or whose it was."""
        generate(api_client, token_for(api_client, lawyer.email), indexed_case)

        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        serialized = str(body)
        assert indexed_case.case_number not in serialized
        assert str(lawyer.id) not in serialized

    def test_the_window_can_be_narrowed(
        self, api_client: TestClient, admin: User
    ) -> None:
        body = api_client.get(
            f"{METRICS_URL}?window_days=7",
            headers=bearer(token_for(api_client, admin.email)),
        ).json()

        assert body["window_days"] == 7


# --------------------------------------------------------------------------- #
# Scope of the surface
# --------------------------------------------------------------------------- #


class TestScope:
    def test_no_field_on_the_wire_carries_a_prompt_or_a_vector(
        self, api_client: TestClient, lawyer: User, indexed_case: Case
    ) -> None:
        token = token_for(api_client, lawyer.email)
        report_id = generate(api_client, token, indexed_case).json()["id"]

        body = api_client.get(f"{REPORTS_URL}/{report_id}", headers=bearer(token)).json()

        assert "prompt" not in body
        assert "vector" not in str(body)
        assert "chunk_number" not in str(body)
        for citation in body["citations"]:
            assert "chunk_number" not in citation
            assert "embedding_model" not in citation

    def test_the_api_exposes_no_compliance_translation_or_scheduling_surface(
        self, client: TestClient
    ) -> None:
        """``14-ai-report-agent.md`` puts all three out of scope, and this is
        where a stray endpoint would show up."""
        paths = {
            path
            for path in client.app.openapi()["paths"]  # type: ignore[attr-defined]
            if path.startswith(f"{settings.API_V1_PREFIX}/reports")
        }

        assert paths == {
            f"{settings.API_V1_PREFIX}/reports",
            f"{settings.API_V1_PREFIX}/reports/templates",
            f"{settings.API_V1_PREFIX}/reports/metrics",
            f"{settings.API_V1_PREFIX}/reports/{{report_id}}",
            f"{settings.API_V1_PREFIX}/reports/{{report_id}}/regenerate",
            f"{settings.API_V1_PREFIX}/reports/{{report_id}}/export",
        }
