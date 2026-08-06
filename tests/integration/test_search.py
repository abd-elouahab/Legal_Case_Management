"""Integration tests for the Semantic Search API.

Exercise the endpoints over real HTTP: the search contract, metadata filtering,
pagination, ranking, authorization (401 vs 403 for every route and every role,
plus the per-case scope), the monitoring view, and the two guarantees the spec
leads with — **a caller retrieves only passages of documents they could already
open**, and **retrieval never generates an answer**.

The corpus is built by the *real* indexing pipeline: a document is uploaded,
extracted, chunked, embedded, and stored, and only then searched. So a passage
returned here travelled the whole way from an uploaded file, which is what makes
these tests about the platform rather than about a fixture.

The service-level rules are unit-tested in ``tests/unit/test_search_service.py``;
what these add is the wire contract — status codes, the response shape a client
renders, error envelopes, and the assurance that **no field on the wire carries an
answer, a summary, a prompt, a vector, or a passage the caller may not read**.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from models.case import Case
from models.document import Document, DocumentCategory
from models.ocr import OcrResult
from models.user import User, UserRole
from services.embedding import EmbeddingError
from services.vector_search import VectorSearchError

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import FakeEmbedder, InMemoryVectorSearcher

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
SEARCH_URL = f"{settings.API_V1_PREFIX}/search"
METRICS_URL = f"{SEARCH_URL}/metrics"

FRENCH_PAGE = (
    "CONTRAT DE BAIL COMMERCIAL. Article 4 : Loyer et charges. Le loyer mensuel est "
    "payable d'avance le premier jour de chaque mois, au domicile du bailleur. Toute "
    "résiliation anticipée doit être notifiée par écrit avec un préavis de trois mois."
)
ARABIC_PAGE = (
    "عقد كراء تجاري. المادة الرابعة: الكراء والتحملات. يؤدى الكراء الشهري مسبقا في "
    "اليوم الأول من كل شهر بمقر المكري، ويجب إشعار الطرف الآخر كتابة قبل ثلاثة أشهر."
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


# --------------------------------------------------------------------------- #
# Actors and corpus
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(
        email="search-admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR
    )


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(
        email="search-lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER
    )


@pytest.fixture
def outsider(make_user: MakeUser) -> User:
    return make_user(
        email="search-outsider@example.com", password=PASSWORD, role=UserRole.LAWYER
    )


@pytest.fixture
def court(make_user: MakeUser) -> User:
    return make_user(
        email="search-court@example.com",
        password=PASSWORD,
        role=UserRole.COURT_REPRESENTATIVE,
    )


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User, court: User) -> Case:
    return make_case(
        assigned_lawyer_id=lawyer.id,
        assigned_court_representative_id=court.id,
    )


@pytest.fixture
def other_case(make_case: MakeCase, outsider: User) -> Case:
    return make_case(assigned_lawyer_id=outsider.id)


@pytest.fixture
def index_document(indexing_service, make_ocr_result: MakeOcrResult):  # type: ignore[no-untyped-def]
    """Push a document through the real indexing pipeline into the vector store."""

    def _index(document: Document, pages: list[str]) -> None:
        indexing_service.schedule_for_ocr_result(
            make_ocr_result(document_id=document.id, pages=pages)
        )

    return _index


@pytest.fixture
def french_contract(  # type: ignore[no-untyped-def]
    make_document: MakeDocument, legal_case: Case, index_document
) -> Document:
    document = make_document(
        case_id=legal_case.id,
        original_filename="bail-commercial.pdf",
        category=DocumentCategory.CONTRACT,
    )
    index_document(document, [FRENCH_PAGE])
    return document


@pytest.fixture
def arabic_evidence(  # type: ignore[no-untyped-def]
    make_document: MakeDocument, legal_case: Case, index_document
) -> Document:
    document = make_document(
        case_id=legal_case.id,
        original_filename="عقد-كراء.pdf",
        category=DocumentCategory.EVIDENCE,
    )
    index_document(document, [ARABIC_PAGE])
    return document


@pytest.fixture
def foreign_document(  # type: ignore[no-untyped-def]
    make_document: MakeDocument, other_case: Case, index_document
) -> Document:
    """An indexed document on a case our lawyer is not party to."""
    document = make_document(case_id=other_case.id, original_filename="autre.pdf")
    index_document(document, [FRENCH_PAGE])
    return document


def search(
    client: TestClient, token: str, query: str = "loyer payable d'avance", **body: Any
) -> Any:
    return client.post(SEARCH_URL, json={"query": query, **body}, headers=bearer(token))


# --------------------------------------------------------------------------- #
# Authentication and capability
# --------------------------------------------------------------------------- #


class TestAuthentication:
    def test_search_requires_authentication(self, api_client: TestClient) -> None:
        response = api_client.post(SEARCH_URL, json={"query": "loyer"})

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_metrics_requires_authentication(self, api_client: TestClient) -> None:
        response = api_client.get(METRICS_URL)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_metrics_is_refused_to_a_lawyer_and_a_court_representative(
        self, api_client: TestClient, lawyer: User, court: User
    ) -> None:
        """`search:monitor` is administrative and deliberately not case-scoped."""
        for user in (lawyer, court):
            response = api_client.get(
                METRICS_URL, headers=bearer(token_for(api_client, user.email))
            )
            assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_denial_names_neither_permission_nor_role(
        self, api_client: TestClient, lawyer: User
    ) -> None:
        response = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, lawyer.email))
        )
        body = response.text.lower()

        assert "search:monitor" not in body
        assert "lawyer" not in body


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


class TestSearch:
    def test_a_search_returns_the_passage_and_its_provenance(
        self,
        api_client: TestClient,
        lawyer: User,
        legal_case: Case,
        french_contract: Document,
    ) -> None:
        response = search(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["result_count"] >= 1
        assert body["is_empty"] is False

        result = body["results"][0]
        assert result["document_id"] == str(french_contract.id)
        assert result["case_id"] == str(legal_case.id)
        assert result["document_version"] == 1
        assert result["page_number"] == 1
        assert result["chunk_number"] == 0
        assert result["rank"] == 1
        assert result["text"]
        assert result["text"] in FRENCH_PAGE
        assert -1.0 <= result["score"] <= 1.0
        assert result["document"]["original_filename"] == "bail-commercial.pdf"

    def test_the_response_reports_relevance_and_timing(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        body = search(api_client, token_for(api_client, lawyer.email)).json()

        assert body["top_score"] is not None
        assert body["average_score"] is not None
        assert body["duration_ms"] >= 0
        assert body["limit"] == settings.SEARCH_DEFAULT_LIMIT
        assert body["offset"] == 0

    def test_the_normalized_query_is_echoed_back(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        body = search(api_client, token_for(api_client, lawyer.email), "  loyer   ").json()

        assert body["query"] == "loyer"

    def test_an_empty_corpus_answers_200_with_no_results(
        self, api_client: TestClient, lawyer: User, legal_case: Case
    ) -> None:
        """Nothing matched is an answer, not a 404."""
        response = search(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["results"] == []
        assert body["is_empty"] is True
        assert body["top_score"] is None

    def test_results_are_ranked_by_similarity(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
    ) -> None:
        body = search(api_client, token_for(api_client, lawyer.email)).json()

        scores = [result["score"] for result in body["results"]]
        ranks = [result["rank"] for result in body["results"]]

        assert scores == sorted(scores, reverse=True)
        assert ranks == list(range(1, len(ranks) + 1))

    def test_an_exact_passage_query_ranks_that_passage_first(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
        vector_store,  # type: ignore[no-untyped-def]
    ) -> None:
        passage = next(
            point.payload["text"]
            for point in vector_store.points.values()
            if point.payload["document_id"] == str(french_contract.id)
        )

        body = search(api_client, token_for(api_client, lawyer.email), passage).json()

        assert body["results"][0]["text"] == passage
        assert body["results"][0]["document_id"] == str(french_contract.id)

    def test_an_arabic_query_is_accepted(
        self, api_client: TestClient, lawyer: User, arabic_evidence: Document
    ) -> None:
        body = search(api_client, token_for(api_client, lawyer.email), ARABIC_PAGE).json()

        assert body["results"]
        assert body["results"][0]["language"] == "ar"

    def test_pagination_pages_through_the_result_set(
        self,
        api_client: TestClient,
        lawyer: User,
        legal_case: Case,
        make_document: MakeDocument,
        index_document,  # type: ignore[no-untyped-def]
    ) -> None:
        for number in range(4):
            index_document(
                make_document(case_id=legal_case.id), [f"{FRENCH_PAGE} Variante {number}."]
            )

        token = token_for(api_client, lawyer.email)
        first = search(api_client, token, limit=2, offset=0).json()
        second = search(api_client, token, limit=2, offset=2).json()

        assert first["has_more"] is True
        assert len(first["results"]) == 2
        assert len(second["results"]) == 2
        assert {result["text"] for result in first["results"]}.isdisjoint(
            {result["text"] for result in second["results"]}
        )


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuthorization:
    def test_a_lawyer_retrieves_only_their_own_cases_passages(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        foreign_document: Document,
    ) -> None:
        body = search(api_client, token_for(api_client, lawyer.email)).json()

        assert {result["document_id"] for result in body["results"]} == {
            str(french_contract.id)
        }

    def test_an_outsider_retrieves_nothing_from_a_case_they_are_not_on(
        self,
        api_client: TestClient,
        outsider: User,
        french_contract: Document,
    ) -> None:
        body = search(api_client, token_for(api_client, outsider.email)).json()

        assert body["results"] == []

    def test_an_administrator_retrieves_across_cases(
        self,
        api_client: TestClient,
        admin: User,
        french_contract: Document,
        foreign_document: Document,
    ) -> None:
        body = search(api_client, token_for(api_client, admin.email)).json()

        assert {result["document_id"] for result in body["results"]} == {
            str(french_contract.id),
            str(foreign_document.id),
        }

    def test_a_court_representative_may_search_their_own_cases(
        self, api_client: TestClient, court: User, french_contract: Document
    ) -> None:
        """They already read the full extracted text of these documents.

        Withholding search would leave them able to read every page of a filing
        but not to find a clause in it.
        """
        response = search(api_client, token_for(api_client, court.email))

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"]

    def test_filtering_by_an_inaccessible_case_is_403_not_an_empty_page(
        self, api_client: TestClient, lawyer: User, other_case: Case
    ) -> None:
        """An inaccessible matter must not be indistinguishable from a quiet one."""
        response = search(
            api_client,
            token_for(api_client, lawyer.email),
            filters={"case_id": str(other_case.id)},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_filtering_by_an_inaccessible_document_is_403(
        self, api_client: TestClient, lawyer: User, foreign_document: Document
    ) -> None:
        response = search(
            api_client,
            token_for(api_client, lawyer.email),
            filters={"document_id": str(foreign_document.id)},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_search_is_no_more_reachable_than_the_document_itself(
        self,
        api_client: TestClient,
        lawyer: User,
        outsider: User,
        french_contract: Document,
    ) -> None:
        """The chain search → document → case, asserted as the identity it is."""
        token = token_for(api_client, outsider.email)
        document_url = f"{settings.API_V1_PREFIX}/documents/{french_contract.id}"

        document_response = api_client.get(document_url, headers=bearer(token))
        search_response = search(
            api_client, token, filters={"document_id": str(french_contract.id)}
        )

        assert document_response.status_code == search_response.status_code

    def test_a_deleted_document_stops_being_searchable(
        self,
        api_client: TestClient,
        admin: User,
        lawyer: User,
        french_contract: Document,
    ) -> None:
        """Deletion is logical, so the vectors outlive it — and must stop surfacing."""
        token = token_for(api_client, lawyer.email)
        assert search(api_client, token).json()["results"]

        deleted = api_client.delete(
            f"{settings.API_V1_PREFIX}/documents/{french_contract.id}",
            headers=bearer(token_for(api_client, admin.email)),
        )
        assert deleted.status_code in {status.HTTP_200_OK, status.HTTP_204_NO_CONTENT}

        assert search(api_client, token).json()["results"] == []


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


class TestFiltering:
    def test_filtering_by_case(
        self,
        api_client: TestClient,
        admin: User,
        legal_case: Case,
        french_contract: Document,
        foreign_document: Document,
    ) -> None:
        body = search(
            api_client,
            token_for(api_client, admin.email),
            filters={"case_id": str(legal_case.id)},
        ).json()

        assert {result["document_id"] for result in body["results"]} == {
            str(french_contract.id)
        }

    def test_filtering_by_document(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
    ) -> None:
        body = search(
            api_client,
            token_for(api_client, lawyer.email),
            filters={"document_id": str(arabic_evidence.id)},
        ).json()

        assert {result["document_id"] for result in body["results"]} == {
            str(arabic_evidence.id)
        }

    def test_filtering_by_language(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
    ) -> None:
        body = search(
            api_client, token_for(api_client, lawyer.email), filters={"languages": ["ar"]}
        ).json()

        assert body["results"]
        assert {result["language"] for result in body["results"]} == {"ar"}

    def test_filtering_by_category(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        arabic_evidence: Document,
    ) -> None:
        body = search(
            api_client, token_for(api_client, lawyer.email), filters={"categories": ["contract"]}
        ).json()

        assert {result["document_id"] for result in body["results"]} == {
            str(french_contract.id)
        }

    def test_filtering_by_file_type(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        token = token_for(api_client, lawyer.email)

        assert search(api_client, token, filters={"file_types": ["pdf"]}).json()["results"]
        assert search(api_client, token, filters={"file_types": ["docx"]}).json()["results"] == []

    def test_filtering_by_version(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        token = token_for(api_client, lawyer.email)

        assert search(api_client, token, filters={"document_version": 1}).json()["results"]
        assert search(api_client, token, filters={"document_version": 9}).json()["results"] == []

    def test_filters_combine_with_and(
        self,
        api_client: TestClient,
        lawyer: User,
        legal_case: Case,
        french_contract: Document,
        arabic_evidence: Document,
    ) -> None:
        body = search(
            api_client,
            token_for(api_client, lawyer.email),
            filters={"case_id": str(legal_case.id), "languages": ["ar"]},
        ).json()

        assert {result["document_id"] for result in body["results"]} == {
            str(arabic_evidence.id)
        }

    def test_a_filter_naming_a_missing_case_is_404(
        self, api_client: TestClient, admin: User
    ) -> None:
        response = search(
            api_client,
            token_for(api_client, admin.email),
            filters={"case_id": str(uuid.uuid4())},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class TestValidation:
    @pytest.mark.parametrize("query", ["", " ", "a", "???"])
    def test_a_query_with_nothing_to_search_for_is_422(
        self, api_client: TestClient, lawyer: User, query: str
    ) -> None:
        response = search(api_client, token_for(api_client, lawyer.email), query)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_over_long_query_is_422(self, api_client: TestClient, lawyer: User) -> None:
        response = search(
            api_client,
            token_for(api_client, lawyer.email),
            "a" * (settings.SEARCH_QUERY_MAX_LENGTH + 1),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_oversized_limit_is_422(self, api_client: TestClient, lawyer: User) -> None:
        response = search(
            api_client, token_for(api_client, lawyer.email), limit=settings.SEARCH_MAX_LIMIT + 1
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_unknown_field_is_422(self, api_client: TestClient, lawyer: User) -> None:
        response = api_client.post(
            SEARCH_URL,
            json={"query": "loyer", "top_k": 5},
            headers=bearer(token_for(api_client, lawyer.email)),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_reversed_date_range_is_422(self, api_client: TestClient, lawyer: User) -> None:
        response = search(
            api_client,
            token_for(api_client, lawyer.email),
            filters={
                "indexed_from": "2026-06-01T00:00:00Z",
                "indexed_to": "2026-01-01T00:00:00Z",
            },
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_over_broad_document_filter_is_refused(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "SEARCH_MAX_FILTER_DOCUMENTS", 0)

        response = search(
            api_client, token_for(api_client, lawyer.email), filters={"file_types": ["pdf"]}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["error"] == "search_filter_too_broad"


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class TestFailures:
    def test_a_disabled_deployment_answers_503(
        self, api_client: TestClient, lawyer: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "SEARCH_ENABLED", False)

        response = search(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "search_disabled"

    def test_a_missing_embedding_model_answers_503_naming_the_cause(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        embedder: FakeEmbedder,
    ) -> None:
        embedder.raises = EmbeddingError("model not installed")

        response = search(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "embedding_unavailable"

    def test_an_unreachable_vector_database_answers_503_naming_the_cause(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        vector_searcher: InMemoryVectorSearcher,
    ) -> None:
        vector_searcher.raises = VectorSearchError("qdrant unreachable")

        response = search(api_client, token_for(api_client, lawyer.email))

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "vector_store_unavailable"

    def test_a_failure_body_never_quotes_the_query(
        self,
        api_client: TestClient,
        lawyer: User,
        french_contract: Document,
        embedder: FakeEmbedder,
    ) -> None:
        embedder.raises = EmbeddingError("failed on 'divorce Benali'")

        response = search(api_client, token_for(api_client, lawyer.email), "divorce Benali")

        assert "Benali" not in response.text


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_metrics_report_the_four_figures_the_spec_names(
        self, api_client: TestClient, admin: User, lawyer: User, french_contract: Document
    ) -> None:
        search(api_client, token_for(api_client, lawyer.email))

        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert body["total_searches"] >= 1
        assert body["average_latency_ms"] is not None
        assert body["average_score"] is not None
        assert body["failed_searches"] == 0
        assert body["success_rate"] + body["failure_rate"] == 100.0

    def test_metrics_report_dependency_availability(
        self, api_client: TestClient, admin: User
    ) -> None:
        """Zero searches, no model, and a dead database all show the same counters."""
        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert body["embedding_available"] is True
        assert body["vector_store_available"] is True
        assert body["embedding_model"]
        assert body["vector_collection"]
        assert body["ranker"] == "similarity"
        assert body["enabled"] is True

    def test_a_failure_appears_in_the_breakdown(
        self,
        api_client: TestClient,
        admin: User,
        lawyer: User,
        french_contract: Document,
        vector_searcher: InMemoryVectorSearcher,
    ) -> None:
        vector_searcher.raises = VectorSearchError("qdrant unreachable")
        search(api_client, token_for(api_client, lawyer.email))
        vector_searcher.raises = None

        body = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).json()

        assert body["failed_searches"] == 1
        assert body["failures_by_code"]["vector_store_unavailable"] == 1

    def test_metrics_contain_no_query_document_case_or_passage(
        self,
        api_client: TestClient,
        admin: User,
        lawyer: User,
        legal_case: Case,
        french_contract: Document,
    ) -> None:
        search(api_client, token_for(api_client, lawyer.email), "loyer payable d'avance")

        raw = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, admin.email))
        ).text

        assert str(french_contract.id) not in raw
        assert str(legal_case.id) not in raw
        assert "bail-commercial.pdf" not in raw
        assert "loyer" not in raw.lower()
        assert "payable" not in raw.lower()


# --------------------------------------------------------------------------- #
# Scope boundary
# --------------------------------------------------------------------------- #


class TestScopeBoundary:
    def test_the_api_exposes_search_and_metrics_and_nothing_else(
        self, api_client: TestClient
    ) -> None:
        """No chat, no answer, no summary endpoint. Those are later features."""
        spec = api_client.get("/openapi.json").json()
        paths = {
            path
            for path in spec["paths"]
            if path.startswith(f"{settings.API_V1_PREFIX}/search")
        }

        assert paths == {SEARCH_URL, METRICS_URL}

    def test_the_response_carries_no_generated_text(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        body = search(api_client, token_for(api_client, lawyer.email)).json()

        for forbidden in ("answer", "summary", "completion", "prompt", "message"):
            assert forbidden not in body

    def test_a_result_carries_no_vector_and_no_internal_metadata(
        self, api_client: TestClient, lawyer: User, french_contract: Document
    ) -> None:
        result = search(api_client, token_for(api_client, lawyer.email)).json()["results"][0]

        assert set(result) == {
            "document_id",
            "document_version",
            "case_id",
            "page_number",
            "chunk_number",
            "score",
            "text",
            "language",
            "rank",
            "document",
        }

    def test_the_search_endpoint_is_a_post_so_queries_stay_out_of_urls(
        self, api_client: TestClient
    ) -> None:
        """A query string reaches the proxy log, the browser history, and Referer."""
        spec = api_client.get("/openapi.json").json()

        assert set(spec["paths"][SEARCH_URL]) == {"post"}
        assert "get" not in spec["paths"][SEARCH_URL]

    def test_indexing_still_exposes_no_search_route(self, api_client: TestClient) -> None:
        """The boundary `10-document-indexing.md` made structural, re-asserted.

        Search shipping is exactly when someone would be tempted to hang a query
        route off the indexing module and delete the separation.
        """
        spec = api_client.get("/openapi.json").json()

        for path in spec["paths"]:
            if "/indexing" in path or "/index" in path:
                assert "search" not in path
                assert "query" not in path
