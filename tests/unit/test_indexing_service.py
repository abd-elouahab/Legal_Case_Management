"""Unit tests for :class:`~services.indexing.IndexingService`.

The service against real repositories on the test database, the real chunker, and
doubles for the two genuinely external things — the embedding model and the
vector store. So the pipeline under test is the production one.

The properties asserted here are the ones ``10-document-indexing.md`` names and
nothing else could enforce: that an index belongs to a document *version*, that
re-indexing is idempotent and leaves no duplicate or stale vectors, that every
failure is a recorded state which preserves the OCR data, that chunk metadata is
complete, and that the whole thing stays clear of search.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from core.exceptions import (
    DocumentIndexNotFoundError,
    DocumentNotFoundError,
    DocumentVersionNotFoundError,
    IndexAccessDeniedError,
    IndexingAlreadyRunningError,
    IndexingDisabledError,
    IndexingNotReadyError,
    InvalidIndexTransitionError,
)
from core.indexing import IndexFailureCode, chunk_point_id
from models.indexing import DocumentIndex, IndexStatus
from models.ocr import OcrStatus
from models.timeline import TimelineEventType
from models.user import UserRole
from services.embedding import EmbeddingError
from services.indexing import IndexingService, IndexJob, NullIndexScheduler
from services.vector_store import VectorStoreError

PAGE_ONE = (
    "CONTRAT DE BAIL COMMERCIAL. Article 1 : Objet. Le bailleur loue au preneur les "
    "locaux désignés ci-après, situés à Casablanca, pour l'exercice d'une activité "
    "commerciale conforme au règlement de copropriété."
)
PAGE_TWO = (
    "Article 2 : Durée. Le présent bail est consenti pour une durée de trois années "
    "entières et consécutives à compter de la date de signature des présentes."
)


@pytest.fixture
def case(make_case: Any, make_user: Any) -> Any:
    lawyer = make_user(role=UserRole.LAWYER, email="indexing-lawyer@example.com")
    return make_case(assigned_lawyer_id=lawyer.id)


@pytest.fixture
def document(make_document: Any, case: Any) -> Any:
    return make_document(case_id=case.id, original_filename="bail.pdf")


@pytest.fixture
def extracted(make_ocr_result: Any, document: Any) -> Any:
    """A completed extraction, which is what indexing consumes."""
    return make_ocr_result(document_id=document.id, pages=[PAGE_ONE, PAGE_TWO])


def index_for(session: Session, document_id: uuid.UUID, version: int = 1) -> DocumentIndex:
    from repositories.indexing import IndexingRepository

    found = IndexingRepository(session).get_for_version(document_id, version)
    assert found is not None
    return found


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


class TestScheduling:
    def test_a_completed_extraction_schedules_an_index(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        assert index_for(db_session, document.id).status is IndexStatus.INDEXED

    def test_the_index_records_the_case(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        case: Any,
        db_session: Session,
    ) -> None:
        # Copied onto the row so a worker can put it in every vector payload
        # without a join, and so the list scope is one subquery rather than two.
        indexing_service.schedule_for_ocr_result(extracted)
        assert index_for(db_session, document.id).case_id == case.id

    def test_an_unfinished_extraction_schedules_nothing(
        self,
        indexing_service: IndexingService,
        make_ocr_result: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        # Indexing begins at "OCR Completed"; a row that could only ever say
        # "there was nothing to index" is not a record worth keeping.
        running = make_ocr_result(document_id=document.id, status=OcrStatus.PROCESSING)
        assert indexing_service.schedule_for_ocr_result(running) is None

    def test_an_extraction_with_no_text_schedules_nothing(
        self,
        indexing_service: IndexingService,
        make_ocr_result: Any,
        document: Any,
    ) -> None:
        blank = make_ocr_result(document_id=document.id, pages=["   ", ""])
        assert indexing_service.schedule_for_ocr_result(blank) is None

    def test_scheduling_is_idempotent(
        self, indexing_service: IndexingService, extracted: Any
    ) -> None:
        first = indexing_service.schedule_for_ocr_result(extracted)
        second = indexing_service.schedule_for_ocr_result(extracted)
        assert first is not None
        assert second is not None
        assert first.id == second.id

    def test_scheduling_never_raises(
        self, indexing_service: IndexingService, extracted: Any, monkeypatch: Any
    ) -> None:
        # The text is already persisted and the extraction has already succeeded,
        # so a queueing problem must not turn a completed extraction into a
        # failure.
        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("queue is on fire")

        monkeypatch.setattr(indexing_service, "_schedule", explode)
        assert indexing_service.schedule_for_ocr_result(extracted) is None

    def test_scheduling_is_skipped_when_indexing_is_disabled(
        self, indexing_service: IndexingService, extracted: Any, monkeypatch: Any
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "INDEXING_ENABLED", False)
        assert indexing_service.schedule_for_ocr_result(extracted) is None

    def test_the_null_scheduler_queues_nothing(self, extracted: Any) -> None:
        assert NullIndexScheduler().schedule_for_ocr_result(extracted) is None


class TestRequeue:
    def test_pending_rows_are_re_queued(
        self,
        indexing_service: IndexingService,
        index_queue: Any,
        make_document_index: Any,
        document: Any,
        case: Any,
    ) -> None:
        # A job's schedule lives in memory but its record lives in the database,
        # so a restart would otherwise strand `pending` rows forever.
        index_queue.run_inline = False
        make_document_index(
            document_id=document.id, case_id=case.id, status=IndexStatus.PENDING
        )
        assert indexing_service.requeue_pending() == 1
        assert len(index_queue.jobs) == 1

    def test_nothing_is_re_queued_when_indexing_is_disabled(
        self, indexing_service: IndexingService, monkeypatch: Any
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "INDEXING_ENABLED", False)
        assert indexing_service.requeue_pending() == 0


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #


class TestPipeline:
    def test_the_text_is_chunked_and_every_chunk_is_embedded(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        embedder: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        index = index_for(db_session, document.id)

        assert index.chunk_count is not None
        assert index.chunk_count > 0
        assert sum(len(batch) for batch in embedder.calls) == index.chunk_count

    def test_vectors_are_stored_with_their_metadata(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        vector_store: Any,
        document: Any,
        case: Any,
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        points = vector_store.for_version(document.id, 1)

        assert points
        for point in points:
            payload = point.payload
            assert payload["document_id"] == str(document.id)
            assert payload["document_version"] == 1
            assert payload["case_id"] == str(case.id)
            assert payload["page_number"] in {1, 2}
            assert isinstance(payload["chunk_number"], int)
            assert payload["language"]
            assert payload["indexed_at"]
            assert payload["embedding_model"] == "fake/test-embedder"

    def test_page_order_survives_into_the_vectors(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        vector_store: Any,
        document: Any,
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        points = vector_store.for_version(document.id, 1)
        pages = [point.payload["page_number"] for point in points]
        assert pages == sorted(pages)

    def test_every_point_id_is_derived_from_its_position(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        vector_store: Any,
        document: Any,
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        for point in vector_store.for_version(document.id, 1):
            assert point.id == chunk_point_id(
                document.id,
                point.payload["document_version"],
                point.payload["page_number"],
                point.payload["chunk_number"],
            )

    def test_the_collection_is_created_at_the_model_s_width(
        self, indexing_service: IndexingService, extracted: Any, vector_store: Any
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        assert vector_store.created_with == 8

    def test_the_run_records_what_it_used(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        # Configuration is current; an index is historical. Changing the model
        # requires re-indexing, and that comparison needs this recorded.
        from core.config import settings

        indexing_service.schedule_for_ocr_result(extracted)
        index = index_for(db_session, document.id)

        assert index.embedding_model == "fake/test-embedder"
        assert index.embedding_dimensions == 8
        assert index.vector_collection == "test-chunks"
        assert index.chunk_size == settings.INDEX_CHUNK_SIZE
        assert index.chunk_overlap == settings.INDEX_CHUNK_OVERLAP

    def test_the_run_records_what_it_produced(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        index = index_for(db_session, document.id)

        assert index.page_count == 2
        assert index.character_count == len(PAGE_ONE) + len(PAGE_TWO)
        assert index.detected_language == "fr"
        assert index.duration_ms is not None
        assert index.started_at is not None
        assert index.finished_at is not None
        assert index.attempt_count == 1

    def test_a_blank_page_produces_no_chunk_but_does_not_renumber(
        self,
        indexing_service: IndexingService,
        make_ocr_result: Any,
        vector_store: Any,
        document: Any,
    ) -> None:
        result = make_ocr_result(
            document_id=document.id, pages=[PAGE_ONE, "   ", PAGE_TWO]
        )
        indexing_service.schedule_for_ocr_result(result)

        pages = {
            point.payload["page_number"] for point in vector_store.for_version(document.id, 1)
        }
        assert pages == {1, 3}

    def test_the_chunk_cap_truncates_rather_than_timing_out(
        self,
        indexing_service: IndexingService,
        make_ocr_result: Any,
        document: Any,
        db_session: Session,
        monkeypatch: Any,
    ) -> None:
        # A partial index and a note in the log is strictly better than a
        # guaranteed timeout that produces none.
        from core.config import settings

        monkeypatch.setattr(settings, "INDEX_MAX_CHUNKS", 1)
        result = make_ocr_result(document_id=document.id, pages=[PAGE_ONE, PAGE_TWO])
        indexing_service.schedule_for_ocr_result(result)

        assert index_for(db_session, document.id).chunk_count == 1


# --------------------------------------------------------------------------- #
# Re-indexing
# --------------------------------------------------------------------------- #


class TestReindexing:
    def test_re_indexing_re_uses_the_row(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        make_user: Any,
        db_session: Session,
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="reindex-admin@example.com")
        first = indexing_service.schedule_for_ocr_result(extracted)
        assert first is not None

        again = indexing_service.reindex(document.id, actor=actor)
        assert again.id == first.id
        assert again.attempt_count == 2

    def test_re_indexing_produces_no_duplicate_vectors(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        vector_store: Any,
        document: Any,
        make_user: Any,
    ) -> None:
        # The spec's headline re-indexing requirement, asserted as the count it
        # is: derived point ids make a repeat write an overwrite.
        actor = make_user(role=UserRole.ADMINISTRATOR, email="dup-admin@example.com")
        indexing_service.schedule_for_ocr_result(extracted)
        before = len(vector_store.for_version(document.id, 1))

        indexing_service.reindex(document.id, actor=actor)
        assert len(vector_store.for_version(document.id, 1)) == before

    def test_re_indexing_is_deterministic(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        vector_store: Any,
        document: Any,
        make_user: Any,
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="det-admin@example.com")
        indexing_service.schedule_for_ocr_result(extracted)
        before = {
            str(point.id): list(point.vector)
            for point in vector_store.for_version(document.id, 1)
        }

        indexing_service.reindex(document.id, actor=actor)
        after = {
            str(point.id): list(point.vector)
            for point in vector_store.for_version(document.id, 1)
        }
        assert before == after

    def test_a_shorter_rebuild_leaves_nothing_behind(
        self,
        indexing_service: IndexingService,
        make_ocr_result: Any,
        vector_store: Any,
        document: Any,
        make_user: Any,
        db_session: Session,
    ) -> None:
        # The half a derived id cannot cover: without the delete, the tail of the
        # previous, longer index would survive as stale vectors.
        actor = make_user(role=UserRole.ADMINISTRATOR, email="short-admin@example.com")
        result = make_ocr_result(document_id=document.id, pages=[PAGE_ONE, PAGE_TWO])
        indexing_service.schedule_for_ocr_result(result)
        assert len(vector_store.for_version(document.id, 1)) >= 2

        # The text shrinks — a corrected extraction, or a re-run that read less.
        result.pages[1].text = ""
        db_session.commit()

        indexing_service.reindex(document.id, actor=actor)
        pages = {
            point.payload["page_number"] for point in vector_store.for_version(document.id, 1)
        }
        assert pages == {1}

    def test_a_replacement_keeps_the_previous_version_s_vectors(
        self,
        indexing_service: IndexingService,
        make_ocr_result: Any,
        vector_store: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        # Both the delete and the point ids are scoped to the *version*, so a new
        # version's index is built without destroying the one that is still the
        # right answer for anyone reading version 1.
        first = make_ocr_result(document_id=document.id, pages=[PAGE_ONE])
        indexing_service.schedule_for_ocr_result(first)

        from models.document import DocumentVersion

        db_session.add(
            DocumentVersion(
                id=uuid.uuid4(),
                document_id=document.id,
                version=2,
                original_filename="bail.pdf",
                stored_filename="v2.pdf",
                file_extension="pdf",
                mime_type="application/pdf",
                file_size=10,
                storage_bucket="test",
                storage_key="v2",
                uploaded_by=None,
            )
        )
        document.version = 2
        db_session.commit()

        second = make_ocr_result(
            document_id=document.id, document_version=2, pages=[PAGE_TWO]
        )
        indexing_service.schedule_for_ocr_result(second)

        assert len(vector_store.for_version(document.id, 1)) >= 1
        assert len(vector_store.for_version(document.id, 2)) >= 1

    def test_a_version_never_indexed_is_bootstrapped_rather_than_refused(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        make_user: Any,
    ) -> None:
        # A document extracted while indexing was disabled is exactly the case a
        # rebuild should be able to fix.
        actor = make_user(role=UserRole.ADMINISTRATOR, email="boot-admin@example.com")
        index = indexing_service.reindex(document.id, actor=actor)
        assert index.status is IndexStatus.INDEXED

    def test_a_running_index_refuses_a_rebuild(
        self,
        indexing_service: IndexingService,
        make_document_index: Any,
        document: Any,
        case: Any,
        extracted: Any,
        make_user: Any,
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="busy-admin@example.com")
        make_document_index(
            document_id=document.id, case_id=case.id, status=IndexStatus.INDEXING
        )
        with pytest.raises(IndexingAlreadyRunningError):
            indexing_service.reindex(document.id, actor=actor)

    def test_a_document_with_no_extracted_text_is_refused_with_its_state(
        self,
        indexing_service: IndexingService,
        make_ocr_result: Any,
        document: Any,
        make_user: Any,
    ) -> None:
        # 409 naming the extraction's state, so the caller can wait for it rather
        # than guessing.
        actor = make_user(role=UserRole.ADMINISTRATOR, email="notready-admin@example.com")
        make_ocr_result(document_id=document.id, status=OcrStatus.FAILED)

        with pytest.raises(IndexingNotReadyError) as failure:
            indexing_service.reindex(document.id, actor=actor)
        assert "failed" in str(failure.value)

    def test_a_document_never_extracted_is_refused(
        self, indexing_service: IndexingService, document: Any, make_user: Any
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="never-admin@example.com")
        with pytest.raises(IndexingNotReadyError):
            indexing_service.reindex(document.id, actor=actor)

    def test_re_indexing_is_refused_when_disabled(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        make_user: Any,
        monkeypatch: Any,
    ) -> None:
        from core.config import settings

        actor = make_user(role=UserRole.ADMINISTRATOR, email="off-admin@example.com")
        monkeypatch.setattr(settings, "INDEXING_ENABLED", False)
        with pytest.raises(IndexingDisabledError):
            indexing_service.reindex(document.id, actor=actor)

    def test_an_unknown_version_is_refused(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        make_user: Any,
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="ver-admin@example.com")
        with pytest.raises(DocumentVersionNotFoundError):
            indexing_service.reindex(document.id, actor=actor, version=99)


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


class TestFailureHandling:
    def _run(self, service: IndexingService, index: DocumentIndex) -> None:
        service.process(
            IndexJob(
                index_id=index.id,
                document_id=index.document_id,
                document_version=index.document_version,
            )
        )

    def test_an_embedding_failure_is_a_recorded_state(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        embedder: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        embedder.raises = EmbeddingError("model exploded")
        indexing_service.schedule_for_ocr_result(extracted)

        index = index_for(db_session, document.id)
        assert index.status is IndexStatus.FAILED
        assert index.error_code == IndexFailureCode.EMBEDDING_FAILURE.value
        assert index.error_message

    def test_an_unavailable_vector_database_is_a_recorded_state(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        vector_store: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        vector_store.raises = VectorStoreError("qdrant is down")
        indexing_service.schedule_for_ocr_result(extracted)

        index = index_for(db_session, document.id)
        assert index.status is IndexStatus.FAILED
        assert index.error_code == IndexFailureCode.VECTOR_STORE_UNAVAILABLE.value

    def test_invalid_ocr_output_is_a_recorded_state(
        self,
        indexing_service: IndexingService,
        make_document_index: Any,
        document: Any,
        case: Any,
        db_session: Session,
    ) -> None:
        # Scheduling refuses to create a row for a document with no text, but a
        # row can still exist — created when there *was* text, then re-queued.
        index = make_document_index(
            document_id=document.id, case_id=case.id, status=IndexStatus.PENDING
        )
        self._run(indexing_service, index)

        db_session.refresh(index)
        assert index.status is IndexStatus.FAILED
        assert index.error_code == IndexFailureCode.INVALID_OCR_OUTPUT.value

    def test_a_timeout_is_a_recorded_state(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        db_session: Session,
        monkeypatch: Any,
    ) -> None:
        from core.config import settings

        # A deadline already in the past: the check between stages fires on the
        # first one, which is the guarantee the service actually makes.
        monkeypatch.setattr(settings, "INDEXING_TIMEOUT_SECONDS", -1)
        indexing_service.schedule_for_ocr_result(extracted)

        index = index_for(db_session, document.id)
        assert index.status is IndexStatus.FAILED
        assert index.error_code == IndexFailureCode.TIMEOUT.value

    def test_an_unexpected_fault_becomes_an_ordinary_failed_run(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        db_session: Session,
        monkeypatch: Any,
    ) -> None:
        # `indexing` forever is the one state nothing recovers from without an
        # operator.
        #
        # Patched on the *class*, not on the fixture instance: the queue runner
        # builds its own service exactly as the production worker does, so an
        # instance-level patch would never be seen by the code under test.
        def explode(*args: object, **kwargs: object) -> None:
            raise ZeroDivisionError("a bug nobody anticipated")

        monkeypatch.setattr(IndexingService, "_chunk", explode)
        indexing_service.schedule_for_ocr_result(extracted)

        index = index_for(db_session, document.id)
        assert index.status is IndexStatus.FAILED
        assert index.error_code == IndexFailureCode.UNKNOWN.value

    def test_a_failure_preserves_the_extracted_text(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        embedder: Any,
        db_session: Session,
    ) -> None:
        # *"Failures must […] preserve OCR data"* — structural here, because the
        # service holds no write path to either OCR table.
        embedder.raises = EmbeddingError("model exploded")
        indexing_service.schedule_for_ocr_result(extracted)

        db_session.refresh(extracted)
        assert extracted.status is OcrStatus.COMPLETED
        assert [page.text for page in extracted.pages] == [PAGE_ONE, PAGE_TWO]

    def test_a_failure_leaves_the_document_untouched(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        embedder: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        before = (document.original_filename, document.version, document.file_size)
        embedder.raises = EmbeddingError("model exploded")
        indexing_service.schedule_for_ocr_result(extracted)

        db_session.refresh(document)
        assert (document.original_filename, document.version, document.file_size) == before

    def test_a_failed_run_stays_retryable(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        embedder: Any,
        document: Any,
        make_user: Any,
        db_session: Session,
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="retry-admin@example.com")
        embedder.raises = EmbeddingError("model exploded")
        indexing_service.schedule_for_ocr_result(extracted)

        embedder.raises = None
        rebuilt = indexing_service.reindex(document.id, actor=actor)
        assert rebuilt.status is IndexStatus.INDEXED
        assert rebuilt.error_code is None
        assert rebuilt.attempt_count == 2


# --------------------------------------------------------------------------- #
# Concurrency and lifecycle
# --------------------------------------------------------------------------- #


class TestConcurrency:
    def test_a_second_worker_does_not_re_index_a_claimed_run(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        index = index_for(db_session, document.id)

        # The run is terminal, so a stale job for it claims nothing.
        assert (
            indexing_service.process(
                IndexJob(index_id=index.id, document_id=document.id, document_version=1)
            )
            is None
        )

    def test_a_job_for_a_missing_row_is_ignored(
        self, indexing_service: IndexingService
    ) -> None:
        assert (
            indexing_service.process(
                IndexJob(
                    index_id=uuid.uuid4(), document_id=uuid.uuid4(), document_version=1
                )
            )
            is None
        )

    def test_the_claim_increments_the_attempt(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        db_session: Session,
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        assert index_for(db_session, document.id).attempt_count == 1


class TestTransitions:
    def test_an_illegal_transition_is_refused(self) -> None:
        # A future caller — a bulk rebuild after a model change, an admin tool —
        # must not be able to write a status directly.
        index = DocumentIndex(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_version=1,
            case_id=uuid.uuid4(),
            status=IndexStatus.PENDING,
        )
        with pytest.raises(InvalidIndexTransitionError):
            IndexingService._transition(index, IndexStatus.INDEXED)


# --------------------------------------------------------------------------- #
# Reading and authorization
# --------------------------------------------------------------------------- #


class TestReading:
    def test_an_unknown_document_is_a_404(
        self, indexing_service: IndexingService, make_user: Any
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="read-admin@example.com")
        with pytest.raises(DocumentNotFoundError):
            indexing_service.get_index(uuid.uuid4(), actor=actor)

    def test_a_document_with_no_index_is_a_404(
        self, indexing_service: IndexingService, document: Any, make_user: Any
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="none-admin@example.com")
        with pytest.raises(DocumentIndexNotFoundError):
            indexing_service.get_index(document.id, actor=actor)

    def test_an_unassigned_lawyer_is_refused(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        make_user: Any,
    ) -> None:
        # Index access follows document access, which follows case access.
        indexing_service.schedule_for_ocr_result(extracted)
        stranger = make_user(role=UserRole.LAWYER, email="stranger@example.com")

        with pytest.raises(IndexAccessDeniedError):
            indexing_service.get_index(document.id, actor=stranger)

    def test_the_history_is_oldest_version_first(
        self,
        indexing_service: IndexingService,
        make_document_index: Any,
        document: Any,
        case: Any,
        make_user: Any,
    ) -> None:
        actor = make_user(role=UserRole.ADMINISTRATOR, email="hist-admin@example.com")
        make_document_index(document_id=document.id, case_id=case.id, document_version=2)
        make_document_index(document_id=document.id, case_id=case.id, document_version=1)

        history = indexing_service.list_document_indexes(document.id, actor=actor)
        assert [entry.document_version for entry in history] == [1, 2]


class TestMetrics:
    def test_it_reports_counts_and_configuration_only(
        self, indexing_service: IndexingService, extracted: Any
    ) -> None:
        indexing_service.schedule_for_ocr_result(extracted)
        metrics = indexing_service.metrics()

        assert metrics.counts.indexed == 1
        assert metrics.counts.total_chunks > 0
        assert metrics.embedding_model == "fake/test-embedder"
        assert metrics.embedding_dimensions == 8
        assert metrics.chunker == "recursive-character"
        assert metrics.collection.name == "test-chunks"

    def test_the_rates_complement_each_other(
        self,
        indexing_service: IndexingService,
        make_document_index: Any,
        document: Any,
        case: Any,
    ) -> None:
        make_document_index(
            document_id=document.id, case_id=case.id, status=IndexStatus.INDEXED
        )
        make_document_index(
            document_id=document.id,
            case_id=case.id,
            document_version=2,
            status=IndexStatus.FAILED,
            error_code="embedding_failure",
        )
        metrics = indexing_service.metrics()
        assert metrics.success_rate + metrics.failure_rate == 100.0

    def test_queued_work_does_not_move_the_rates(
        self,
        indexing_service: IndexingService,
        make_document_index: Any,
        document: Any,
        case: Any,
    ) -> None:
        # Counting queued runs would make the success rate dip on every upload
        # and recover as it processed, which measures traffic rather than
        # quality.
        make_document_index(
            document_id=document.id, case_id=case.id, status=IndexStatus.INDEXED
        )
        before = indexing_service.metrics().success_rate

        make_document_index(
            document_id=document.id,
            case_id=case.id,
            document_version=2,
            status=IndexStatus.PENDING,
        )
        assert indexing_service.metrics().success_rate == before

    def test_the_failure_breakdown_says_what_went_wrong(
        self,
        indexing_service: IndexingService,
        make_document_index: Any,
        document: Any,
        case: Any,
    ) -> None:
        make_document_index(
            document_id=document.id,
            case_id=case.id,
            status=IndexStatus.FAILED,
            error_code="vector_store_unavailable",
        )
        assert indexing_service.metrics().failures_by_code == {
            "vector_store_unavailable": 1
        }


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


class TestTimelinePublication:
    def test_a_successful_run_publishes_started_then_completed(
        self, indexing_service: IndexingService, extracted: Any, db_session: Session
    ) -> None:
        from models.timeline import TimelineEvent

        indexing_service.schedule_for_ocr_result(extracted)
        types = [
            event.event_type
            for event in db_session.query(TimelineEvent)
            .order_by(TimelineEvent.created_at)
            .all()
        ]
        assert types == [
            TimelineEventType.INDEXING_STARTED.value,
            TimelineEventType.INDEXING_COMPLETED.value,
        ]

    def test_a_failed_run_publishes_started_then_failed(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        embedder: Any,
        db_session: Session,
    ) -> None:
        from models.timeline import TimelineEvent

        embedder.raises = EmbeddingError("model exploded")
        indexing_service.schedule_for_ocr_result(extracted)

        types = [
            event.event_type
            for event in db_session.query(TimelineEvent)
            .order_by(TimelineEvent.created_at)
            .all()
        ]
        assert types == [
            TimelineEventType.INDEXING_STARTED.value,
            TimelineEventType.INDEXING_FAILED.value,
        ]

    def test_a_rebuild_publishes_retried(
        self,
        indexing_service: IndexingService,
        extracted: Any,
        document: Any,
        make_user: Any,
        db_session: Session,
    ) -> None:
        from models.timeline import TimelineEvent

        actor = make_user(role=UserRole.ADMINISTRATOR, email="tl-admin@example.com")
        indexing_service.schedule_for_ocr_result(extracted)
        indexing_service.reindex(document.id, actor=actor)

        types = [
            event.event_type
            for event in db_session.query(TimelineEvent)
            .order_by(TimelineEvent.created_at)
            .all()
        ]
        assert TimelineEventType.INDEXING_RETRIED.value in types

    def test_the_event_names_the_file_for_an_entitled_reader(
        self, indexing_service: IndexingService, extracted: Any, db_session: Session
    ) -> None:
        # The timeline is served only to users already entitled to the case; the
        # application log is not, which is why the filename appears in one and
        # never in the other.
        from models.timeline import TimelineEvent

        indexing_service.schedule_for_ocr_result(extracted)
        event = (
            db_session.query(TimelineEvent)
            .filter(TimelineEvent.event_type == TimelineEventType.INDEXING_COMPLETED.value)
            .one()
        )
        assert "bail.pdf" in (event.description or "")

    def test_no_event_carries_a_passage_of_the_document(
        self, indexing_service: IndexingService, extracted: Any, db_session: Session
    ) -> None:
        from models.timeline import TimelineEvent

        indexing_service.schedule_for_ocr_result(extracted)
        for event in db_session.query(TimelineEvent).all():
            rendered = f"{event.title} {event.description} {event.event_metadata}"
            assert "bailleur" not in rendered
            assert "Casablanca" not in rendered
