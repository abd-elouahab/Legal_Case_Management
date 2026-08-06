"""Unit tests for :class:`~services.ocr.OcrService`.

The real repository against the SQLite test database, the real access policy, the
real timeline — only the two genuinely external collaborators (object storage and
the OCR engine) are doubles. So the scheduling rules, the lifecycle, the claim,
the idempotency, and the failure handling are all genuinely exercised.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from core.exceptions import (
    DocumentNotFoundError,
    DocumentStorageError,
    DocumentVersionNotFoundError,
    InvalidOcrTransitionError,
    OcrAccessDeniedError,
    OcrAlreadyRunningError,
    OcrDisabledError,
    OcrResultNotFoundError,
    OcrUnsupportedFormatError,
)
from core.ocr import OcrFailureCode
from models.ocr import OcrStatus
from models.timeline import TimelineEventType
from models.user import UserRole
from repositories.ocr import OcrRepository
from schemas.ocr import OcrListQuery
from services.ocr import OcrService
from services.ocr_engine import (
    ExtractedPage,
    OcrCorruptedDocumentError,
    OcrEngineUnavailableError,
    OcrTimeoutError,
)
from services.ocr_queue import OcrJob
from tests.helpers import PDF_BYTES, PNG_BYTES

MakeUser = Any
MakeCase = Any
MakeDocument = Any
MakeOcrResult = Any


@pytest.fixture
def admin(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(
        email="lawyer@example.com",
        first_name="Yasmine",
        last_name="Haddad",
        role=UserRole.LAWYER,
    )


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer):  # type: ignore[no-untyped-def]
    return make_case(assigned_lawyer_id=lawyer.id)


@pytest.fixture
def document(make_document: MakeDocument, legal_case):  # type: ignore[no-untyped-def]
    return make_document(case_id=legal_case.id, extension="pdf", content=PDF_BYTES)


def job_for(result) -> OcrJob:  # type: ignore[no-untyped-def]
    return OcrJob(
        ocr_result_id=result.id,
        document_id=result.document_id,
        document_version=result.document_version,
    )


class TestScheduling:
    def test_it_queues_a_pending_run_for_a_supported_document(
        self, ocr_service: OcrService, ocr_queue, document, admin
    ) -> None:
        ocr_queue.run_inline = False

        result = ocr_service.schedule_for_document(document, actor=admin)

        assert result is not None
        assert result.status is OcrStatus.PENDING
        assert result.document_version == document.version
        assert len(ocr_queue.jobs) == 1

    def test_it_records_who_asked(
        self, ocr_service: OcrService, ocr_queue, document, admin
    ) -> None:
        ocr_queue.run_inline = False

        result = ocr_service.schedule_for_document(document, actor=admin)

        assert result is not None
        assert result.requested_by == admin.id

    def test_an_automatic_run_has_no_requester(
        self, ocr_service: OcrService, ocr_queue, document
    ) -> None:
        ocr_queue.run_inline = False

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        # Nobody *requested* it: it is what the platform does with an upload.
        assert result.requested_by is None

    @pytest.mark.parametrize("extension", ["docx", "doc", "txt"])
    def test_it_schedules_nothing_for_an_unsupported_type(
        self,
        ocr_service: OcrService,
        ocr_queue,
        make_document: MakeDocument,
        legal_case,
        extension: str,
    ) -> None:
        from tests.helpers import DOCX_BYTES, TXT_BYTES

        content = TXT_BYTES if extension == "txt" else DOCX_BYTES
        document = make_document(case_id=legal_case.id, extension=extension, content=content)

        assert ocr_service.schedule_for_document(document) is None
        assert ocr_queue.jobs == []

    def test_it_schedules_nothing_when_ocr_is_disabled(
        self, ocr_service: OcrService, ocr_queue, document, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "OCR_ENABLED", False)

        assert ocr_service.schedule_for_document(document) is None
        assert ocr_queue.jobs == []

    def test_scheduling_twice_reuses_the_run(
        self, ocr_service: OcrService, ocr_queue, document
    ) -> None:
        # Idempotency: retrying OCR for the same document version must not
        # create duplicate records.
        ocr_queue.run_inline = False

        first = ocr_service.schedule_for_document(document)
        second = ocr_service.schedule_for_document(document)

        assert first is not None and second is not None
        assert first.id == second.id
        # The second call does not re-queue: the first job is still pending.
        assert len(ocr_queue.jobs) == 1

    def test_a_scheduling_failure_never_propagates(
        self, ocr_service: OcrService, document, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The file is stored and the response is earned by the time this runs.
        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("database on fire")

        monkeypatch.setattr(ocr_service, "_schedule", explode)

        assert ocr_service.schedule_for_document(document) is None

    def test_it_publishes_no_timeline_event(
        self, ocr_service: OcrService, ocr_queue, document, db_session: Session
    ) -> None:
        from models.timeline import TimelineEvent

        ocr_queue.run_inline = False
        ocr_service.schedule_for_document(document)

        # Queueing is not an event a lawyer reads; "started" is. Recording both
        # would double every document's history for no added information.
        events = db_session.query(TimelineEvent).all()
        assert events == []


class TestProcessing:
    def test_it_completes_and_persists_the_text(
        self, ocr_service: OcrService, ocr_engine, document
    ) -> None:
        ocr_engine.pages = [
            ExtractedPage(page_number=1, text="Page one.", confidence=90.0),
            ExtractedPage(page_number=2, text="Page two.", confidence=80.0),
        ]

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.status is OcrStatus.COMPLETED
        assert [page.text for page in result.pages] == ["Page one.", "Page two."]
        assert [page.page_number for page in result.pages] == [1, 2]

    def test_it_records_the_metadata_the_spec_lists(
        self, ocr_service: OcrService, ocr_engine, document
    ) -> None:
        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.engine == "fake"
        assert result.engine_version == "5.0.0-fake"
        assert result.detected_language == "eng+fra"
        assert result.page_count == 1
        assert result.confidence == 91.5
        assert result.started_at is not None
        assert result.finished_at is not None
        assert result.duration_ms is not None and result.duration_ms >= 0
        assert result.attempt_count == 1

    def test_it_reads_the_stored_file_for_the_recorded_version(
        self, ocr_service: OcrService, ocr_engine, document
    ) -> None:
        ocr_service.schedule_for_document(document)

        assert ocr_engine.calls == [(len(PDF_BYTES), "pdf")]

    def test_pages_that_yield_no_text_still_complete(
        self, ocr_service: OcrService, ocr_engine, document
    ) -> None:
        # A blank separator sheet is a correct answer, not a failure — marking it
        # failed would invite retries that can never succeed.
        ocr_engine.pages = [ExtractedPage(page_number=1, text="", confidence=None)]

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.status is OcrStatus.COMPLETED
        assert result.character_count == 0

    def test_no_pages_at_all_is_a_failure(
        self, ocr_service: OcrService, ocr_engine, document
    ) -> None:
        ocr_engine.pages = []

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.status is OcrStatus.FAILED
        assert result.error_code == OcrFailureCode.UNREADABLE_DOCUMENT.value

    def test_an_orphaned_job_is_ignored(self, ocr_service: OcrService) -> None:
        outcome = ocr_service.process(
            OcrJob(ocr_result_id=uuid.uuid4(), document_id=uuid.uuid4(), document_version=1)
        )

        assert outcome is None

    def test_it_extracts_an_image(
        self, ocr_service: OcrService, ocr_engine, make_document: MakeDocument, legal_case
    ) -> None:
        document = make_document(case_id=legal_case.id, extension="png", content=PNG_BYTES)

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.status is OcrStatus.COMPLETED
        assert ocr_engine.calls == [(len(PNG_BYTES), "png")]


class TestConcurrency:
    def test_a_run_is_claimed_exactly_once(
        self, ocr_service: OcrService, ocr_queue, document, db_session: Session
    ) -> None:
        # The whole of the "prevent multiple OCR jobs from processing the same
        # document simultaneously" requirement: two workers handed the same job
        # must not both process it.
        ocr_queue.run_inline = False
        result = ocr_service.schedule_for_document(document)
        assert result is not None

        repository = OcrRepository(db_session)

        assert repository.claim(result.id) is True
        assert repository.claim(result.id) is False

    def test_processing_an_already_claimed_job_is_a_no_op(
        self, ocr_service: OcrService, ocr_queue, document, db_session: Session
    ) -> None:
        ocr_queue.run_inline = False
        result = ocr_service.schedule_for_document(document)
        assert result is not None

        # A second worker "wins" the claim first.
        OcrRepository(db_session).claim(result.id)

        assert ocr_service.process(job_for(result)) is None

    def test_processing_twice_does_not_duplicate_pages(
        self, ocr_service: OcrService, ocr_queue, ocr_engine, document
    ) -> None:
        result = ocr_service.schedule_for_document(document)
        assert result is not None

        # The queue re-delivering a job it already ran must change nothing: the
        # run is no longer `pending`, so the claim refuses it.
        ocr_service.process(job_for(result))

        assert len(result.pages) == 1
        assert result.attempt_count == 1

    def test_the_claim_stamps_the_start_and_counts_the_attempt(
        self, ocr_service: OcrService, ocr_queue, document, db_session: Session
    ) -> None:
        ocr_queue.run_inline = False
        result = ocr_service.schedule_for_document(document)
        assert result is not None

        repository = OcrRepository(db_session)
        repository.claim(result.id)
        claimed = repository.get_by_id(result.id)

        assert claimed is not None
        assert claimed.status is OcrStatus.PROCESSING
        assert claimed.started_at is not None
        assert claimed.attempt_count == 1


class TestFailureHandling:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (OcrCorruptedDocumentError("bad"), OcrFailureCode.CORRUPTED_DOCUMENT),
            (OcrTimeoutError("slow"), OcrFailureCode.TIMEOUT),
            (OcrEngineUnavailableError("missing"), OcrFailureCode.ENGINE_FAILURE),
        ],
    )
    def test_an_engine_error_becomes_a_failed_run(
        self, ocr_service: OcrService, ocr_engine, document, error: Exception, expected
    ) -> None:
        ocr_engine.raises = error

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.status is OcrStatus.FAILED
        assert result.error_code == expected.value
        assert result.error_message

    def test_a_storage_failure_becomes_a_failed_run(
        self, ocr_service: OcrService, document_storage, document
    ) -> None:
        document_storage.objects.clear()

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.status is OcrStatus.FAILED
        assert result.error_code == OcrFailureCode.STORAGE_FAILURE.value

    def test_an_unexpected_error_still_ends_the_run(
        self, ocr_service: OcrService, ocr_engine, document
    ) -> None:
        # The one state nothing can recover from is `processing` forever.
        ocr_engine.raises = ValueError("something nobody anticipated")

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.status is OcrStatus.FAILED
        assert result.error_code == OcrFailureCode.UNKNOWN.value

    def test_a_failure_leaves_the_document_and_its_file_untouched(
        self, ocr_service: OcrService, ocr_engine, document_storage, document
    ) -> None:
        ocr_engine.raises = OcrCorruptedDocumentError("bad")
        before = dict(document_storage.objects)
        filename = document.original_filename

        ocr_service.schedule_for_document(document)

        assert document_storage.objects == before
        assert document.original_filename == filename
        assert document.deleted_at is None

    def test_the_error_message_never_quotes_the_engine(
        self, ocr_service: OcrService, ocr_engine, document
    ) -> None:
        # pytesseract surfaces Tesseract's stderr verbatim, which can echo the
        # page it was reading.
        ocr_engine.raises = OcrCorruptedDocumentError("Estimating resolution: MAUDIT CONTRAT")

        result = ocr_service.schedule_for_document(document)

        assert result is not None
        assert result.error_message is not None
        assert "MAUDIT" not in result.error_message


class TestRetry:
    def test_a_failed_run_can_be_retried(
        self, ocr_service: OcrService, ocr_engine, document, admin
    ) -> None:
        ocr_engine.raises = OcrCorruptedDocumentError("bad")
        failed = ocr_service.schedule_for_document(document)
        assert failed is not None and failed.status is OcrStatus.FAILED

        ocr_engine.raises = None
        retried = ocr_service.retry(document.id, actor=admin)

        assert retried.id == failed.id
        assert retried.status is OcrStatus.COMPLETED
        assert retried.error_code is None
        assert retried.error_message is None

    def test_a_retry_reuses_the_row_rather_than_creating_a_second(
        self, ocr_service: OcrService, document, admin, db_session: Session
    ) -> None:
        from models.ocr import OcrResult

        first = ocr_service.schedule_for_document(document)
        assert first is not None

        ocr_service.retry(document.id, actor=admin)

        rows = db_session.query(OcrResult).filter_by(document_id=document.id).all()
        assert len(rows) == 1
        assert rows[0].id == first.id

    def test_a_retry_increments_the_attempt_count(
        self, ocr_service: OcrService, document, admin
    ) -> None:
        ocr_service.schedule_for_document(document)

        retried = ocr_service.retry(document.id, actor=admin)

        assert retried.attempt_count == 2

    def test_a_retry_replaces_the_previous_pages(
        self, ocr_service: OcrService, ocr_engine, document, admin
    ) -> None:
        ocr_engine.pages = [
            ExtractedPage(page_number=1, text="one"),
            ExtractedPage(page_number=2, text="two"),
            ExtractedPage(page_number=3, text="three"),
        ]
        ocr_service.schedule_for_document(document)

        # A retry is a new *reading*: a merge would leave page 3 from the first
        # attempt behind a second attempt that produced fewer pages.
        ocr_engine.pages = [ExtractedPage(page_number=1, text="only")]
        retried = ocr_service.retry(document.id, actor=admin)

        assert [page.text for page in retried.pages] == ["only"]

    def test_a_retry_records_who_asked(
        self, ocr_service: OcrService, document, admin, lawyer
    ) -> None:
        ocr_service.schedule_for_document(document, actor=admin)

        retried = ocr_service.retry(document.id, actor=lawyer)

        assert retried.requested_by == lawyer.id

    def test_a_running_run_cannot_be_retried(
        self, ocr_service: OcrService, ocr_queue, document, admin
    ) -> None:
        ocr_queue.run_inline = False
        ocr_service.schedule_for_document(document)

        with pytest.raises(OcrAlreadyRunningError):
            ocr_service.retry(document.id, actor=admin)

    def test_a_version_never_processed_is_bootstrapped(
        self, ocr_service: OcrService, document, admin
    ) -> None:
        # Uploaded while OCR was disabled, or before the feature existed.
        retried = ocr_service.retry(document.id, actor=admin)

        assert retried.status is OcrStatus.COMPLETED
        assert retried.document_version == document.version

    def test_an_unsupported_type_is_refused(
        self, ocr_service: OcrService, make_document: MakeDocument, legal_case, admin
    ) -> None:
        from tests.helpers import DOCX_BYTES

        document = make_document(case_id=legal_case.id, extension="docx", content=DOCX_BYTES)

        with pytest.raises(OcrUnsupportedFormatError):
            ocr_service.retry(document.id, actor=admin)

    def test_a_disabled_platform_refuses_a_retry(
        self, ocr_service: OcrService, document, admin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "OCR_ENABLED", False)

        with pytest.raises(OcrDisabledError):
            ocr_service.retry(document.id, actor=admin)

    def test_an_unknown_document_is_a_404(self, ocr_service: OcrService, admin) -> None:
        with pytest.raises(DocumentNotFoundError):
            ocr_service.retry(uuid.uuid4(), actor=admin)

    def test_an_unknown_version_is_a_404(
        self, ocr_service: OcrService, document, admin
    ) -> None:
        with pytest.raises(DocumentVersionNotFoundError):
            ocr_service.retry(document.id, actor=admin, version=99)

    def test_it_publishes_a_retried_event(
        self, ocr_service: OcrService, document, admin, db_session: Session
    ) -> None:
        from models.timeline import TimelineEvent

        ocr_service.retry(document.id, actor=admin)

        types = [event.event_type for event in db_session.query(TimelineEvent).all()]
        assert TimelineEventType.OCR_RETRIED.value in types


class TestVersioning:
    def test_each_version_gets_its_own_run(
        self, ocr_service: OcrService, document, db_session: Session
    ) -> None:
        from models.document import DocumentVersion

        first = ocr_service.schedule_for_document(document)
        assert first is not None

        # Simulate a replacement: version 2 of the same document.
        document.versions.append(
            DocumentVersion(
                id=uuid.uuid4(),
                document_id=document.id,
                version=2,
                original_filename=document.original_filename,
                stored_filename=document.stored_filename,
                file_extension="pdf",
                mime_type=document.mime_type,
                file_size=document.file_size,
                storage_bucket=document.storage_bucket,
                storage_key=document.storage_key,
            )
        )
        document.version = 2
        db_session.commit()

        second = ocr_service.schedule_for_document(document)

        assert second is not None
        assert second.id != first.id
        assert second.document_version == 2

    def test_the_earlier_versions_text_survives_a_replacement(
        self, ocr_service: OcrService, ocr_engine, document, admin, db_session: Session
    ) -> None:
        from models.document import DocumentVersion

        ocr_engine.pages = [ExtractedPage(page_number=1, text="Version one text.")]
        ocr_service.schedule_for_document(document)

        document.versions.append(
            DocumentVersion(
                id=uuid.uuid4(),
                document_id=document.id,
                version=2,
                original_filename=document.original_filename,
                stored_filename=document.stored_filename,
                file_extension="pdf",
                mime_type=document.mime_type,
                file_size=document.file_size,
                storage_bucket=document.storage_bucket,
                storage_key=document.storage_key,
            )
        )
        document.version = 2
        db_session.commit()

        ocr_engine.pages = [ExtractedPage(page_number=1, text="Version two text.")]
        ocr_service.schedule_for_document(document)

        original = ocr_service.get_result(document.id, actor=admin, version=1)
        assert [page.text for page in original.pages] == ["Version one text."]


class TestReading:
    def test_it_returns_the_current_versions_run_by_default(
        self, ocr_service: OcrService, document, admin
    ) -> None:
        scheduled = ocr_service.schedule_for_document(document)
        assert scheduled is not None

        assert ocr_service.get_result(document.id, actor=admin).id == scheduled.id

    def test_a_document_with_no_run_is_a_404(
        self, ocr_service: OcrService, document, admin
    ) -> None:
        with pytest.raises(OcrResultNotFoundError):
            ocr_service.get_result(document.id, actor=admin)

    def test_a_deleted_document_hides_its_text(
        self, ocr_service: OcrService, document, admin, db_session: Session
    ) -> None:
        ocr_service.schedule_for_document(document)
        document.deleted_at = datetime.now(UTC)
        db_session.commit()

        # The text inherits the document's permissions, and the document is no
        # longer readable.
        with pytest.raises(DocumentNotFoundError):
            ocr_service.get_result(document.id, actor=admin)

    def test_the_history_lists_every_version(
        self, ocr_service: OcrService, document, admin, db_session: Session
    ) -> None:
        from models.document import DocumentVersion

        ocr_service.schedule_for_document(document)
        document.versions.append(
            DocumentVersion(
                id=uuid.uuid4(),
                document_id=document.id,
                version=2,
                original_filename=document.original_filename,
                stored_filename=document.stored_filename,
                file_extension="pdf",
                mime_type=document.mime_type,
                file_size=document.file_size,
                storage_bucket=document.storage_bucket,
                storage_key=document.storage_key,
            )
        )
        document.version = 2
        db_session.commit()
        ocr_service.schedule_for_document(document)

        history = ocr_service.list_document_results(document.id, actor=admin)

        assert [run.document_version for run in history] == [1, 2]


class TestAuthorization:
    def test_an_unassigned_lawyer_is_refused(
        self, ocr_service: OcrService, document, make_user: MakeUser
    ) -> None:
        stranger = make_user(email="stranger@example.com", role=UserRole.LAWYER)
        ocr_service.schedule_for_document(document)

        with pytest.raises(OcrAccessDeniedError):
            ocr_service.get_result(document.id, actor=stranger)

    def test_the_assigned_lawyer_may_read(
        self, ocr_service: OcrService, document, lawyer
    ) -> None:
        ocr_service.schedule_for_document(document)

        assert ocr_service.get_result(document.id, actor=lawyer) is not None

    def test_an_unassigned_lawyer_cannot_retry(
        self, ocr_service: OcrService, document, make_user: MakeUser
    ) -> None:
        stranger = make_user(email="stranger@example.com", role=UserRole.LAWYER)

        with pytest.raises(OcrAccessDeniedError):
            ocr_service.retry(document.id, actor=stranger)

    def test_the_list_is_scoped_to_the_callers_cases(
        self,
        ocr_service: OcrService,
        document,
        lawyer,
        admin,
        make_case: MakeCase,
        make_document: MakeDocument,
    ) -> None:
        other_case = make_case(title="Unrelated")
        other = make_document(case_id=other_case.id, extension="pdf", content=PDF_BYTES)
        ocr_service.schedule_for_document(document)
        ocr_service.schedule_for_document(other)

        lawyer_page = ocr_service.list_results(OcrListQuery(), actor=lawyer)
        admin_page = ocr_service.list_results(OcrListQuery(), actor=admin)

        # The scope is applied in SQL, so the *total* counts only what the caller
        # may reach rather than being filtered afterwards.
        assert lawyer_page.total == 1
        assert admin_page.total == 2


class TestTimelinePublication:
    def test_a_successful_run_publishes_started_then_completed(
        self, ocr_service: OcrService, document, db_session: Session
    ) -> None:
        from models.timeline import TimelineEvent

        ocr_service.schedule_for_document(document)

        types = [
            event.event_type
            for event in db_session.query(TimelineEvent)
            .order_by(TimelineEvent.created_at)
            .all()
        ]
        # A *prefix*, not the whole list. A completed extraction hands the
        # pipeline on to indexing (spec 10), which publishes its own events after
        # these two — asserting equality here would fail every time a later stage
        # correctly attached itself to the same document, which is exactly what
        # the OCR service's `IndexScheduler` seam exists to allow.
        assert types[:2] == [
            TimelineEventType.OCR_STARTED.value,
            TimelineEventType.OCR_COMPLETED.value,
        ]
        # And the OCR pair is complete: neither event appears twice, and nothing
        # of OCR's is published after the hand-off.
        assert [entry for entry in types if entry.startswith("ocr_")] == types[:2]

    def test_a_failed_run_publishes_started_then_failed(
        self, ocr_service: OcrService, ocr_engine, document, db_session: Session
    ) -> None:
        from models.timeline import TimelineEvent

        ocr_engine.raises = OcrTimeoutError("slow")
        ocr_service.schedule_for_document(document)

        types = [
            event.event_type
            for event in db_session.query(TimelineEvent)
            .order_by(TimelineEvent.created_at)
            .all()
        ]
        assert types == [
            TimelineEventType.OCR_STARTED.value,
            TimelineEventType.OCR_FAILED.value,
        ]

    def test_the_event_carries_the_filename_and_the_run(
        self, ocr_service: OcrService, document, db_session: Session
    ) -> None:
        from models.timeline import TimelineEvent

        result = ocr_service.schedule_for_document(document)
        assert result is not None

        event = (
            db_session.query(TimelineEvent)
            .filter_by(event_type=TimelineEventType.OCR_COMPLETED.value)
            .one()
        )
        assert event.event_metadata["filename"] == document.original_filename
        assert event.event_metadata["ocr_result_id"] == str(result.id)
        assert event.case_id == document.case_id

    def test_the_timeline_never_carries_the_extracted_text(
        self, ocr_service: OcrService, ocr_engine, document, db_session: Session
    ) -> None:
        from models.timeline import TimelineEvent

        secret = "CLAUSE PENALE CONFIDENTIELLE"
        ocr_engine.pages = [ExtractedPage(page_number=1, text=secret)]
        ocr_service.schedule_for_document(document)

        for event in db_session.query(TimelineEvent).all():
            assert secret not in (event.description or "")
            assert secret not in str(event.event_metadata)


class TestTransitionGuard:
    def test_an_illegal_transition_is_refused(self, ocr_service: OcrService, document) -> None:
        from models.ocr import OcrResult

        result = OcrResult(
            id=uuid.uuid4(),
            document_id=document.id,
            document_version=1,
            status=OcrStatus.PENDING,
        )

        # A run that reached `completed` without processing would make its
        # duration and its start time both a lie.
        with pytest.raises(InvalidOcrTransitionError):
            OcrService._transition(result, OcrStatus.COMPLETED)


class TestMetrics:
    def test_it_reports_the_rates_over_finished_runs(
        self, ocr_service: OcrService, document, make_ocr_result: MakeOcrResult
    ) -> None:
        make_ocr_result(document_id=document.id, document_version=1, status=OcrStatus.COMPLETED)
        make_ocr_result(
            document_id=document.id,
            document_version=2,
            status=OcrStatus.FAILED,
            error_code=OcrFailureCode.TIMEOUT.value,
        )
        make_ocr_result(document_id=document.id, document_version=3, status=OcrStatus.PENDING)

        metrics = ocr_service.metrics()

        assert metrics.counts.total == 3
        assert metrics.counts.finished == 2
        assert metrics.success_rate == 50.0
        assert metrics.failure_rate == 50.0

    def test_the_rates_sum_to_one_hundred(
        self, ocr_service: OcrService, document, make_ocr_result: MakeOcrResult
    ) -> None:
        for version in range(1, 4):
            make_ocr_result(
                document_id=document.id, document_version=version, status=OcrStatus.COMPLETED
            )
        make_ocr_result(document_id=document.id, document_version=4, status=OcrStatus.FAILED)
        make_ocr_result(document_id=document.id, document_version=5, status=OcrStatus.FAILED)

        metrics = ocr_service.metrics()

        assert metrics.success_rate + metrics.failure_rate == 100.0

    def test_no_runs_reports_zero_rather_than_dividing_by_zero(
        self, ocr_service: OcrService
    ) -> None:
        metrics = ocr_service.metrics()

        assert metrics.counts.total == 0
        assert metrics.success_rate == 0.0
        assert metrics.failure_rate == 0.0

    def test_the_average_duration_excludes_failures(
        self, ocr_service: OcrService, document, make_ocr_result: MakeOcrResult
    ) -> None:
        # A timeout contributes the timeout; averaging it in would answer a
        # different question from "how long does extraction take".
        make_ocr_result(
            document_id=document.id,
            document_version=1,
            status=OcrStatus.COMPLETED,
            duration_ms=1000,
        )
        make_ocr_result(
            document_id=document.id,
            document_version=2,
            status=OcrStatus.FAILED,
            duration_ms=180_000,
        )

        assert ocr_service.metrics().counts.average_duration_ms == 1000

    def test_failures_are_grouped_by_cause(
        self, ocr_service: OcrService, document, make_ocr_result: MakeOcrResult
    ) -> None:
        make_ocr_result(
            document_id=document.id,
            document_version=1,
            status=OcrStatus.FAILED,
            error_code=OcrFailureCode.TIMEOUT.value,
        )
        make_ocr_result(
            document_id=document.id,
            document_version=2,
            status=OcrStatus.FAILED,
            error_code=OcrFailureCode.TIMEOUT.value,
        )
        make_ocr_result(
            document_id=document.id,
            document_version=3,
            status=OcrStatus.FAILED,
            error_code=OcrFailureCode.ENGINE_FAILURE.value,
        )

        assert ocr_service.metrics().failures_by_code == {"timeout": 2, "engine_failure": 1}

    def test_a_window_restricts_the_figures(
        self, ocr_service: OcrService, document, make_ocr_result: MakeOcrResult
    ) -> None:
        make_ocr_result(
            document_id=document.id,
            document_version=1,
            status=OcrStatus.COMPLETED,
            created_at=datetime.now(UTC) - timedelta(days=30),
        )
        make_ocr_result(document_id=document.id, document_version=2, status=OcrStatus.COMPLETED)

        assert ocr_service.metrics(window_days=7).counts.total == 1
        assert ocr_service.metrics().counts.total == 2

    def test_it_reports_whether_the_engine_is_installed(
        self, ocr_service: OcrService, ocr_engine
    ) -> None:
        # The difference between "documents are unreadable" and "the platform
        # cannot read anything", which the rates alone cannot tell apart.
        assert ocr_service.metrics().engine_available is True

        ocr_engine.available = False
        assert ocr_service.metrics().engine_available is False


class TestRequeue:
    def test_it_requeues_pending_runs(
        self, ocr_service: OcrService, ocr_queue, document
    ) -> None:
        ocr_queue.run_inline = False
        ocr_service.schedule_for_document(document)
        ocr_queue.jobs.clear()

        assert ocr_service.requeue_pending() == 1
        assert len(ocr_queue.jobs) == 1

    def test_it_leaves_finished_runs_alone(
        self, ocr_service: OcrService, ocr_queue, document
    ) -> None:
        ocr_service.schedule_for_document(document)
        ocr_queue.jobs.clear()

        assert ocr_service.requeue_pending() == 0
        assert ocr_queue.jobs == []

    def test_it_does_nothing_when_ocr_is_disabled(
        self,
        ocr_service: OcrService,
        ocr_queue,
        document,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.config import settings

        ocr_queue.run_inline = False
        ocr_service.schedule_for_document(document)
        ocr_queue.jobs.clear()
        monkeypatch.setattr(settings, "OCR_ENABLED", False)

        assert ocr_service.requeue_pending() == 0


class TestStorageFailureIsolation:
    def test_the_run_stays_retryable_after_a_storage_outage(
        self, ocr_service: OcrService, document_storage, document, admin
    ) -> None:
        stored = dict(document_storage.objects)
        document_storage.objects.clear()
        failed = ocr_service.schedule_for_document(document)
        assert failed is not None and failed.status is OcrStatus.FAILED

        document_storage.objects.update(stored)
        recovered = ocr_service.retry(document.id, actor=admin)

        assert recovered.status is OcrStatus.COMPLETED

    def test_a_storage_error_is_not_raised_to_the_caller(
        self, ocr_service: OcrService, document_storage, document
    ) -> None:
        document_storage.objects.clear()

        # No DocumentStorageError escapes: a background run records its failure.
        try:
            ocr_service.schedule_for_document(document)
        except DocumentStorageError:  # pragma: no cover - the assertion is the absence
            pytest.fail("a storage failure must not escape the background run")
