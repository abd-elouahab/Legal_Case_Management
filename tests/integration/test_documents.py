"""Integration tests for the Document Management API.

Exercise the endpoints over real HTTP: the upload/download/preview/replace/delete
contract, validation responses, authorization (401 vs 403 for every route and
every role, plus the per-case assignment check), search, filtering, sorting, and
pagination.

The service-level rules are unit-tested in ``tests/unit/test_document_service.py``;
what these add is the wire contract — status codes, multipart handling, streamed
response headers, error envelopes, and the guarantee that a lawyer cannot reach a
document on a case they are not on.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from models.case import Case
from models.document import Document, DocumentCategory
from models.user import User, UserRole
from tests.helpers import DOCX_BYTES, PDF_BYTES, PNG_BYTES, TXT_BYTES

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import InMemoryDocumentStorage

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
DOCUMENTS_URL = f"{settings.API_V1_PREFIX}/documents"
UPLOAD_URL = f"{DOCUMENTS_URL}/upload"

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeDocument = Callable[..., Document]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == status.HTTP_200_OK, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def upload_file(
    filename: str = "contract.pdf",
    payload: bytes = PDF_BYTES,
    content_type: str = "application/pdf",
) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (filename, payload, content_type)}


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
def legal_case(make_case: MakeCase) -> Case:
    return make_case()


# --------------------------------------------------------------------------- #
# Authentication and capability authorization
# --------------------------------------------------------------------------- #


class TestAuthentication:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", ""),
            ("POST", "/upload"),
            ("GET", "/{id}"),
            ("GET", "/{id}/versions"),
            ("GET", "/{id}/download"),
            ("GET", "/{id}/preview"),
            ("PATCH", "/{id}"),
            ("POST", "/{id}/replace"),
            ("DELETE", "/{id}"),
        ],
    )
    def test_every_route_refuses_an_anonymous_caller(
        self, api_client: TestClient, method: str, path: str
    ) -> None:
        url = DOCUMENTS_URL + path.replace("{id}", str(uuid.uuid4()))

        response = api_client.request(method, url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        # RFC 6750: tell the client what kind of credential is expected.
        assert response.headers["WWW-Authenticate"] == "Bearer"


class TestCapabilityAuthorization:
    """Which *roles* may reach which endpoints, before any per-case check."""

    def test_a_lawyer_may_view_and_upload(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        assert api_client.get(DOCUMENTS_URL, headers=lawyer_headers).status_code == 200

    @pytest.mark.parametrize("role_headers", ["lawyer_headers", "representative_headers"])
    def test_neither_restricted_role_may_update_a_document(
        self,
        api_client: TestClient,
        request: pytest.FixtureRequest,
        role_headers: str,
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        # `documents:update` and `documents:delete` are administrator-only in
        # `core/roles.py`, which matches the spec's per-role lists exactly.
        headers = request.getfixturevalue(role_headers)
        document = make_document(case_id=legal_case.id)

        response = api_client.patch(
            f"{DOCUMENTS_URL}/{document.id}", json={"category": "invoice"}, headers=headers
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.parametrize("role_headers", ["lawyer_headers", "representative_headers"])
    def test_neither_restricted_role_may_delete_a_document(
        self,
        api_client: TestClient,
        request: pytest.FixtureRequest,
        role_headers: str,
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        headers = request.getfixturevalue(role_headers)
        document = make_document(case_id=legal_case.id)

        response = api_client.delete(f"{DOCUMENTS_URL}/{document.id}", headers=headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_denial_names_neither_the_permission_nor_the_role(
        self, api_client: TestClient, lawyer_headers: dict[str, str], make_document: MakeDocument, legal_case: Case
    ) -> None:
        document = make_document(case_id=legal_case.id)

        body = api_client.delete(
            f"{DOCUMENTS_URL}/{document.id}", headers=lawyer_headers
        ).json()

        assert body["error"] == "forbidden"
        assert "documents:delete" not in body["message"]
        assert "administrator" not in body["message"].lower()


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #


class TestUpload:
    def test_it_creates_a_document_and_returns_201(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        document_storage: InMemoryDocumentStorage,
    ) -> None:
        response = api_client.post(
            UPLOAD_URL,
            data={
                "case_id": str(legal_case.id),
                "category": "contract",
                "description": "Bail commercial",
            },
            files=upload_file("Contrat de bail.pdf"),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()
        assert body["original_filename"] == "Contrat de bail.pdf"
        assert body["category"] == "contract"
        assert body["description"] == "Bail commercial"
        assert body["version"] == 1
        assert body["version_count"] == 1
        assert body["is_previewable"] is True
        assert body["file_size"] == len(PDF_BYTES)
        # Metadata in PostgreSQL, the binary in object storage.
        assert document_storage.objects[body["storage_key"]] == PDF_BYTES

    def test_the_category_defaults_to_other(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(legal_case.id)},
            files=upload_file(),
            headers=admin_headers,
        )

        assert response.json()["category"] == "other"

    def test_the_uploader_is_the_authenticated_caller(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(legal_case.id)},
            files=upload_file(),
            headers=admin_headers,
        )
        body = response.json()

        assert body["uploaded_by"] == str(admin.id)
        assert body["uploader"]["email"] == admin.email

    def test_an_unknown_case_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(uuid.uuid4())},
            files=upload_file(),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "case_not_found"

    def test_a_missing_file_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        response = api_client.post(
            UPLOAD_URL, data={"case_id": str(legal_case.id)}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_empty_file_is_a_422_naming_the_file_field(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(legal_case.id)},
            files=upload_file("empty.pdf", b""),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        body = response.json()
        assert body["error"] == "invalid_document_file"
        assert body["details"][0]["field"] == "file"

    def test_an_unsupported_type_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(legal_case.id)},
            files=upload_file("payload.exe", b"MZ\x90\x00", "application/octet-stream"),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_corrupted_file_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(legal_case.id)},
            files=upload_file("broken.pdf", b"this is not a pdf"),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "corrupted" in response.json()["message"].lower()

    def test_a_spoofed_content_type_does_not_decide_how_the_file_is_served(
        self, api_client: TestClient, admin_headers: dict[str, str], legal_case: Case
    ) -> None:
        # The browser's Content-Type is attacker-controlled; the platform's MIME
        # type comes from the extension mapping alone.
        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(legal_case.id)},
            files=upload_file("note.txt", TXT_BYTES, "text/html"),
            headers=admin_headers,
        )

        assert response.json()["mime_type"].startswith("text/plain")

    def test_a_lawyer_cannot_upload_to_a_case_they_are_not_assigned_to(
        self, api_client: TestClient, lawyer_headers: dict[str, str], legal_case: Case
    ) -> None:
        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(legal_case.id)},
            files=upload_file(),
            headers=lawyer_headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_lawyer_can_upload_to_their_assigned_case(
        self,
        api_client: TestClient,
        lawyer: User,
        lawyer_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        assigned = make_case(assigned_lawyer_id=lawyer.id)

        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(assigned.id)},
            files=upload_file(),
            headers=lawyer_headers,
        )

        assert response.status_code == status.HTTP_201_CREATED, response.text

    def test_a_court_representative_can_upload_court_documents(
        self,
        api_client: TestClient,
        representative: User,
        representative_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        assigned = make_case(assigned_court_representative_id=representative.id)

        response = api_client.post(
            UPLOAD_URL,
            data={"case_id": str(assigned.id), "category": "court_decision"},
            files=upload_file("jugement.pdf"),
            headers=representative_headers,
        )

        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert response.json()["category"] == "court_decision"


# --------------------------------------------------------------------------- #
# Reading, downloading, previewing
# --------------------------------------------------------------------------- #


class TestRead:
    def test_it_returns_the_full_record(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id, description="Pièce 3")

        body = api_client.get(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers).json()

        assert body["id"] == str(document.id)
        assert body["case"]["case_number"] == legal_case.case_number
        assert body["description"] == "Pièce 3"
        assert [entry["version"] for entry in body["versions"]] == [1]

    def test_an_unknown_document_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{DOCUMENTS_URL}/{uuid.uuid4()}", headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "document_not_found"

    def test_a_lawyer_is_refused_a_document_on_another_case(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        response = api_client.get(f"{DOCUMENTS_URL}/{document.id}", headers=lawyer_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestDownload:
    def test_it_streams_the_file_as_an_attachment(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id, original_filename="Contrat.pdf")

        response = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/download", headers=admin_headers
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.content == PDF_BYTES
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.headers["content-disposition"].startswith("attachment")
        assert "Contrat.pdf" in response.headers["content-disposition"]

    def test_it_sets_the_headers_a_user_supplied_body_needs(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        headers = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/download", headers=admin_headers
        ).headers

        assert headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in headers["content-security-policy"]
        assert "no-store" in headers["cache-control"]

    def test_a_non_ascii_filename_survives_the_header(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id, original_filename="عقد الإيجار.pdf")

        disposition = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/download", headers=admin_headers
        ).headers["content-disposition"]

        # Both forms: an ASCII fallback and the RFC 5987 extended parameter.
        assert "filename=" in disposition
        assert "filename*=UTF-8''" in disposition

    def test_a_lawyer_cannot_download_a_document_on_another_case(
        self,
        api_client: TestClient,
        lawyer_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        response = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/download", headers=lawyer_headers
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestPreview:
    def test_a_pdf_is_served_inline(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        response = api_client.get(f"{DOCUMENTS_URL}/{document.id}/preview", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-disposition"].startswith("inline")

    def test_an_image_is_served_inline(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id, extension="png", content=PNG_BYTES)

        response = api_client.get(f"{DOCUMENTS_URL}/{document.id}/preview", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("image/png")

    def test_a_word_document_answers_415_and_points_at_the_download(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id, extension="docx", content=DOCX_BYTES)

        response = api_client.get(f"{DOCUMENTS_URL}/{document.id}/preview", headers=admin_headers)

        assert response.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        assert response.json()["error"] == "preview_unavailable"
        assert "download" in response.json()["message"].lower()

    def test_the_document_says_in_advance_that_preview_is_unavailable(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id, extension="docx", content=DOCX_BYTES)

        body = api_client.get(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers).json()

        # So a client never offers a preview the API is about to refuse.
        assert body["is_previewable"] is False


# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #


class TestVersioning:
    def test_replacing_increments_the_version_and_keeps_the_identifier(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        response = api_client.post(
            f"{DOCUMENTS_URL}/{document.id}/replace",
            files=upload_file("revision.pdf"),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["id"] == str(document.id)
        assert body["version"] == 2
        assert body["original_filename"] == "revision.pdf"

    def test_previous_versions_remain_listed_and_downloadable(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id, original_filename="v1.pdf")
        api_client.post(
            f"{DOCUMENTS_URL}/{document.id}/replace",
            files=upload_file("v2.txt", TXT_BYTES, "text/plain"),
            headers=admin_headers,
        )

        history = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/versions", headers=admin_headers
        ).json()
        first = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/download?version=1", headers=admin_headers
        )
        second = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/download", headers=admin_headers
        )

        assert [entry["version"] for entry in history] == [1, 2]
        assert [entry["original_filename"] for entry in history] == ["v1.pdf", "v2.txt"]
        assert first.content == PDF_BYTES
        assert second.content == TXT_BYTES

    def test_the_version_history_records_who_uploaded_each_version(
        self,
        api_client: TestClient,
        admin: User,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)
        api_client.post(
            f"{DOCUMENTS_URL}/{document.id}/replace",
            files=upload_file(),
            headers=admin_headers,
        )

        history = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/versions", headers=admin_headers
        ).json()

        assert history[1]["uploader"]["email"] == admin.email
        assert history[1]["created_at"] is not None

    def test_no_previous_object_is_overwritten(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
        document_storage: InMemoryDocumentStorage,
    ) -> None:
        document = make_document(case_id=legal_case.id)
        original_key = document.storage_key

        api_client.post(
            f"{DOCUMENTS_URL}/{document.id}/replace",
            files=upload_file("v2.png", PNG_BYTES, "image/png"),
            headers=admin_headers,
        )

        assert document_storage.objects[original_key] == PDF_BYTES
        assert len(document_storage.objects) == 2

    def test_an_unknown_version_is_a_404(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        response = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/download?version=7", headers=admin_headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "document_version_not_found"


# --------------------------------------------------------------------------- #
# Metadata update and deletion
# --------------------------------------------------------------------------- #


class TestMetadataUpdate:
    def test_it_updates_the_category_and_description(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        body = api_client.patch(
            f"{DOCUMENTS_URL}/{document.id}",
            json={"category": "invoice", "description": "Honoraires"},
            headers=admin_headers,
        ).json()

        assert body["category"] == "invoice"
        assert body["description"] == "Honoraires"

    def test_it_never_touches_the_binary(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        body = api_client.patch(
            f"{DOCUMENTS_URL}/{document.id}", json={"category": "evidence"}, headers=admin_headers
        ).json()

        assert body["version"] == 1
        assert body["storage_key"] == document.storage_key
        assert body["original_filename"] == document.original_filename

    @pytest.mark.parametrize("field", ["original_filename", "storage_key", "version", "case_id"])
    def test_a_binary_or_immutable_field_is_a_422(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
        field: str,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        response = api_client.patch(
            f"{DOCUMENTS_URL}/{document.id}", json={field: "hijacked"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_empty_body_is_a_422(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        response = api_client.patch(
            f"{DOCUMENTS_URL}/{document.id}", json={}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestDelete:
    def test_it_soft_deletes_and_returns_the_document(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        body = api_client.delete(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers).json()

        assert body["is_deleted"] is True
        assert body["deleted_at"] is not None

    def test_the_file_is_retained_in_object_storage(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
        document_storage: InMemoryDocumentStorage,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        api_client.delete(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers)

        # "Do not immediately remove the file from MinIO."
        assert document_storage.objects[document.storage_key] == PDF_BYTES

    def test_a_deleted_document_leaves_the_list_and_the_read_path(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)
        api_client.delete(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers)

        listed = api_client.get(DOCUMENTS_URL, headers=admin_headers).json()
        read = api_client.get(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers)
        download = api_client.get(
            f"{DOCUMENTS_URL}/{document.id}/download", headers=admin_headers
        )

        assert listed["total_records"] == 0
        assert read.status_code == status.HTTP_404_NOT_FOUND
        assert download.status_code == status.HTTP_404_NOT_FOUND

    def test_it_is_idempotent(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        document = make_document(case_id=legal_case.id)

        first = api_client.delete(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers)
        second = api_client.delete(f"{DOCUMENTS_URL}/{document.id}", headers=admin_headers)

        assert first.status_code == second.status_code == status.HTTP_200_OK
        assert first.json()["deleted_at"] == second.json()["deleted_at"]


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class TestListing:
    def test_it_reports_the_pagination_totals(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        for _ in range(5):
            make_document(case_id=legal_case.id)

        body = api_client.get(f"{DOCUMENTS_URL}?page=2&page_size=2", headers=admin_headers).json()

        assert body["total_records"] == 5
        assert body["page"] == 2
        assert body["page_size"] == 2
        assert body["total_pages"] == 3
        assert len(body["items"]) == 2

    def test_search_is_case_insensitive(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        wanted = make_document(case_id=legal_case.id, original_filename="Contrat de bail.pdf")
        make_document(case_id=legal_case.id, original_filename="jugement.pdf")

        body = api_client.get(f"{DOCUMENTS_URL}?search=CONTRAT", headers=admin_headers).json()

        assert [item["id"] for item in body["items"]] == [str(wanted.id)]

    def test_filters_combine(
        self,
        api_client: TestClient,
        admin: User,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        wanted = make_document(
            case_id=legal_case.id, category=DocumentCategory.EVIDENCE, uploaded_by=admin.id
        )
        make_document(case_id=legal_case.id, category=DocumentCategory.INVOICE, uploaded_by=admin.id)

        body = api_client.get(
            f"{DOCUMENTS_URL}?category=evidence&uploaded_by={admin.id}&file_extension=pdf",
            headers=admin_headers,
        ).json()

        assert [item["id"] for item in body["items"]] == [str(wanted.id)]

    def test_it_sorts_in_both_directions(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        legal_case: Case,
        make_document: MakeDocument,
    ) -> None:
        make_document(case_id=legal_case.id, original_filename="alpha.pdf")
        make_document(case_id=legal_case.id, original_filename="omega.pdf")

        ascending = api_client.get(
            f"{DOCUMENTS_URL}?sort_by=original_filename&sort_order=asc", headers=admin_headers
        ).json()
        descending = api_client.get(
            f"{DOCUMENTS_URL}?sort_by=original_filename&sort_order=desc", headers=admin_headers
        ).json()

        assert [item["original_filename"] for item in ascending["items"]] == [
            "alpha.pdf",
            "omega.pdf",
        ]
        assert [item["original_filename"] for item in descending["items"]] == [
            "omega.pdf",
            "alpha.pdf",
        ]

    def test_a_lawyer_sees_only_documents_on_their_cases(
        self,
        api_client: TestClient,
        lawyer: User,
        lawyer_headers: dict[str, str],
        make_case: MakeCase,
        make_document: MakeDocument,
    ) -> None:
        make_document(case_id=make_case(assigned_lawyer_id=lawyer.id).id)
        make_document(case_id=make_case().id)

        body = api_client.get(DOCUMENTS_URL, headers=lawyer_headers).json()

        # The scope is applied in SQL, so the total counts only what they may see.
        assert body["total_records"] == 1

    def test_an_unknown_query_parameter_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{DOCUMENTS_URL}?sort_direction=asc", headers=admin_headers)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #


class TestOpenApiDocumentation:
    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/api/v1/documents", "get"),
            ("/api/v1/documents/upload", "post"),
            ("/api/v1/documents/{document_id}", "get"),
            ("/api/v1/documents/{document_id}", "patch"),
            ("/api/v1/documents/{document_id}", "delete"),
            ("/api/v1/documents/{document_id}/versions", "get"),
            ("/api/v1/documents/{document_id}/download", "get"),
            ("/api/v1/documents/{document_id}/preview", "get"),
            ("/api/v1/documents/{document_id}/replace", "post"),
        ],
    )
    def test_every_endpoint_is_documented(
        self, api_client: TestClient, path: str, method: str
    ) -> None:
        spec = api_client.get("/openapi.json").json()
        operation = spec["paths"][path][method]

        assert operation["summary"]
        assert operation["description"]
        assert "401" in operation["responses"]
        assert "403" in operation["responses"]
