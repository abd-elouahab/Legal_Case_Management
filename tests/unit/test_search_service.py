"""Unit tests for :class:`~services.search.SearchService`.

The corpus these search over is **built by the real indexing pipeline** — a
document, its OCR text, then the real chunker, the real payload builder, and the
real vector store double — so a passage retrieved here travelled the whole way
from an uploaded file. Only the embedding model and the vector database are
substituted, which are the two genuinely external things.

The tests are grouped by what they protect, and the first group is the one that
matters most: **authorization is applied inside the query, never after it.** A
search that filtered in Python would return fewer results than asked for, leak
the existence of matches through the count, and pull unauthorized text into the
process — so these assert the shape of the filter that reaches the searcher, not
merely the results that come back.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.exceptions import (
    CaseNotFoundError,
    DocumentNotFoundError,
    SearchAccessDeniedError,
    SearchDisabledError,
    SearchFilterTooBroadError,
    SearchUnavailableError,
)
from core.search import SearchFailureCode
from models.document import DocumentCategory
from models.user import UserRole
from schemas.search import SearchFilterInput, SearchRequest
from services.embedding import EmbeddingError
from services.vector_search import VectorSearchError

MakeUser = Any
MakeCase = Any
MakeDocument = Any
MakeOcr = Any

# Accented, deliberately: the platform's language heuristic tells French from
# English by the diacritics English does not have, so an unaccented French page
# is legitimately labelled `en` (recorded in `progress-tracker.md`). A fixture
# that ignored that would make the language-filter tests pass for the wrong
# reason.
FRENCH_PAGE = (
    "Article 4 : Loyer et charges. Le loyer mensuel est payable d'avance le premier "
    "jour de chaque mois, au domicile du bailleur ou par virement bancaire. Toute "
    "résiliation anticipée doit être notifiée par écrit."
)
ARABIC_PAGE = (
    "المادة الرابعة: الكراء والتحملات. يؤدى الكراء الشهري مسبقا في اليوم الأول من كل "
    "شهر بمقر المكري أو بواسطة تحويل بنكي."
)


@pytest.fixture
def administrator(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="svc-search-admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def assigned_lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="svc-search-assigned@example.com", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="svc-search-other@example.com", role=UserRole.LAWYER)


@pytest.fixture
def court(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="svc-search-court@example.com", role=UserRole.COURT_REPRESENTATIVE)


@pytest.fixture
def legal_case(make_case: MakeCase, assigned_lawyer, court):  # type: ignore[no-untyped-def]
    return make_case(
        assigned_lawyer_id=assigned_lawyer.id,
        assigned_court_representative_id=court.id,
    )


@pytest.fixture
def other_case(make_case: MakeCase, other_lawyer):  # type: ignore[no-untyped-def]
    return make_case(assigned_lawyer_id=other_lawyer.id)


@pytest.fixture
def index_document(indexing_service, make_ocr_result: MakeOcr):  # type: ignore[no-untyped-def]
    """Run a document through the *real* indexing pipeline into the vector store."""

    def _index(document, pages: list[str]):  # type: ignore[no-untyped-def]
        result = make_ocr_result(document_id=document.id, pages=pages)
        indexing_service.schedule_for_ocr_result(result)
        return result

    return _index


@pytest.fixture
def indexed_document(  # type: ignore[no-untyped-def]
    make_document: MakeDocument, legal_case, index_document
):
    document = make_document(
        case_id=legal_case.id,
        original_filename="bail-commercial.pdf",
        category=DocumentCategory.CONTRACT,
    )
    index_document(document, [FRENCH_PAGE])
    return document


@pytest.fixture
def foreign_document(  # type: ignore[no-untyped-def]
    make_document: MakeDocument, other_case, index_document
):
    """An indexed document on a case the assigned lawyer is *not* party to."""
    document = make_document(case_id=other_case.id, original_filename="autre-affaire.pdf")
    index_document(document, [FRENCH_PAGE])
    return document


def a_request(query: str = "loyer payable d'avance", **kwargs: Any) -> SearchRequest:
    return SearchRequest(query=query, **kwargs)


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuthorization:
    def test_a_lawyer_retrieves_only_passages_from_their_own_cases(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        foreign_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        outcome = search_service.search(a_request(), actor=assigned_lawyer)

        assert outcome.results
        assert {result.document_id for result in outcome.results} == {indexed_document.id}

    def test_the_scope_is_applied_in_the_query_not_afterwards(
        self,
        search_service,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        foreign_document,  # type: ignore[no-untyped-def]
        legal_case,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """Filtering afterwards would return short pages and leak match counts."""
        search_service.search(a_request(), actor=assigned_lawyer)

        assert vector_searcher.last_filters is not None
        assert vector_searcher.last_filters.case_ids == frozenset({legal_case.id})

    def test_an_administrator_searches_without_a_case_restriction(
        self,
        search_service,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        foreign_document,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
    ) -> None:
        outcome = search_service.search(a_request(), actor=administrator)

        assert vector_searcher.last_filters.case_ids is None
        assert {result.document_id for result in outcome.results} == {
            indexed_document.id,
            foreign_document.id,
        }

    def test_a_lawyer_assigned_to_nothing_retrieves_nothing(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        make_user: MakeUser,
    ) -> None:
        """**The catastrophic-if-wrong case.**

        An empty scope must match nothing. Confusing it with "no restriction"
        would turn an unassigned lawyer into a platform-wide reader.
        """
        stranger = make_user(email="svc-search-nobody@example.com", role=UserRole.LAWYER)

        outcome = search_service.search(a_request(), actor=stranger)

        assert outcome.results == []

    def test_an_unassigned_caller_costs_no_embedding_and_no_query(
        self,
        search_service,  # type: ignore[no-untyped-def]
        embedder,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        make_user: MakeUser,
    ) -> None:
        """Short-circuited before anything expensive, and before anything external."""
        stranger = make_user(email="svc-search-nobody2@example.com", role=UserRole.LAWYER)
        embedder.calls.clear()
        vector_searcher.calls.clear()

        search_service.search(a_request(), actor=stranger)

        assert embedder.calls == []
        assert vector_searcher.calls == []

    def test_a_court_representative_searches_their_own_cases(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        foreign_document,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        outcome = search_service.search(a_request(), actor=court)

        assert {result.document_id for result in outcome.results} == {indexed_document.id}

    def test_filtering_by_someone_else_s_case_is_refused(
        self,
        search_service,  # type: ignore[no-untyped-def]
        other_case,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(SearchAccessDeniedError):
            search_service.search(
                a_request(filters=SearchFilterInput(case_id=other_case.id)),
                actor=assigned_lawyer,
            )

    def test_filtering_by_someone_else_s_document_is_refused(
        self,
        search_service,  # type: ignore[no-untyped-def]
        foreign_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(SearchAccessDeniedError):
            search_service.search(
                a_request(filters=SearchFilterInput(document_id=foreign_document.id)),
                actor=assigned_lawyer,
            )

    def test_a_filter_can_never_widen_the_scope(
        self,
        search_service,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        foreign_document,  # type: ignore[no-untyped-def]
        legal_case,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """The spec's "metadata filtering cannot bypass permissions"."""
        search_service.search(
            a_request(
                filters=SearchFilterInput(languages=["fr", "ar", "en"], document_version=1)
            ),
            actor=assigned_lawyer,
        )

        assert vector_searcher.last_filters.case_ids == frozenset({legal_case.id})

    def test_a_deleted_document_s_passages_are_not_returned(
        self,
        search_service,  # type: ignore[no-untyped-def]
        db_session,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """Deletion is logical, so the vectors outlive it.

        This is the point at which a search result stops being more visible than
        the document it came from.
        """
        from datetime import UTC, datetime

        assert search_service.search(a_request(), actor=assigned_lawyer).results

        indexed_document.deleted_at = datetime.now(UTC)
        db_session.commit()

        assert search_service.search(a_request(), actor=assigned_lawyer).results == []


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


class TestRetrieval:
    def test_a_result_carries_the_passage_and_its_provenance(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        legal_case,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        result = search_service.search(a_request(), actor=assigned_lawyer).results[0]

        assert result.document_id == indexed_document.id
        assert result.document_version == 1
        assert result.case_id == legal_case.id
        assert result.page_number == 1
        assert result.chunk_number == 0
        assert result.text
        assert result.text in FRENCH_PAGE
        assert result.language == "fr"
        assert result.rank == 1
        assert result.document is not None
        assert result.document.original_filename == "bail-commercial.pdf"

    def test_an_exact_query_scores_at_the_top(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        vector_store,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """Relevance, not merely presence.

        The fake embedder is deterministic over text, so a query equal to a
        passage embeds to that passage's own vector and scores 1.0.
        """
        passage = next(iter(vector_store.points.values())).payload["text"]

        outcome = search_service.search(a_request(passage), actor=assigned_lawyer)

        assert outcome.results[0].text == passage
        assert outcome.top_score == pytest.approx(1.0, abs=1e-6)

    def test_results_are_ranked_and_numbered_from_one(
        self,
        search_service,  # type: ignore[no-untyped-def]
        make_document: MakeDocument,
        legal_case,  # type: ignore[no-untyped-def]
        index_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        for number in range(3):
            document = make_document(case_id=legal_case.id)
            index_document(document, [f"{FRENCH_PAGE} Variante {number}."])

        outcome = search_service.search(a_request(), actor=assigned_lawyer)

        assert [result.rank for result in outcome.results] == list(
            range(1, len(outcome.results) + 1)
        )
        scores = [result.score for result in outcome.results]
        assert scores == sorted(scores, reverse=True)

    def test_the_limit_and_offset_reach_the_database(
        self,
        search_service,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """Pagination executes in the vector database, not by slicing in Python."""
        vector_searcher.calls.clear()

        search_service.search(a_request(limit=3, offset=6), actor=assigned_lawyer)

        assert vector_searcher.calls == [(3, 6)]

    def test_matching_nothing_is_a_success_with_an_empty_page(
        self,
        search_service,  # type: ignore[no-untyped-def]
        legal_case,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        search_metrics,  # type: ignore[no-untyped-def]
    ) -> None:
        outcome = search_service.search(a_request(), actor=assigned_lawyer)

        assert outcome.results == []
        assert outcome.top_score is None
        assert outcome.average_score is None
        assert search_metrics.snapshot().failed_searches == 0

    def test_has_more_is_true_only_when_the_page_filled(
        self,
        search_service,  # type: ignore[no-untyped-def]
        make_document: MakeDocument,
        legal_case,  # type: ignore[no-untyped-def]
        index_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        for number in range(3):
            index_document(make_document(case_id=legal_case.id), [f"{FRENCH_PAGE} {number}"])

        assert search_service.search(a_request(limit=2), actor=assigned_lawyer).has_more is True
        assert (
            search_service.search(a_request(limit=50), actor=assigned_lawyer).has_more is False
        )

    def test_has_more_reads_what_the_database_returned_not_what_survived(
        self,
        search_service,  # type: ignore[no-untyped-def]
        db_session,  # type: ignore[no-untyped-def]
        make_document: MakeDocument,
        legal_case,  # type: ignore[no-untyped-def]
        index_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """A page that lost a result to a deleted document still has more behind it.

        Reading the surviving count would say otherwise and strand the reader on
        page one with no way forward.
        """
        from datetime import UTC, datetime

        documents = [make_document(case_id=legal_case.id) for _ in range(2)]
        for number, document in enumerate(documents):
            index_document(document, [f"{FRENCH_PAGE} Variante {number}."])

        documents[0].deleted_at = datetime.now(UTC)
        db_session.commit()

        outcome = search_service.search(a_request(limit=2), actor=assigned_lawyer)

        assert len(outcome.results) == 1
        assert outcome.retrieved == 2
        assert outcome.has_more is True

    def test_the_query_is_embedded_with_the_indexing_model(
        self,
        search_service,  # type: ignore[no-untyped-def]
        embedder,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """`ai-architecture.md`: one model for documents and for queries."""
        embedder.calls.clear()

        search_service.search(a_request("loyer"), actor=assigned_lawyer)

        assert embedder.calls == [["loyer"]]

    def test_an_arabic_query_is_accepted_and_embedded(
        self,
        search_service,  # type: ignore[no-untyped-def]
        make_document: MakeDocument,
        legal_case,  # type: ignore[no-untyped-def]
        index_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        document = make_document(case_id=legal_case.id, original_filename="كراء.pdf")
        index_document(document, [ARABIC_PAGE])

        outcome = search_service.search(a_request(ARABIC_PAGE), actor=assigned_lawyer)

        assert outcome.results
        assert outcome.results[0].language == "ar"


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


class TestFiltering:
    def test_filtering_by_case_narrows_to_that_case(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        foreign_document,  # type: ignore[no-untyped-def]
        other_case,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
    ) -> None:
        outcome = search_service.search(
            a_request(filters=SearchFilterInput(case_id=other_case.id)), actor=administrator
        )

        assert {result.document_id for result in outcome.results} == {foreign_document.id}

    def test_filtering_by_document_narrows_to_that_document(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        make_document: MakeDocument,
        legal_case,  # type: ignore[no-untyped-def]
        index_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        index_document(make_document(case_id=legal_case.id), [FRENCH_PAGE])

        outcome = search_service.search(
            a_request(filters=SearchFilterInput(document_id=indexed_document.id)),
            actor=assigned_lawyer,
        )

        assert {result.document_id for result in outcome.results} == {indexed_document.id}

    def test_filtering_by_language_narrows_to_that_language(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        make_document: MakeDocument,
        legal_case,  # type: ignore[no-untyped-def]
        index_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """Per passage, so a French annex in an Arabic filing is reachable alone."""
        index_document(make_document(case_id=legal_case.id), [ARABIC_PAGE])

        outcome = search_service.search(
            a_request(filters=SearchFilterInput(languages=["ar"])), actor=assigned_lawyer
        )

        assert outcome.results
        assert {result.language for result in outcome.results} == {"ar"}

    def test_filtering_by_category_resolves_through_the_documents_table(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        make_document: MakeDocument,
        legal_case,  # type: ignore[no-untyped-def]
        index_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """The payload carries no category — indexing stores what a *chunk* is."""
        evidence = make_document(case_id=legal_case.id, category=DocumentCategory.EVIDENCE)
        index_document(evidence, [FRENCH_PAGE])

        outcome = search_service.search(
            a_request(filters=SearchFilterInput(categories=[DocumentCategory.CONTRACT])),
            actor=assigned_lawyer,
        )

        assert {result.document_id for result in outcome.results} == {indexed_document.id}

    def test_filtering_by_file_type_resolves_the_same_way(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        make_document: MakeDocument,
        legal_case,  # type: ignore[no-untyped-def]
        index_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        from tests.helpers import PNG_BYTES

        scan = make_document(case_id=legal_case.id, extension="png", content=PNG_BYTES)
        index_document(scan, [FRENCH_PAGE])

        outcome = search_service.search(
            a_request(filters=SearchFilterInput(file_types=["png"])), actor=assigned_lawyer
        )

        assert {result.document_id for result in outcome.results} == {scan.id}

    def test_a_document_level_filter_is_still_scoped_to_the_caller(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        foreign_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """The SQL resolution carries the same scope the vector filter does."""
        outcome = search_service.search(
            a_request(filters=SearchFilterInput(file_types=["pdf"])), actor=assigned_lawyer
        )

        assert {result.document_id for result in outcome.results} == {indexed_document.id}

    def test_filtering_by_version_narrows_to_that_version(
        self,
        search_service,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        search_service.search(
            a_request(filters=SearchFilterInput(document_version=2)), actor=assigned_lawyer
        )

        assert vector_searcher.last_filters.document_version == 2

    def test_an_over_broad_document_filter_is_refused_rather_than_truncated(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A silently shortened set would drop matches with nothing to show for it."""
        from core.config import settings

        monkeypatch.setattr(settings, "SEARCH_MAX_FILTER_DOCUMENTS", 0)

        with pytest.raises(SearchFilterTooBroadError):
            search_service.search(
                a_request(filters=SearchFilterInput(file_types=["pdf"])),
                actor=assigned_lawyer,
            )

    def test_a_filter_naming_a_missing_case_is_a_404(
        self,
        search_service,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(CaseNotFoundError):
            search_service.search(
                a_request(filters=SearchFilterInput(case_id=uuid.uuid4())), actor=administrator
            )

    def test_a_filter_naming_a_missing_document_is_a_404(
        self,
        search_service,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(DocumentNotFoundError):
            search_service.search(
                a_request(filters=SearchFilterInput(document_id=uuid.uuid4())),
                actor=administrator,
            )


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


class TestFailures:
    def test_search_can_be_switched_off(
        self,
        search_service,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "SEARCH_ENABLED", False)

        with pytest.raises(SearchDisabledError):
            search_service.search(a_request(), actor=assigned_lawyer)

    def test_a_missing_embedding_model_is_a_503_naming_the_cause(
        self,
        search_service,  # type: ignore[no-untyped-def]
        embedder,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        embedder.raises = EmbeddingError("model missing")

        with pytest.raises(SearchUnavailableError) as excinfo:
            search_service.search(a_request(), actor=assigned_lawyer)

        assert excinfo.value.status_code == 503
        assert excinfo.value.error_code == SearchFailureCode.EMBEDDING_UNAVAILABLE.value

    def test_an_unreachable_vector_database_is_a_503_naming_the_cause(
        self,
        search_service,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        vector_searcher.raises = VectorSearchError("qdrant down")

        with pytest.raises(SearchUnavailableError) as excinfo:
            search_service.search(a_request(), actor=assigned_lawyer)

        assert excinfo.value.error_code == SearchFailureCode.VECTOR_STORE_UNAVAILABLE.value

    def test_a_failure_message_never_quotes_the_query(
        self,
        search_service,  # type: ignore[no-untyped-def]
        embedder,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        embedder.raises = EmbeddingError("failed encoding 'divorce Benali'")

        with pytest.raises(SearchUnavailableError) as excinfo:
            search_service.search(a_request("divorce Benali"), actor=assigned_lawyer)

        assert "Benali" not in str(excinfo.value)

    def test_a_failure_is_recorded_before_it_is_raised(
        self,
        search_service,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
        search_metrics,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """Raising without recording would make the failure rate under-report outages."""
        vector_searcher.raises = VectorSearchError("qdrant down")

        with pytest.raises(SearchUnavailableError):
            search_service.search(a_request(), actor=assigned_lawyer)

        snapshot = search_metrics.snapshot()
        assert snapshot.failed_searches == 1
        assert snapshot.failures_by_code == {"vector_store_unavailable": 1}

    def test_a_malformed_point_is_skipped_rather_than_failing_the_search(
        self,
        search_service,  # type: ignore[no-untyped-def]
        vector_store,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """One bad point, from an older build, must not fail the whole search."""
        from services.vector_store import VectorPoint

        vector_store.points["broken"] = VectorPoint(
            id=uuid.uuid4(), vector=[0.0] * 8, payload={"case_id": str(uuid.uuid4())}
        )

        outcome = search_service.search(a_request(), actor=assigned_lawyer)

        assert outcome.results
        assert all(result.document_id for result in outcome.results)


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #


class TestObservability:
    def test_a_successful_search_is_recorded(
        self,
        search_service,  # type: ignore[no-untyped-def]
        search_metrics,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        search_service.search(a_request(), actor=assigned_lawyer)

        snapshot = search_metrics.snapshot()
        assert snapshot.successful_searches == 1
        assert snapshot.total_results >= 1
        assert snapshot.average_latency_ms is not None

    def test_the_log_carries_a_fingerprint_and_not_the_query(
        self,
        search_service,  # type: ignore[no-untyped-def]
        indexed_document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """The spec's logging rule, asserted on the fields the service emits."""
        from core.search import query_fingerprint

        event = search_service._event(a_request("divorce Benali"), actor=assigned_lawyer)

        assert event["query"] is None
        assert event["query_fingerprint"] == query_fingerprint("divorce Benali")
        assert "Benali" not in str(event)

    def test_the_query_appears_only_when_the_deployment_opts_in(
        self,
        search_service,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "SEARCH_LOG_QUERIES", True)
        event = search_service._event(a_request("divorce Benali"), actor=assigned_lawyer)

        assert event["query"] == "divorce Benali"
        assert event["query_fingerprint"]

    def test_the_log_reports_filters_as_a_shape_not_as_values(
        self,
        search_service,  # type: ignore[no-untyped-def]
        legal_case,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """A list of case identifiers is a list of the caller's matters."""
        event = search_service._event(
            a_request(filters=SearchFilterInput(case_id=legal_case.id)), actor=assigned_lawyer
        )

        assert event["filtered"] is True
        assert str(legal_case.id) not in str(event)

    def test_health_reports_availability_rather_than_inferring_it(
        self,
        search_service,  # type: ignore[no-untyped-def]
        embedder,  # type: ignore[no-untyped-def]
        vector_searcher,  # type: ignore[no-untyped-def]
    ) -> None:
        """Zero searches, no model, and a dead database all show the same counters."""
        embedder.available = False
        vector_searcher.available = False

        health = search_service.health()

        assert health.embedding_available is False
        assert health.vector_store_available is False
        assert health.embedding_model == embedder.model
        assert health.ranker == "similarity"


# --------------------------------------------------------------------------- #
# Scope boundary
# --------------------------------------------------------------------------- #


class TestScopeBoundary:
    def test_the_service_has_no_generation_capability(
        self,
        search_service,  # type: ignore[no-untyped-def]
    ) -> None:
        """Retrieval only.

        ``11-semantic-search.md`` forbids answer generation, summarization, and
        LLM calls. The guard is structural — the service's collaborators are an
        embedder, a searcher, a ranker, and two repositories, none of which can
        generate text — and this pins the public surface that would have to grow
        for that to change.
        """
        public = {
            name for name in dir(search_service) if not name.startswith("_")
        }

        assert public == {"health", "search"}

    def test_the_service_publishes_nothing_and_schedules_nothing(
        self,
        search_service,  # type: ignore[no-untyped-def]
    ) -> None:
        """A search changes nothing, so it has nothing to announce or to queue."""
        assert not hasattr(search_service, "_timeline")
        assert not hasattr(search_service, "_queue")
