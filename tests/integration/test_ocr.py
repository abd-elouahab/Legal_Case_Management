"""Integration tests for the OCR Processing API.

Exercise the endpoints over real HTTP: the status/text/history/retry contract, the
monitoring view, authorization (401 vs 403 for every route and every role, plus
the per-case assignment check), and the guarantee the spec leads with — **the
upload request never waits for OCR**.

The service-level rules are unit-tested in ``tests/unit/test_ocr_service.py``;
what these add is the wire contract — status codes, the response shapes a client
polls and renders, error envelopes, and the assurance that extracted text is no
more reachable than the document it came from.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from core.ocr import PAGE_SEPARATOR
from models.case import Case
from models.document import Document
from models.ocr import OcrResult, OcrStatus
from models.user import User, UserRole
from services.ocr_engine import ExtractedPage, OcrCorruptedDocumentError, OcrTimeoutError
from tests.helpers import DOCX_BYTES, PDF_BYTES, PNG_BYTES

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import FakeOcrEngine, InMemoryDocumentStorage, RecordingOcrQueue

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
DOCUMENTS_URL = f"{settings.API_V1_PREFIX}/documents"
UPLOAD_URL = f"{DOCUMENTS_URL}/upload"
OCR_URL = f"{settings.API_V1_PREFIX}/ocr"
METRICS_URL = f"{OCR_URL}/metrics"

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


def ocr_url(document_id: Any, suffix: str = "") -> str:
    return f"{DOCUMENTS_URL}/{document_id}/ocr{suffix}"


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def admin_headers(api_client: TestClient, admin: User) -> dict[str, str]:
    return bearer(token_for(api_client, admin.email))


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(
        email="lawyer@example.com",
        password=PASSWORD,
        first_name="Karim",
        last_name="Zahra",
        role=UserRole.LAWYER,
    )


@pytest.fixture
def lawyer_headers(api_client: TestClient, lawyer: User) -> dict[str, str]:
    return bearer(token_for(api_client, lawyer.email))


@pytest.fixture
def other_lawyer(make_user: MakeUser) -> User:
    return make_user(
        email="other.lawyer@example.com",
        password=PASSWORD,
        first_name="Sofia",
        last_name="Bennani",
        role=UserRole.LAWYER,
    )


@pytest.fixture
def other_lawyer_headers(api_client: TestClient, other_lawyer: User) -> dict[str, str]:
    return bearer(token_for(api_client, other_lawyer.email))


@pytest.fixture
def representative(make_user: MakeUser) -> User:
    return make_user(
        email="court@example.com",
        password=PASSWORD,
        first_name="Nadia",
        last_name="Alami",
        role=UserRole.COURT_REPRESENTATIVE,
    )


@pytest.fixture
def representative_headers(api_client: TestClient, representative: User) -> dict[str, str]:
    return bearer(token_for(api_client, representative.email))


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User, representative: User) -> Case:
    return make_case(
        assigned_lawyer_id=lawyer.id,
        assigned_court_representative_id=representative.id,
    )


@pytest.fixture
def document(make_document: MakeDocument, legal_case: Case) -> Document:
    return make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)


class TestAuthentication:
    @pytest.mark.parametrize(
        ("method", "suffix"),
        [
            ("get", ""),
            ("get", "/text"),
            ("get", "/history"),
            ("post", "/retry"),
        ],
    )
    def test_every_document_route_requires_a_token(
        self, api_client: TestClient, document: Document, method: str, suffix: str
    ) -> None:
        response = getattr(api_client, method)(ocr_url(document.id, suffix))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize("url", [OCR_URL, METRICS_URL])
    def test_every_collection_route_requires_a_token(
        self, api_client: TestClient, url: str
    ) -> None:
        response = api_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_a_malformed_token_is_401_not_403(
        self, api_client: TestClient, document: Document
    ) -> None:
        response = api_client.get(ocr_url(document.id), headers=bearer("not-a-token"))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestUploadSchedulesExtraction:
    def test_an_upload_creates_an_ocr_job(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_queue: RecordingOcrQueue,
    ) -> None:
        ocr_queue.run_inline = False

        response = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("scan.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert len(ocr_queue.jobs) == 1

    def test_the_upload_returns_immediately(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_queue: RecordingOcrQueue,
    ) -> None:
        # The spec's headline requirement: "the upload request must never wait
        # for OCR to complete". With the queue not running inline, the response
        # arrives while the run is still `pending`.
        ocr_queue.run_inline = False

        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("scan.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = upload.json()["id"]

        response = api_client.get(ocr_url(document_id), headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == OcrStatus.PENDING.value
        assert response.json()["is_active"] is True

    def test_the_upload_response_carries_no_ocr_field(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        # The document payload is unchanged by this feature: OCR is its own
        # resource with its own endpoints, so a document reader is unaffected.
        response = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("scan.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )

        assert "ocr" not in response.json()

    def test_extraction_runs_asynchronously_and_completes(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("scan.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = upload.json()["id"]

        response = api_client.get(ocr_url(document_id), headers=admin_headers)
        body = response.json()

        assert body["status"] == OcrStatus.COMPLETED.value
        assert body["is_terminal"] is True
        assert body["is_active"] is False

    @pytest.mark.parametrize(
        ("filename", "payload", "content_type"),
        [
            ("scan.pdf", PDF_BYTES, "application/pdf"),
            ("photo.png", PNG_BYTES, "image/png"),
        ],
    )
    def test_every_supported_format_is_processed(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        filename: str,
        payload: bytes,
        content_type: str,
    ) -> None:
        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": (filename, payload, content_type)},
            data={"case_id": str(legal_case.id)},
        )

        response = api_client.get(ocr_url(upload.json()["id"]), headers=admin_headers)

        assert response.json()["status"] == OcrStatus.COMPLETED.value

    def test_an_unsupported_type_gets_no_run(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_queue: RecordingOcrQueue,
    ) -> None:
        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={
                "file": (
                    "brief.docx",
                    DOCX_BYTES,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data={"case_id": str(legal_case.id)},
        )

        assert upload.status_code == status.HTTP_201_CREATED
        assert ocr_queue.jobs == []

        response = api_client.get(ocr_url(upload.json()["id"]), headers=admin_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "ocr_result_not_found"

    def test_a_replacement_schedules_its_own_extraction(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("scan.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = upload.json()["id"]

        api_client.post(
            f"{DOCUMENTS_URL}/{document_id}/replace",
            headers=admin_headers,
            files={"file": ("scan-v2.pdf", PDF_BYTES, "application/pdf")},
        )

        history = api_client.get(ocr_url(document_id, "/history"), headers=admin_headers)

        assert [run["document_version"] for run in history.json()] == [1, 2]


class TestStatusEndpoint:
    def test_it_reports_every_metadata_field_the_spec_lists(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        body = api_client.get(ocr_url(document.id), headers=admin_headers).json()

        for field in (
            "status",
            "started_at",
            "finished_at",
            "duration_ms",
            "engine",
            "detected_language",
            "page_count",
            "confidence",
        ):
            assert field in body, field
        assert body["engine"] == "fake"
        assert body["detected_language"] == "eng+fra"
        assert body["page_count"] == 1
        assert body["duration_ms"] is not None

    def test_it_carries_no_extracted_text(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        body = api_client.get(ocr_url(document.id), headers=admin_headers).json()

        assert "pages" not in body
        assert "full_text" not in body

    def test_an_unknown_document_is_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(ocr_url(uuid.uuid4()), headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "document_not_found"

    def test_an_unknown_version_is_404(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        response = api_client.get(
            ocr_url(document.id), headers=admin_headers, params={"version": 99}
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "document_version_not_found"

    def test_a_version_below_one_is_422(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        response = api_client.get(
            ocr_url(document.id), headers=admin_headers, params={"version": 0}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestTextEndpoint:
    def test_it_returns_the_pages_in_order(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        ocr_engine.pages = [
            ExtractedPage(page_number=1, text="Page one.", confidence=90.0),
            ExtractedPage(page_number=2, text="Page two.", confidence=85.0),
            ExtractedPage(page_number=3, text="Page three.", confidence=95.0),
        ]
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        body = api_client.get(ocr_url(document.id, "/text"), headers=admin_headers).json()

        assert [page["page_number"] for page in body["pages"]] == [1, 2, 3]
        assert [page["text"] for page in body["pages"]] == [
            "Page one.",
            "Page two.",
            "Page three.",
        ]

    def test_the_full_text_preserves_page_boundaries(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        ocr_engine.pages = [
            ExtractedPage(page_number=1, text="one"),
            ExtractedPage(page_number=2, text="two"),
        ]
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        body = api_client.get(ocr_url(document.id, "/text"), headers=admin_headers).json()

        assert body["page_separator"] == PAGE_SEPARATOR
        # Splitting the joined form recovers exactly the pages array, so the
        # convenience shape loses no boundary.
        assert body["full_text"].split(body["page_separator"]) == ["one", "two"]

    def test_it_preserves_multilingual_unicode(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        arabic = "محضر الجلسة"
        french = "Procès-verbal d'audience"
        ocr_engine.pages = [
            ExtractedPage(page_number=1, text=arabic),
            ExtractedPage(page_number=2, text=french),
        ]
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        body = api_client.get(ocr_url(document.id, "/text"), headers=admin_headers).json()

        assert body["pages"][0]["text"] == arabic
        assert body["pages"][1]["text"] == french

    def test_it_counts_the_characters(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        ocr_engine.pages = [ExtractedPage(page_number=1, text="abcd")]
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        body = api_client.get(ocr_url(document.id, "/text"), headers=admin_headers).json()

        assert body["character_count"] == 4
        assert body["pages"][0]["character_count"] == 4

    def test_an_unfinished_run_returns_its_status_not_an_error(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        ocr_queue: RecordingOcrQueue,
    ) -> None:
        ocr_queue.run_inline = False
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        response = api_client.get(ocr_url(document.id, "/text"), headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == OcrStatus.PENDING.value
        assert response.json()["pages"] == []

    def test_a_document_with_no_run_is_404(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        response = api_client.get(ocr_url(document.id, "/text"), headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestRetry:
    def test_a_failed_run_can_be_retried_without_re_uploading(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        ocr_engine.raises = OcrCorruptedDocumentError("bad scan")
        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("scan.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = upload.json()["id"]
        assert (
            api_client.get(ocr_url(document_id), headers=admin_headers).json()["status"]
            == OcrStatus.FAILED.value
        )

        ocr_engine.raises = None
        response = api_client.post(ocr_url(document_id, "/retry"), headers=admin_headers)

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert (
            api_client.get(ocr_url(document_id), headers=admin_headers).json()["status"]
            == OcrStatus.COMPLETED.value
        )

    def test_a_retry_is_202_not_200(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        # The work is accepted, not done.
        response = api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        assert response.status_code == status.HTTP_202_ACCEPTED

    def test_a_retry_never_duplicates_the_record(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
    ) -> None:
        first = api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers).json()
        second = api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers).json()

        assert first["id"] == second["id"]
        history = api_client.get(ocr_url(document.id, "/history"), headers=admin_headers)
        assert len(history.json()) == 1

    def test_a_retry_counts_the_attempt(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        body = api_client.get(ocr_url(document.id), headers=admin_headers).json()

        assert body["attempt_count"] == 2

    def test_a_running_extraction_is_409(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        ocr_queue: RecordingOcrQueue,
    ) -> None:
        ocr_queue.run_inline = False
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        response = api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error"] == "ocr_already_running"

    def test_an_unsupported_type_is_422(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_document: MakeDocument,
        legal_case: Case,
    ) -> None:
        document = make_document(case_id=legal_case.id, extension="docx", content=DOCX_BYTES)

        response = api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["error"] == "ocr_unsupported_format"
        # The message names what *is* supported, so the caller can act on it.
        assert "pdf" in response.json()["message"]

    def test_a_disabled_platform_is_503(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "OCR_ENABLED", False)

        response = api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "ocr_disabled"

    def test_it_never_touches_the_stored_file(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        document_storage: InMemoryDocumentStorage,
    ) -> None:
        before = dict(document_storage.objects)

        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        assert document_storage.objects == before
        assert document_storage.logical_deletes == []

    def test_can_retry_matches_what_the_api_allows(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        ocr_queue: RecordingOcrQueue,
    ) -> None:
        ocr_queue.run_inline = False
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)
        pending = api_client.get(ocr_url(document.id), headers=admin_headers).json()

        # A client that trusts `can_retry` must never be offered an action the
        # API answers with 409.
        assert pending["can_retry"] is False
        assert (
            api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers).status_code
            == status.HTTP_409_CONFLICT
        )


class TestAuthorization:
    def test_the_assigned_lawyer_may_read_and_retry(
        self, api_client: TestClient, lawyer_headers: dict[str, str], document: Document
    ) -> None:
        assert (
            api_client.post(ocr_url(document.id, "/retry"), headers=lawyer_headers).status_code
            == status.HTTP_202_ACCEPTED
        )
        assert (
            api_client.get(ocr_url(document.id), headers=lawyer_headers).status_code
            == status.HTTP_200_OK
        )

    @pytest.mark.parametrize("suffix", ["", "/text", "/history"])
    def test_an_unassigned_lawyer_is_refused(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        other_lawyer_headers: dict[str, str],
        document: Document,
        suffix: str,
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        response = api_client.get(ocr_url(document.id, suffix), headers=other_lawyer_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_the_denial_names_neither_permission_nor_role(
        self,
        api_client: TestClient,
        other_lawyer_headers: dict[str, str],
        document: Document,
    ) -> None:
        body = api_client.get(ocr_url(document.id), headers=other_lawyer_headers).text

        assert "ocr:" not in body
        assert "lawyer" not in body.lower()

    def test_extracted_text_is_no_more_reachable_than_the_document(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        other_lawyer_headers: dict[str, str],
        document: Document,
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        document_response = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}", headers=other_lawyer_headers
        )
        text_response = api_client.get(ocr_url(document.id, "/text"), headers=other_lawyer_headers)

        # The spec's "extracted text inherits document permissions", asserted as
        # the identity it is rather than as two rules that happen to agree.
        assert document_response.status_code == text_response.status_code
        assert text_response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_court_representative_may_read_but_not_retry(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        representative_headers: dict[str, str],
        document: Document,
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        assert (
            api_client.get(ocr_url(document.id), headers=representative_headers).status_code
            == status.HTTP_200_OK
        )
        # `ocr:retry` consumes real processing capacity, and the court role's
        # description does not extend to operating the platform's pipeline.
        assert (
            api_client.post(
                ocr_url(document.id, "/retry"), headers=representative_headers
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

    @pytest.mark.parametrize("headers_name", ["lawyer_headers", "representative_headers"])
    def test_metrics_are_administrator_only(
        self, api_client: TestClient, request: pytest.FixtureRequest, headers_name: str
    ) -> None:
        headers = request.getfixturevalue(headers_name)

        assert api_client.get(METRICS_URL, headers=headers).status_code == (
            status.HTTP_403_FORBIDDEN
        )

    def test_a_deleted_document_hides_its_text(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)
        api_client.delete(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers)

        response = api_client.get(ocr_url(document.id, "/text"), headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestListEndpoint:
    def test_it_pages_and_totals(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
    ) -> None:
        for _ in range(3):
            document = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
            make_ocr_result(document_id=document.id)

        response = api_client.get(OCR_URL, headers=admin_headers, params={"page_size": 2})
        body = response.json()

        assert body["total_records"] == 3
        assert body["total_pages"] == 2
        assert len(body["items"]) == 2

    def test_it_filters_by_status(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
    ) -> None:
        first = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
        second = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
        make_ocr_result(document_id=first.id, status=OcrStatus.COMPLETED)
        make_ocr_result(document_id=second.id, status=OcrStatus.FAILED, error_code="timeout")

        response = api_client.get(OCR_URL, headers=admin_headers, params={"status": "failed"})

        assert response.json()["total_records"] == 1

    def test_it_filters_by_case(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_case: MakeCase,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
    ) -> None:
        mine = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
        other_case = make_case(title="Unrelated")
        theirs = make_document(case_id=other_case.id, extension="pdf", content=PDF_BYTES)
        make_ocr_result(document_id=mine.id)
        make_ocr_result(document_id=theirs.id)

        response = api_client.get(
            OCR_URL, headers=admin_headers, params={"case_id": str(legal_case.id)}
        )

        assert response.json()["total_records"] == 1

    def test_the_scope_restricts_the_total(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        admin_headers: dict[str, str],
        legal_case: Case,
        make_case: MakeCase,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
    ) -> None:
        mine = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
        other_case = make_case(title="Unrelated")
        theirs = make_document(case_id=other_case.id, extension="pdf", content=PDF_BYTES)
        make_ocr_result(document_id=mine.id)
        make_ocr_result(document_id=theirs.id)

        # Applied in SQL, so the total counts only what the caller may access —
        # it must not reveal how many runs a lawyer is not allowed to see.
        assert (
            api_client.get(OCR_URL, headers=lawyer_headers).json()["total_records"] == 1
        )
        assert api_client.get(OCR_URL, headers=admin_headers).json()["total_records"] == 2

    def test_an_unknown_parameter_is_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(OCR_URL, headers=admin_headers, params={"nope": "1"})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_oversized_page_is_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(OCR_URL, headers=admin_headers, params={"page_size": 500})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_page_past_the_end_is_empty_not_an_error(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(OCR_URL, headers=admin_headers, params={"page": 50})

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"] == []


class TestMetricsEndpoint:
    def test_it_reports_the_rates_and_the_average(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
    ) -> None:
        for _ in range(3):
            document = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
            make_ocr_result(
                document_id=document.id, status=OcrStatus.COMPLETED, duration_ms=1000
            )
        failed = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
        make_ocr_result(
            document_id=failed.id, status=OcrStatus.FAILED, error_code="timeout", duration_ms=9000
        )

        body = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert body["total_runs"] == 4
        assert body["success_rate"] == 75.0
        assert body["failure_rate"] == 25.0
        # Failed runs are excluded: a timeout answers a different question from
        # "how long does extraction take".
        assert body["average_duration_ms"] == 1000
        assert body["average_duration_seconds"] == 1.0

    def test_it_groups_failures_by_cause(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
    ) -> None:
        for code in ("timeout", "timeout", "engine_failure"):
            document = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
            make_ocr_result(document_id=document.id, status=OcrStatus.FAILED, error_code=code)

        body = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert body["failures_by_code"] == {"timeout": 2, "engine_failure": 1}

    def test_an_empty_platform_reports_zero(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert body["total_runs"] == 0
        assert body["success_rate"] == 0.0
        assert body["failure_rate"] == 0.0

    def test_a_window_restricts_the_figures(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
        make_ocr_result: MakeOcrResult,
    ) -> None:
        old = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
        recent = make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)
        make_ocr_result(document_id=old.id, created_at=datetime.now(UTC) - timedelta(days=40))
        make_ocr_result(document_id=recent.id)

        body = api_client.get(
            METRICS_URL, headers=admin_headers, params={"window_days": 7}
        ).json()

        assert body["total_runs"] == 1
        assert body["window_days"] == 7

    def test_it_reports_the_engine_and_whether_it_is_installed(
        self, api_client: TestClient, admin_headers: dict[str, str], ocr_engine: FakeOcrEngine
    ) -> None:
        assert api_client.get(METRICS_URL, headers=admin_headers).json()["engine_available"] is (
            True
        )

        ocr_engine.available = False
        body = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert body["engine"] == "fake"
        assert body["engine_available"] is False

    def test_it_names_the_supported_formats(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert body["supported_extensions"] == ["jpeg", "jpg", "pdf", "png"]

    def test_it_never_names_a_document_or_a_case(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        body = api_client.get(METRICS_URL, headers=admin_headers).text

        # An operational view of the pipeline: counts and timings only.
        assert str(document.id) not in body
        assert document.original_filename not in body

    def test_an_out_of_range_window_is_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            METRICS_URL, headers=admin_headers, params={"window_days": 0}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestTimelineIntegration:
    def timeline(self, client: TestClient, case_id: Any, headers: dict[str, str]) -> list[str]:
        response = client.get(
            f"{settings.API_V1_PREFIX}/cases/{case_id}/timeline",
            headers=headers,
            params={"page_size": 100},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        return [event["event_type"] for event in response.json()["items"]]

    def test_a_successful_run_records_started_and_completed(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        legal_case: Case,
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        types = self.timeline(api_client, legal_case.id, admin_headers)

        assert "ocr_retried" in types
        assert "ocr_started" in types
        assert "ocr_completed" in types

    def test_a_failed_run_records_failed(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        ocr_engine.raises = OcrTimeoutError("slow")

        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        assert "ocr_failed" in self.timeline(api_client, legal_case.id, admin_headers)

    def test_the_events_are_categorised_as_document_events(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        legal_case: Case,
    ) -> None:
        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        response = api_client.get(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}/timeline",
            headers=admin_headers,
            params={"event_type": "ocr_completed"},
        )

        # They reuse the five icon families `08-timeline.md` defines rather than
        # forcing a sixth into the timeline module.
        assert response.json()["items"][0]["category"] == "document"

    def test_the_timeline_never_carries_the_extracted_text(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        secret = "CLAUSE PENALE CONFIDENTIELLE"
        ocr_engine.pages = [ExtractedPage(page_number=1, text=secret)]

        api_client.post(ocr_url(document.id, "/retry"), headers=admin_headers)

        response = api_client.get(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}/timeline",
            headers=admin_headers,
            params={"page_size": 100},
        )
        assert secret not in response.text


class TestOpenApiContract:
    def test_every_route_is_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        for path in (
            f"{settings.API_V1_PREFIX}/documents/{{document_id}}/ocr",
            f"{settings.API_V1_PREFIX}/documents/{{document_id}}/ocr/text",
            f"{settings.API_V1_PREFIX}/documents/{{document_id}}/ocr/history",
            f"{settings.API_V1_PREFIX}/documents/{{document_id}}/ocr/retry",
            f"{settings.API_V1_PREFIX}/ocr",
            f"{settings.API_V1_PREFIX}/ocr/metrics",
        ):
            assert path in paths, path

    def test_each_route_documents_its_failures(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        retry = paths[f"{settings.API_V1_PREFIX}/documents/{{document_id}}/ocr/retry"]["post"]

        assert retry["summary"]
        assert retry["description"]
        for code in ("401", "403", "404", "409", "422", "503"):
            assert code in retry["responses"], code

    def test_the_ocr_routes_are_tagged_together(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        # They live under two prefixes but belong to one module, so OpenAPI must
        # group them with the module that owns them.
        assert paths[f"{settings.API_V1_PREFIX}/ocr"]["get"]["tags"] == ["ocr"]
        assert paths[f"{settings.API_V1_PREFIX}/documents/{{document_id}}/ocr"]["get"][
            "tags"
        ] == ["ocr"]


class TestDependencyWiring:
    def test_the_application_wires_a_real_queue_and_engine(self) -> None:
        from api.deps import get_ocr_engine_dependency, get_ocr_job_queue
        from services.ocr_engine import TesseractOcrEngine
        from services.ocr_queue import ThreadPoolOcrJobQueue

        # `OcrService` defaults to a queue that schedules nothing, so it can be
        # built in a context with no background worker. This asserts the
        # *application* never takes that default — if it did, every upload would
        # sit at `pending` forever while every unit test still passed.
        assert isinstance(get_ocr_job_queue(), ThreadPoolOcrJobQueue)
        assert isinstance(get_ocr_engine_dependency(), TesseractOcrEngine)

    def test_the_document_service_schedules_through_the_ocr_service(
        self, db_session: Any, document_storage: InMemoryDocumentStorage
    ) -> None:
        from api.deps import (
            get_document_service,
            get_event_publisher,
            get_indexing_service,
            get_ocr_service,
        )
        from repositories.case import CaseRepository
        from repositories.document import DocumentRepository
        from repositories.indexing import IndexingRepository
        from repositories.ocr import OcrRepository
        from repositories.timeline import TimelineRepository
        from services.chunking import get_chunker
        from services.embedding import get_embedder
        from services.indexing import IndexJob
        from services.job_queue import NullJobQueue
        from services.ocr import OcrService
        from services.ocr_engine import get_ocr_engine
        from services.ocr_queue import NullOcrJobQueue
        from services.timeline import TimelineService
        from services.vector_store import get_vector_store

        cases = CaseRepository(db_session)
        documents = DocumentRepository(db_session)
        results = OcrRepository(db_session)
        timeline = TimelineService(TimelineRepository(db_session), cases)
        # Indexing is a publisher too, and `get_ocr_service` now takes one — so it
        # is built the same way the application does, through its own factory.
        indexing = get_indexing_service(
            IndexingRepository(db_session),
            documents,
            results,
            get_chunker(),
            get_embedder(),
            get_vector_store(),
            NullJobQueue[IndexJob](name="indexing"),
            timeline,
            get_event_publisher(),
        )
        ocr = get_ocr_service(
            results,
            documents,
            document_storage,
            get_ocr_engine(),
            NullOcrJobQueue(),
            timeline,
            indexing,
            get_event_publisher(),
        )

        service = get_document_service(
            documents, cases, document_storage, timeline, ocr, get_event_publisher()
        )

        assert isinstance(service._ocr, OcrService)
