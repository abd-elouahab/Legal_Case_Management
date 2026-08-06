"""Integration tests for the Document Indexing API.

Exercise the endpoints over real HTTP: the status/history/reindex contract, the
list, the monitoring view, authorization (401 vs 403 for every route and every
role, plus the per-case assignment check), and the guarantee the spec's flow
diagram leads with — **a completed extraction hands the pipeline on to indexing
without anything waiting for it**.

The service-level rules are unit-tested in
``tests/unit/test_indexing_service.py``; what these add is the wire contract —
status codes, the response shapes a client polls and renders, error envelopes,
the assurance that an index is no more reachable than the document it was built
from, and the assurance that **no endpoint here returns a passage or a vector**,
because Semantic Search is out of scope.
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
from models.case import Case
from models.document import Document
from models.indexing import DocumentIndex, IndexStatus
from models.ocr import OcrResult, OcrStatus
from models.user import User, UserRole
from services.embedding import EmbeddingError
from services.ocr_engine import ExtractedPage
from services.vector_store import VectorStoreError
from tests.helpers import PDF_BYTES

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import (
        FakeEmbedder,
        FakeOcrEngine,
        InMemoryVectorStore,
    )

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
DOCUMENTS_URL = f"{settings.API_V1_PREFIX}/documents"
UPLOAD_URL = f"{DOCUMENTS_URL}/upload"
INDEXING_URL = f"{settings.API_V1_PREFIX}/indexing"
METRICS_URL = f"{INDEXING_URL}/metrics"

PAGE_ONE = (
    "CONTRAT DE BAIL COMMERCIAL. Article 1 : Objet. Le bailleur loue au preneur les "
    "locaux désignés ci-après, situés à Casablanca, pour l'exercice d'une activité "
    "commerciale conforme au règlement de copropriété de l'immeuble."
)

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeDocument = Callable[..., Document]
MakeOcrResult = Callable[..., OcrResult]
MakeIndex = Callable[..., DocumentIndex]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == status.HTTP_200_OK, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def index_url(document_id: Any, suffix: str = "") -> str:
    return f"{DOCUMENTS_URL}/{document_id}/index{suffix}"


# --------------------------------------------------------------------------- #
# Actors
# --------------------------------------------------------------------------- #


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
    return make_document(case_id=legal_case.id, original_filename="bail.pdf")


@pytest.fixture
def extracted(make_ocr_result: MakeOcrResult, document: Document) -> OcrResult:
    """A completed extraction — the precondition for indexing."""
    return make_ocr_result(document_id=document.id, pages=[PAGE_ONE])


@pytest.fixture
def indexed(
    make_document_index: MakeIndex, document: Document, legal_case: Case
) -> DocumentIndex:
    return make_document_index(document_id=document.id, case_id=legal_case.id)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


class TestAuthentication:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "status"),
            ("get", "history"),
            ("post", "reindex"),
            ("get", "list"),
            ("get", "metrics"),
        ],
    )
    def test_every_route_requires_a_token(
        self, api_client: TestClient, document: Document, method: str, path: str
    ) -> None:
        url = {
            "status": index_url(document.id),
            "history": index_url(document.id, "/history"),
            "reindex": index_url(document.id, "/reindex"),
            "list": INDEXING_URL,
            "metrics": METRICS_URL,
        }[path]

        response = getattr(api_client, method)(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers.get("WWW-Authenticate") == "Bearer"


# --------------------------------------------------------------------------- #
# The pipeline, end to end over HTTP
# --------------------------------------------------------------------------- #


class TestUploadToIndex:
    def test_an_upload_produces_a_searchable_document_without_waiting(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        # The whole pipeline in one request: upload → extract → chunk → embed →
        # store → indexed. The upload itself returns 201 immediately; both stages
        # run on (test-inline) background queues.
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]

        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id), "category": "contract"},
        )
        assert upload.status_code == status.HTTP_201_CREATED, upload.text
        document_id = upload.json()["id"]

        response = api_client.get(index_url(document_id), headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK, response.text

        body = response.json()
        assert body["status"] == "indexed"
        assert body["chunk_count"] >= 1
        assert body["is_terminal"] is True
        assert body["can_reindex"] is True

    def test_the_vectors_are_actually_written(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
        vector_store: InMemoryVectorStore,
    ) -> None:
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]

        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = uuid.UUID(upload.json()["id"])

        assert vector_store.for_version(document_id, 1)

    def test_a_document_whose_extraction_failed_gets_no_index(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        # A row that could only ever say "there was nothing to index" is not a
        # record worth keeping; the read endpoint says so explicitly instead.
        from services.ocr_engine import OcrCorruptedDocumentError

        ocr_engine.raises = OcrCorruptedDocumentError("unreadable")

        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = upload.json()["id"]

        response = api_client.get(index_url(document_id), headers=admin_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "document_index_not_found"


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


class TestReadingTheIndex:
    def test_the_status_payload_carries_the_metadata_the_spec_lists(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        indexed: DocumentIndex,
    ) -> None:
        body = api_client.get(index_url(document.id), headers=admin_headers).json()

        assert body["document_id"] == str(document.id)
        assert body["document_version"] == 1
        assert body["case_id"] == str(document.case_id)
        assert body["chunk_count"] == 3
        assert body["embedding_model"] == "fake/test-embedder"
        assert body["embedding_dimensions"] == 8
        assert body["vector_collection"] == "test-chunks"
        assert body["chunk_size"] == 1000
        assert body["chunk_overlap"] == 200
        assert body["detected_language"] == "fr"
        assert body["duration_seconds"] == 4.2

    def test_the_payload_carries_no_passage_and_no_vector(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        indexed: DocumentIndex,
    ) -> None:
        # ``10-document-indexing.md`` puts Semantic Search out of scope; a
        # payload returning passages would be a retrieval API with another name.
        body = api_client.get(index_url(document.id), headers=admin_headers).json()

        for forbidden in ("chunks", "passages", "text", "vector", "vectors"):
            assert forbidden not in body

    def test_an_unknown_document_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(index_url(uuid.uuid4()), headers=admin_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "document_not_found"

    def test_an_unknown_version_is_a_404(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        indexed: DocumentIndex,
    ) -> None:
        response = api_client.get(
            index_url(document.id), headers=admin_headers, params={"version": 9}
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "document_version_not_found"

    def test_version_zero_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        response = api_client.get(
            index_url(document.id), headers=admin_headers, params={"version": 0}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_deleted_document_s_index_is_unreachable(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        indexed: DocumentIndex,
    ) -> None:
        # An index inherits its document's permissions, and a withdrawn document
        # is no longer readable.
        api_client.delete(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers)

        response = api_client.get(index_url(document.id), headers=admin_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestHistory:
    def test_it_lists_one_index_per_version_oldest_first(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_document_index: MakeIndex,
        document: Document,
        legal_case: Case,
    ) -> None:
        make_document_index(
            document_id=document.id, case_id=legal_case.id, document_version=2
        )
        make_document_index(
            document_id=document.id, case_id=legal_case.id, document_version=1
        )

        body = api_client.get(
            index_url(document.id, "/history"), headers=admin_headers
        ).json()
        assert [entry["document_version"] for entry in body] == [1, 2]

    def test_a_document_never_indexed_has_an_empty_history(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        response = api_client.get(index_url(document.id, "/history"), headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []


# --------------------------------------------------------------------------- #
# Re-indexing
# --------------------------------------------------------------------------- #


class TestReindexing:
    def test_a_rebuild_is_accepted_with_202(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        extracted: OcrResult,
        indexed: DocumentIndex,
    ) -> None:
        # 202, not 200: the work has been accepted, not done.
        response = api_client.post(index_url(document.id, "/reindex"), headers=admin_headers)
        assert response.status_code == status.HTTP_202_ACCEPTED, response.text

    def test_a_rebuild_re_uses_the_same_record(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        extracted: OcrResult,
        indexed: DocumentIndex,
    ) -> None:
        body = api_client.post(
            index_url(document.id, "/reindex"), headers=admin_headers
        ).json()
        assert body["id"] == str(indexed.id)
        assert body["attempt_count"] == 2

    def test_a_rebuild_produces_no_duplicate_vectors(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
        vector_store: InMemoryVectorStore,
    ) -> None:
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]
        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = uuid.UUID(upload.json()["id"])
        before = len(vector_store.for_version(document_id, 1))

        api_client.post(index_url(document_id, "/reindex"), headers=admin_headers)
        assert len(vector_store.for_version(document_id, 1)) == before

    def test_a_document_with_no_extracted_text_is_refused_with_409(
        self, api_client: TestClient, admin_headers: dict[str, str], document: Document
    ) -> None:
        # Indexing begins where extraction ends, so the answer names the
        # sequencing conflict rather than pretending the request was malformed.
        response = api_client.post(index_url(document.id, "/reindex"), headers=admin_headers)
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error"] == "indexing_not_ready"

    def test_a_failed_extraction_is_refused_and_names_its_state(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_ocr_result: MakeOcrResult,
        document: Document,
    ) -> None:
        make_ocr_result(document_id=document.id, status=OcrStatus.FAILED)

        response = api_client.post(index_url(document.id, "/reindex"), headers=admin_headers)
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "failed" in response.json()["message"]

    def test_a_running_index_is_refused_with_409(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_document_index: MakeIndex,
        document: Document,
        legal_case: Case,
        extracted: OcrResult,
    ) -> None:
        make_document_index(
            document_id=document.id, case_id=legal_case.id, status=IndexStatus.INDEXING
        )
        response = api_client.post(index_url(document.id, "/reindex"), headers=admin_headers)
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error"] == "indexing_already_running"

    def test_can_reindex_never_offers_what_the_api_would_refuse(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_document_index: MakeIndex,
        document: Document,
        legal_case: Case,
        extracted: OcrResult,
    ) -> None:
        make_document_index(
            document_id=document.id, case_id=legal_case.id, status=IndexStatus.INDEXING
        )
        body = api_client.get(index_url(document.id), headers=admin_headers).json()
        assert body["can_reindex"] is False

        refused = api_client.post(index_url(document.id, "/reindex"), headers=admin_headers)
        assert refused.status_code == status.HTTP_409_CONFLICT

    def test_a_rebuild_leaves_the_document_and_its_text_untouched(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        extracted: OcrResult,
        indexed: DocumentIndex,
    ) -> None:
        before = api_client.get(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers).json()
        text_before = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/ocr/text", headers=admin_headers
        ).json()

        api_client.post(index_url(document.id, "/reindex"), headers=admin_headers)

        after = api_client.get(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers).json()
        text_after = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/ocr/text", headers=admin_headers
        ).json()
        assert after == before
        assert text_after["pages"] == text_before["pages"]

    def test_indexing_disabled_answers_503(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        extracted: OcrResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "INDEXING_ENABLED", False)
        response = api_client.post(index_url(document.id, "/reindex"), headers=admin_headers)
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "indexing_disabled"


# --------------------------------------------------------------------------- #
# Failure handling over the wire
# --------------------------------------------------------------------------- #


class TestFailures:
    def test_an_embedding_failure_is_a_200_describing_a_failed_run(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
        embedder: FakeEmbedder,
    ) -> None:
        # A failure is a recorded *state*, not a failed request: answering 5xx
        # would say the platform is broken when one document could not be
        # embedded.
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]
        embedder.raises = EmbeddingError("model exploded")

        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = upload.json()["id"]

        response = api_client.get(index_url(document_id), headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK

        body = response.json()
        assert body["status"] == "failed"
        assert body["error_code"] == "embedding_failure"
        assert body["error_message"]
        assert body["can_reindex"] is True

    def test_an_unavailable_vector_database_is_recorded_and_retryable(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
        vector_store: InMemoryVectorStore,
    ) -> None:
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]
        vector_store.raises = VectorStoreError("qdrant is down")

        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        document_id = upload.json()["id"]

        body = api_client.get(index_url(document_id), headers=admin_headers).json()
        assert body["error_code"] == "vector_store_unavailable"

        # And the document is entirely unaffected.
        document = api_client.get(f"{DOCUMENTS_URL}/{document_id}", headers=admin_headers)
        assert document.status_code == status.HTTP_200_OK

    def test_a_failure_message_never_quotes_the_document(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
        embedder: FakeEmbedder,
    ) -> None:
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]
        embedder.raises = EmbeddingError("failed on 'Le bailleur loue au preneur'")

        upload = api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )
        body = api_client.get(
            index_url(upload.json()["id"]), headers=admin_headers
        ).json()
        assert "bailleur" not in (body["error_message"] or "")


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuthorization:
    def test_the_assigned_lawyer_reads_and_rebuilds(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        document: Document,
        extracted: OcrResult,
        indexed: DocumentIndex,
    ) -> None:
        assert (
            api_client.get(index_url(document.id), headers=lawyer_headers).status_code
            == status.HTTP_200_OK
        )
        assert (
            api_client.post(
                index_url(document.id, "/reindex"), headers=lawyer_headers
            ).status_code
            == status.HTTP_202_ACCEPTED
        )

    def test_the_court_representative_reads_but_may_not_rebuild(
        self,
        api_client: TestClient,
        representative_headers: dict[str, str],
        document: Document,
        extracted: OcrResult,
        indexed: DocumentIndex,
    ) -> None:
        # A rebuild re-embeds every passage of the document — by far the most
        # expensive operation the platform performs — and the court role's
        # description does not extend to operating the pipeline.
        assert (
            api_client.get(index_url(document.id), headers=representative_headers).status_code
            == status.HTTP_200_OK
        )
        refused = api_client.post(
            index_url(document.id, "/reindex"), headers=representative_headers
        )
        assert refused.status_code == status.HTTP_403_FORBIDDEN

    def test_an_unassigned_lawyer_is_refused_every_per_document_route(
        self,
        api_client: TestClient,
        other_lawyer_headers: dict[str, str],
        document: Document,
        extracted: OcrResult,
        indexed: DocumentIndex,
    ) -> None:
        for response in (
            api_client.get(index_url(document.id), headers=other_lawyer_headers),
            api_client.get(index_url(document.id, "/history"), headers=other_lawyer_headers),
            api_client.post(index_url(document.id, "/reindex"), headers=other_lawyer_headers),
        ):
            assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_the_denial_names_neither_permission_nor_role(
        self,
        api_client: TestClient,
        other_lawyer_headers: dict[str, str],
        document: Document,
        indexed: DocumentIndex,
    ) -> None:
        body = api_client.get(index_url(document.id), headers=other_lawyer_headers).json()
        rendered = str(body).lower()
        assert "indexing:" not in rendered
        assert "lawyer" not in rendered

    def test_the_index_is_exactly_as_reachable_as_the_document(
        self,
        api_client: TestClient,
        other_lawyer_headers: dict[str, str],
        document: Document,
        indexed: DocumentIndex,
    ) -> None:
        # The invariant the spec asks for, asserted as the identity it is.
        document_response = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}", headers=other_lawyer_headers
        )
        index_response = api_client.get(index_url(document.id), headers=other_lawyer_headers)
        assert index_response.status_code == document_response.status_code

    def test_the_index_is_exactly_as_reachable_as_the_extracted_text(
        self,
        api_client: TestClient,
        other_lawyer_headers: dict[str, str],
        document: Document,
        extracted: OcrResult,
        indexed: DocumentIndex,
    ) -> None:
        text_response = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/ocr/text", headers=other_lawyer_headers
        )
        index_response = api_client.get(index_url(document.id), headers=other_lawyer_headers)
        assert index_response.status_code == text_response.status_code


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class TestListing:
    @pytest.fixture(autouse=True)
    def _seed(
        self,
        make_document_index: MakeIndex,
        make_document: MakeDocument,
        make_case: MakeCase,
        document: Document,
        legal_case: Case,
        lawyer: User,
    ) -> None:
        # Two on the lawyer's case, two on a case they are not party to.
        #
        # `created_at` is spaced explicitly rather than left to the clock: the
        # default sort is by that column with the primary key as a tiebreaker, and
        # rows written in the same millisecond would order by a *random* UUID —
        # which is the flake the timeline feature already paid for once.
        base = datetime.now(UTC) - timedelta(hours=4)

        make_document_index(
            document_id=document.id, case_id=legal_case.id, created_at=base
        )
        make_document_index(
            document_id=document.id,
            case_id=legal_case.id,
            document_version=2,
            status=IndexStatus.FAILED,
            error_code="embedding_failure",
            created_at=base + timedelta(hours=1),
        )

        other_case = make_case(case_number="CASE-2026-9001")
        other_document = make_document(case_id=other_case.id)
        make_document_index(
            document_id=other_document.id,
            case_id=other_case.id,
            created_at=base + timedelta(hours=2),
        )
        make_document_index(
            document_id=other_document.id,
            case_id=other_case.id,
            document_version=2,
            embedding_model="legacy/old-model",
            created_at=base + timedelta(hours=3),
        )

    def test_an_administrator_sees_every_run(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(INDEXING_URL, headers=admin_headers).json()
        assert body["total_records"] == 4

    def test_a_lawyer_sees_only_their_own_cases(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        # The scope is applied in SQL, so `total_records` counts only what the
        # caller may access.
        body = api_client.get(INDEXING_URL, headers=lawyer_headers).json()
        assert body["total_records"] == 2

    def test_an_unassigned_lawyer_sees_nothing(
        self, api_client: TestClient, other_lawyer_headers: dict[str, str]
    ) -> None:
        body = api_client.get(INDEXING_URL, headers=other_lawyer_headers).json()
        assert body["total_records"] == 0

    def test_filters_combine(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        body = api_client.get(
            INDEXING_URL,
            headers=admin_headers,
            params={"status": "failed", "case_id": str(legal_case.id)},
        ).json()
        assert body["total_records"] == 1

    def test_the_embedding_model_filter_finds_the_stragglers(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # Changing the embedding model requires re-indexing everything; this is
        # how an operator finds the documents still on the old one.
        body = api_client.get(
            INDEXING_URL, headers=admin_headers, params={"embedding_model": "legacy/old-model"}
        ).json()
        assert body["total_records"] == 1

    def test_the_failure_cause_filter_works(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(
            INDEXING_URL, headers=admin_headers, params={"error_code": "embedding_failure"}
        ).json()
        assert body["total_records"] == 1

    def test_an_unknown_parameter_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            INDEXING_URL, headers=admin_headers, params={"nonsense": "yes"}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_oversized_page_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            INDEXING_URL, headers=admin_headers, params={"page_size": 500}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_page_past_the_end_is_empty_rather_than_an_error(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(INDEXING_URL, headers=admin_headers, params={"page": 50})
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"] == []

    def test_both_sort_directions_are_exact_reverses(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        ascending = api_client.get(
            INDEXING_URL,
            headers=admin_headers,
            params={"sort_by": "created_at", "sort_order": "asc"},
        ).json()["items"]
        descending = api_client.get(
            INDEXING_URL,
            headers=admin_headers,
            params={"sort_by": "created_at", "sort_order": "desc"},
        ).json()["items"]

        assert [item["id"] for item in ascending] == [
            item["id"] for item in reversed(descending)
        ]

    def test_the_list_carries_no_passage(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(INDEXING_URL, headers=admin_headers).json()
        for item in body["items"]:
            assert "text" not in item
            assert "chunks" not in item


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_it_reports_the_four_figures_the_spec_names(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_document_index: MakeIndex,
        document: Document,
        legal_case: Case,
    ) -> None:
        make_document_index(document_id=document.id, case_id=legal_case.id, chunk_count=12)
        make_document_index(
            document_id=document.id,
            case_id=legal_case.id,
            document_version=2,
            status=IndexStatus.FAILED,
            error_code="vector_store_unavailable",
        )

        body = api_client.get(METRICS_URL, headers=admin_headers).json()

        assert body["indexed"] == 1  # indexed documents
        assert body["total_chunks"] == 12  # indexed chunks
        assert body["average_duration_seconds"] == 4.2  # average duration
        assert body["failed"] == 1  # failures
        assert body["failures_by_code"] == {"vector_store_unavailable": 1}

    def test_the_rates_sum_to_one_hundred(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_document_index: MakeIndex,
        document: Document,
        legal_case: Case,
    ) -> None:
        make_document_index(document_id=document.id, case_id=legal_case.id)
        make_document_index(
            document_id=document.id,
            case_id=legal_case.id,
            document_version=2,
            status=IndexStatus.FAILED,
        )
        body = api_client.get(METRICS_URL, headers=admin_headers).json()
        assert body["success_rate"] + body["failure_rate"] == 100.0

    def test_it_reports_whether_the_pipeline_can_actually_run(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        embedder: FakeEmbedder,
        vector_store: InMemoryVectorStore,
    ) -> None:
        # The three things the counts cannot tell apart: no model, no vector
        # database, and nothing to index all show the same zeros.
        embedder.available = False
        vector_store.available = False

        body = api_client.get(METRICS_URL, headers=admin_headers).json()
        assert body["embedding_available"] is False
        assert body["vector_store_available"] is False
        assert body["stored_vectors"] is None

    def test_it_reports_the_configuration(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(METRICS_URL, headers=admin_headers).json()
        assert body["embedding_model"] == "fake/test-embedder"
        assert body["embedding_dimensions"] == 8
        assert body["chunker"] == "recursive-character"
        assert body["chunk_size"] == settings.INDEX_CHUNK_SIZE
        assert body["chunk_overlap"] == settings.INDEX_CHUNK_OVERLAP
        assert body["vector_collection"] == "test-chunks"

    def test_it_names_no_document_case_or_filename(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        indexed: DocumentIndex,
    ) -> None:
        rendered = api_client.get(METRICS_URL, headers=admin_headers).text
        assert str(document.id) not in rendered
        assert str(document.case_id) not in rendered
        assert "bail.pdf" not in rendered

    def test_it_is_refused_to_both_restricted_roles(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        representative_headers: dict[str, str],
    ) -> None:
        # `indexing:monitor` is administrative and deliberately not scoped to a
        # case.
        for headers in (lawyer_headers, representative_headers):
            assert (
                api_client.get(METRICS_URL, headers=headers).status_code
                == status.HTTP_403_FORBIDDEN
            )

    def test_a_zero_window_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            METRICS_URL, headers=admin_headers, params={"window_days": 0}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_the_window_restricts_the_figures(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_document_index: MakeIndex,
        document: Document,
        legal_case: Case,
    ) -> None:
        make_document_index(
            document_id=document.id,
            case_id=legal_case.id,
            created_at=datetime.now(UTC) - timedelta(days=40),
        )
        make_document_index(
            document_id=document.id, case_id=legal_case.id, document_version=2
        )

        recent = api_client.get(
            METRICS_URL, headers=admin_headers, params={"window_days": 7}
        ).json()
        everything = api_client.get(METRICS_URL, headers=admin_headers).json()
        assert recent["total_runs"] == 1
        assert everything["total_runs"] == 2


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


class TestTimeline:
    def test_the_case_timeline_carries_the_indexing_events(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]
        api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )

        timeline = api_client.get(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}/timeline",
            headers=admin_headers,
            params={"page_size": 50},
        ).json()
        types = {event["event_type"] for event in timeline["items"]}
        assert {"indexing_started", "indexing_completed"} <= types

    def test_the_events_are_categorised_as_document_events(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        # Reusing the five icon families rather than forcing a sixth into the
        # timeline's presentation.
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]
        api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )

        timeline = api_client.get(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}/timeline",
            headers=admin_headers,
            params={"page_size": 50},
        ).json()
        for event in timeline["items"]:
            if event["event_type"].startswith("indexing_"):
                assert event["category"] == "document"

    def test_the_headline_names_the_capability_not_the_machinery(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        ocr_engine: FakeOcrEngine,
    ) -> None:
        ocr_engine.pages = [ExtractedPage(page_number=1, text=PAGE_ONE, confidence=93.0)]
        api_client.post(
            UPLOAD_URL,
            headers=admin_headers,
            files={"file": ("bail.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id)},
        )

        timeline = api_client.get(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}/timeline",
            headers=admin_headers,
            params={"page_size": 50},
        ).json()
        titles = {
            event["title"]
            for event in timeline["items"]
            if event["event_type"].startswith("indexing_")
        }
        assert titles <= {"Search Indexing Started", "Search Indexing Completed"}
        assert not any("embedding" in title.lower() for title in titles)


# --------------------------------------------------------------------------- #
# Scope boundary and OpenAPI
# --------------------------------------------------------------------------- #


class TestScopeBoundary:
    def test_the_api_exposes_no_search_endpoint(self, api_client: TestClient) -> None:
        # ``10-document-indexing.md``: Semantic Search is out of scope, and
        # indexing must remain independent from retrieval.
        paths = api_client.get("/openapi.json").json()["paths"]
        for path in paths:
            assert "search" not in path.lower()
            assert "/query" not in path.lower()

    def test_the_indexing_routes_are_exactly_the_five_documented(
        self, api_client: TestClient
    ) -> None:
        paths = api_client.get("/openapi.json").json()["paths"]
        indexing_paths = {
            path
            for path, operations in paths.items()
            if any("indexing" in operation.get("tags", []) for operation in operations.values())
        }
        assert indexing_paths == {
            f"{settings.API_V1_PREFIX}/indexing",
            f"{settings.API_V1_PREFIX}/indexing/metrics",
            f"{settings.API_V1_PREFIX}/documents/{{document_id}}/index",
            f"{settings.API_V1_PREFIX}/documents/{{document_id}}/index/history",
            f"{settings.API_V1_PREFIX}/documents/{{document_id}}/index/reindex",
        }

    def test_the_status_route_is_not_shadowed_by_the_document_routes(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        document: Document,
        indexed: DocumentIndex,
    ) -> None:
        # Registered after the document router; this asserts the ordering did not
        # cost `/documents/{id}` its place, nor `/documents/{id}/index` its own.
        assert (
            api_client.get(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers).status_code
            == status.HTTP_200_OK
        )
        assert (
            api_client.get(index_url(document.id), headers=admin_headers).status_code
            == status.HTTP_200_OK
        )


class TestOpenApi:
    def test_the_reindex_endpoint_documents_its_failures(
        self, api_client: TestClient
    ) -> None:
        schema = api_client.get("/openapi.json").json()
        operation = schema["paths"][
            f"{settings.API_V1_PREFIX}/documents/{{document_id}}/index/reindex"
        ]["post"]

        assert operation["summary"]
        for code in ("401", "403", "404", "409", "422", "503"):
            assert code in operation["responses"], code

    def test_both_prefixes_are_tagged_indexing(self, api_client: TestClient) -> None:
        paths = api_client.get("/openapi.json").json()["paths"]
        assert paths[f"{settings.API_V1_PREFIX}/indexing"]["get"]["tags"] == ["indexing"]
        assert paths[f"{settings.API_V1_PREFIX}/documents/{{document_id}}/index"]["get"][
            "tags"
        ] == ["indexing"]


class TestDependencyWiring:
    def test_the_application_wires_a_real_queue_embedder_and_store(self) -> None:
        from api.deps import (
            get_chunker_dependency,
            get_embedder_dependency,
            get_index_job_queue,
            get_vector_store_dependency,
        )
        from services.chunking import RecursiveCharacterChunker
        from services.embedding import SentenceTransformerEmbedder
        from services.job_queue import ThreadPoolJobQueue
        from services.vector_store import QdrantVectorStore

        # `IndexingService` defaults to a queue that schedules nothing, so it can
        # be built in a context with no background worker. This asserts the
        # *application* never takes that default — if it did, every document
        # would sit at `pending` forever while every unit test still passed.
        assert isinstance(get_index_job_queue(), ThreadPoolJobQueue)
        assert isinstance(get_embedder_dependency(), SentenceTransformerEmbedder)
        assert isinstance(get_vector_store_dependency(), QdrantVectorStore)
        assert isinstance(get_chunker_dependency(), RecursiveCharacterChunker)

    def test_the_ocr_service_schedules_through_the_indexing_service(
        self, db_session: Any, document_storage: Any
    ) -> None:
        from api.deps import get_indexing_service, get_ocr_service
        from repositories.case import CaseRepository
        from repositories.document import DocumentRepository
        from repositories.indexing import IndexingRepository
        from repositories.ocr import OcrRepository
        from repositories.timeline import TimelineRepository
        from services.chunking import get_chunker
        from services.embedding import get_embedder
        from services.indexing import IndexingService, IndexJob
        from services.job_queue import NullJobQueue
        from services.ocr_engine import get_ocr_engine
        from services.ocr_queue import NullOcrJobQueue
        from services.timeline import TimelineService
        from services.vector_store import get_vector_store

        documents = DocumentRepository(db_session)
        results = OcrRepository(db_session)
        timeline = TimelineService(TimelineRepository(db_session), CaseRepository(db_session))

        indexing = get_indexing_service(
            IndexingRepository(db_session),
            documents,
            results,
            get_chunker(),
            get_embedder(),
            get_vector_store(),
            NullJobQueue[IndexJob](name="indexing"),
            timeline,
        )
        ocr = get_ocr_service(
            results,
            documents,
            document_storage,
            get_ocr_engine(),
            NullOcrJobQueue(),
            timeline,
            indexing,
        )

        assert isinstance(ocr._indexing, IndexingService)
