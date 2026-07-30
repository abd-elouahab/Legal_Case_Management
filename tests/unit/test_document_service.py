"""Unit tests for :mod:`services.document`.

The *real* repository runs against SQLite in-memory, so the search, filter, sort,
pagination, and scope SQL is genuinely exercised without a container; only object
storage is a double. Everything the service owns — versioning, the current-version
mirror, soft delete, per-resource access — is asserted here rather than only
through HTTP.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.orm import Session

from core.exceptions import (
    CaseNotFoundError,
    DocumentAccessDeniedError,
    DocumentNotFoundError,
    DocumentPreviewUnavailableError,
    DocumentStorageError,
    DocumentVersionNotFoundError,
)
from models.case import Case
from models.document import Document, DocumentCategory
from models.user import User, UserRole
from repositories.case import CaseRepository
from repositories.document import DocumentRepository
from schemas.case import SortOrder
from schemas.document import (
    DocumentListQuery,
    DocumentSortField,
    DocumentUpdate,
    DocumentUploadForm,
)
from services.document import DocumentService
from services.document_storage import DocumentStorageService
from services.document_validation import ValidatedUpload, validate_upload
from tests.helpers import DOCX_BYTES, PDF_BYTES, PNG_BYTES, TXT_BYTES

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see progress-tracker
    from tests.conftest import InMemoryDocumentStorage


@pytest.fixture
def service(  # type: ignore[no-untyped-def]
    db_session: Session, document_storage
) -> DocumentService:
    """A service wired to the real repositories and the fake object store."""
    return DocumentService(
        DocumentRepository(db_session),
        CaseRepository(db_session),
        cast(DocumentStorageService, document_storage),
    )


def _upload(
    filename: str = "contract.pdf", payload: bytes = PDF_BYTES
) -> ValidatedUpload:
    return validate_upload(filename=filename, stream=io.BytesIO(payload))


def _form(case_id: uuid.UUID, **overrides: object) -> DocumentUploadForm:
    return DocumentUploadForm(case_id=case_id, **overrides)  # type: ignore[arg-type]


class TestUpload:
    def test_it_stores_the_file_and_records_version_1(
        self,
        service: DocumentService,
        document_storage: InMemoryDocumentStorage,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        legal_case = make_case()

        document = service.upload_document(
            _form(legal_case.id, category=DocumentCategory.CONTRACT, description="Bail commercial"),
            _upload(),
            actor=admin,
        )

        assert document.version == 1
        assert document.category is DocumentCategory.CONTRACT
        assert document.description == "Bail commercial"
        assert document.uploaded_by == admin.id
        assert [entry.version for entry in document.versions] == [1]
        # The bytes actually reached object storage, not just the row.
        assert document_storage.objects[document.storage_key] == PDF_BYTES

    def test_the_original_filename_is_preserved(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        document = service.upload_document(
            _form(make_case().id), _upload("Contrat de bail.pdf"), actor=make_user()
        )

        assert document.original_filename == "Contrat de bail.pdf"
        # …while the stored name is generated, so a crafted filename cannot
        # influence the storage layout.
        assert document.stored_filename != document.original_filename

    def test_the_storage_key_is_scoped_to_the_case_and_version(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        legal_case = make_case()

        document = service.upload_document(_form(legal_case.id), _upload(), actor=make_user())

        assert document.storage_key.startswith(f"cases/{legal_case.id}/documents/{document.id}/v1/")

    def test_an_unknown_case_is_rejected(
        self, service: DocumentService, make_user: Callable[..., User]
    ) -> None:
        with pytest.raises(CaseNotFoundError):
            service.upload_document(_form(uuid.uuid4()), _upload(), actor=make_user())

    def test_a_lawyer_cannot_upload_to_a_case_they_are_not_assigned_to(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)

        with pytest.raises(DocumentAccessDeniedError):
            service.upload_document(_form(make_case().id), _upload(), actor=lawyer)

    def test_a_lawyer_can_upload_to_their_assigned_case(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        document = service.upload_document(_form(legal_case.id), _upload(), actor=lawyer)

        assert document.case_id == legal_case.id

    def test_a_storage_failure_leaves_no_metadata_behind(
        self,
        service: DocumentService,
        db_session: Session,
        document_storage: InMemoryDocumentStorage,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        # Storage runs first, deliberately: the reverse order would let a
        # committed row point at an object that was never written.
        document_storage.fail_next_upload = True

        with pytest.raises(DocumentStorageError):
            service.upload_document(_form(make_case().id), _upload(), actor=make_user())

        assert db_session.query(Document).count() == 0


class TestReplace:
    def test_it_adds_a_version_and_preserves_the_previous_file(
        self,
        service: DocumentService,
        document_storage: InMemoryDocumentStorage,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)
        original_key = document.storage_key

        replaced = service.replace_document(
            document.id, _upload("scan.png", PNG_BYTES), actor=admin
        )

        assert replaced.version == 2
        assert [entry.version for entry in replaced.versions] == [1, 2]
        # Never overwritten: both objects are still there, under different keys.
        assert replaced.storage_key != original_key
        assert document_storage.objects[original_key] == PDF_BYTES
        assert document_storage.objects[replaced.storage_key] == PNG_BYTES

    def test_the_identifier_and_metadata_survive_a_replacement(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(
            _form(make_case().id, category=DocumentCategory.EVIDENCE, description="Pièce 3"),
            _upload(),
            actor=admin,
        )

        replaced = service.replace_document(document.id, _upload(), actor=admin)

        # Existing links keep working, and category/description describe the
        # document rather than the particular file.
        assert replaced.id == document.id
        assert replaced.category is DocumentCategory.EVIDENCE
        assert replaced.description == "Pièce 3"

    def test_the_document_row_mirrors_the_new_current_version(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)

        replaced = service.replace_document(
            document.id, _upload("minutes.txt", TXT_BYTES), actor=admin
        )
        current = replaced.current_version

        assert current is not None
        assert replaced.original_filename == current.original_filename == "minutes.txt"
        assert replaced.file_extension == current.file_extension == "txt"
        assert replaced.file_size == current.file_size == len(TXT_BYTES)
        assert replaced.storage_key == current.storage_key

    def test_three_replacements_build_a_complete_history(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)

        for _ in range(3):
            document = service.replace_document(document.id, _upload(), actor=admin)

        assert [entry.version for entry in document.versions] == [1, 2, 3, 4]
        # Every version still has its own object.
        assert len({entry.storage_key for entry in document.versions}) == 4

    def test_replacing_a_deleted_document_is_rejected(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)
        service.delete_document(document.id, actor=admin)

        with pytest.raises(DocumentNotFoundError):
            service.replace_document(document.id, _upload(), actor=admin)


class TestMetadataUpdate:
    def test_it_changes_only_what_was_sent(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(
            _form(make_case().id, category=DocumentCategory.OTHER, description="Original"),
            _upload(),
            actor=admin,
        )

        updated = service.update_document(
            document.id, DocumentUpdate(category=DocumentCategory.INVOICE), actor=admin
        )

        assert updated.category is DocumentCategory.INVOICE
        assert updated.description == "Original"

    def test_an_explicit_null_clears_the_description(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(
            _form(make_case().id, description="Original"), _upload(), actor=admin
        )

        assert service.update_document(
            document.id, DocumentUpdate(description=None), actor=admin
        ).description is None

    def test_the_binary_is_untouched(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)
        before = (document.storage_key, document.version, document.original_filename)

        updated = service.update_document(
            document.id, DocumentUpdate(category=DocumentCategory.PLEADING), actor=admin
        )

        assert (updated.storage_key, updated.version, updated.original_filename) == before


class TestDelete:
    def test_it_is_a_soft_delete_that_keeps_the_bytes(
        self,
        service: DocumentService,
        db_session: Session,
        document_storage: InMemoryDocumentStorage,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)
        key = document.storage_key

        deleted = service.delete_document(document.id, actor=admin)

        assert deleted.deleted_at is not None
        assert db_session.get(Document, document.id) is not None
        # Permanently destroying a legal document is forbidden; the deletion is
        # recorded against the object rather than applied to it.
        assert document_storage.objects[key] == PDF_BYTES
        assert key in document_storage.logical_deletes

    def test_a_deleted_document_is_no_longer_readable(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)
        service.delete_document(document.id, actor=admin)

        with pytest.raises(DocumentNotFoundError):
            service.get_document(document.id, actor=admin)

    def test_it_is_idempotent(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)

        first = service.delete_document(document.id, actor=admin)
        second = service.delete_document(document.id, actor=admin)

        assert second.deleted_at == first.deleted_at

    def test_an_unknown_document_is_a_404(
        self, service: DocumentService, make_user: Callable[..., User]
    ) -> None:
        with pytest.raises(DocumentNotFoundError):
            service.delete_document(uuid.uuid4(), actor=make_user())


class TestServing:
    def test_download_serves_the_current_version(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)
        service.replace_document(document.id, _upload("v2.txt", TXT_BYTES), actor=admin)

        download = service.open_download(document.id, actor=admin)

        assert download.version == 2
        assert download.filename == "v2.txt"
        assert b"".join(download.stream) == TXT_BYTES

    def test_a_previous_version_stays_downloadable(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)
        service.replace_document(document.id, _upload("v2.txt", TXT_BYTES), actor=admin)

        download = service.open_download(document.id, actor=admin, version=1)

        assert download.version == 1
        assert b"".join(download.stream) == PDF_BYTES

    def test_an_unknown_version_is_a_404(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)

        with pytest.raises(DocumentVersionNotFoundError):
            service.open_download(document.id, actor=admin, version=9)

    def test_preview_works_for_a_renderable_type(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(_form(make_case().id), _upload(), actor=admin)

        assert service.open_preview(document.id, actor=admin).mime_type == "application/pdf"

    def test_preview_is_refused_for_a_word_document(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        admin = make_user()
        document = service.upload_document(
            _form(make_case().id), _upload("brief.docx", DOCX_BYTES), actor=admin
        )

        with pytest.raises(DocumentPreviewUnavailableError):
            service.open_preview(document.id, actor=admin)


class TestPerResourceAccess:
    def test_a_lawyer_reads_only_documents_on_their_cases(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        mine = make_document(case_id=make_case(assigned_lawyer_id=lawyer.id).id)
        theirs = make_document(case_id=make_case().id)

        assert service.get_document(mine.id, actor=lawyer).id == mine.id
        with pytest.raises(DocumentAccessDeniedError):
            service.get_document(theirs.id, actor=lawyer)

    def test_a_court_representative_reads_their_assigned_cases(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        court = make_user(email="court@example.com", role=UserRole.COURT_REPRESENTATIVE)
        document = make_document(
            case_id=make_case(assigned_court_representative_id=court.id).id
        )

        assert service.get_document(document.id, actor=court).id == document.id

    def test_the_list_total_counts_only_reachable_documents(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        make_document(case_id=make_case(assigned_lawyer_id=lawyer.id).id)
        make_document(case_id=make_case().id)
        make_document(case_id=make_case().id)

        scoped = service.list_documents(DocumentListQuery(), actor=lawyer)
        unscoped = service.list_documents(DocumentListQuery(), actor=make_user())

        # Applied in SQL, so the total cannot reveal how many documents the
        # lawyer is not allowed to see.
        assert scoped.total == 1
        assert unscoped.total == 3

    def test_a_lawyer_cannot_read_a_document_by_guessing_its_id(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        document = make_document(case_id=make_case().id)

        with pytest.raises(DocumentAccessDeniedError):
            service.open_download(document.id, actor=lawyer)


class TestSearch:
    @pytest.fixture(autouse=True)
    def _documents(  # type: ignore[no-untyped-def]
        self, make_case: Callable[..., Case], make_document: Callable[..., Document]
    ):
        case_id = make_case().id
        self.contract = make_document(
            case_id=case_id,
            original_filename="Contrat de bail.pdf",
            category=DocumentCategory.CONTRACT,
            description="Bail commercial signé",
        )
        self.decision = make_document(
            case_id=case_id,
            original_filename="jugement.pdf",
            category=DocumentCategory.COURT_DECISION,
            description=None,
        )
        self.case_id = case_id

    def _search(self, service: DocumentService, actor: User, term: str) -> list[uuid.UUID]:
        return [
            document.id
            for document in service.list_documents(
                DocumentListQuery(search=term), actor=actor
            ).documents
        ]

    def test_it_matches_the_filename_case_insensitively(
        self, service: DocumentService, make_user: Callable[..., User]
    ) -> None:
        assert self._search(service, make_user(), "CONTRAT") == [self.contract.id]

    def test_it_matches_the_description(
        self, service: DocumentService, make_user: Callable[..., User]
    ) -> None:
        assert self._search(service, make_user(), "commercial") == [self.contract.id]

    def test_it_matches_a_category_name(
        self, service: DocumentService, make_user: Callable[..., User]
    ) -> None:
        # Spelled as a human reads it, not as it is stored.
        assert self._search(service, make_user(), "court decision") == [self.decision.id]

    def test_a_wildcard_is_matched_literally(
        self, service: DocumentService, make_user: Callable[..., User]
    ) -> None:
        # An unescaped `%` would match every document, which reads as a broken
        # filter rather than as the injection-shaped bug it is.
        assert self._search(service, make_user(), "%") == []


class TestFilters:
    def test_they_combine(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        admin = make_user()
        other = make_user(email="other@example.com")
        case_id = make_case().id

        wanted = make_document(
            case_id=case_id,
            category=DocumentCategory.EVIDENCE,
            extension="pdf",
            uploaded_by=admin.id,
        )
        make_document(case_id=case_id, category=DocumentCategory.EVIDENCE, uploaded_by=other.id)
        make_document(
            case_id=case_id,
            category=DocumentCategory.INVOICE,
            extension="pdf",
            uploaded_by=admin.id,
        )
        make_document(
            case_id=case_id,
            category=DocumentCategory.EVIDENCE,
            extension="png",
            content=PNG_BYTES,
            uploaded_by=admin.id,
        )

        page = service.list_documents(
            DocumentListQuery(
                category=DocumentCategory.EVIDENCE, uploaded_by=admin.id, file_extension="pdf"
            ),
            actor=admin,
        )

        assert [document.id for document in page.documents] == [wanted.id]

    def test_the_case_filter_narrows_to_one_case(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        first, second = make_case(), make_case()
        wanted = make_document(case_id=first.id)
        make_document(case_id=second.id)

        page = service.list_documents(DocumentListQuery(case_id=first.id), actor=make_user())

        assert [document.id for document in page.documents] == [wanted.id]

    def test_the_upload_date_range_includes_the_whole_end_day(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        afternoon = datetime(2026, 7, 30, 14, 32, tzinfo=UTC)
        document = make_document(case_id=make_case().id, created_at=afternoon)
        make_document(case_id=make_case().id, created_at=afternoon + timedelta(days=5))

        page = service.list_documents(
            DocumentListQuery(uploaded_from=afternoon.date(), uploaded_to=afternoon.date()),
            actor=make_user(),
        )

        # `<= 2026-07-30 00:00` would silently exclude a document uploaded that
        # afternoon.
        assert [entry.id for entry in page.documents] == [document.id]

    def test_deleted_documents_are_excluded_unless_asked_for(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        case_id = make_case().id
        make_document(case_id=case_id)
        make_document(case_id=case_id, deleted_at=datetime.now(UTC))
        admin = make_user()

        assert service.list_documents(DocumentListQuery(), actor=admin).total == 1
        assert (
            service.list_documents(DocumentListQuery(include_deleted=True), actor=admin).total == 2
        )


class TestSortingAndPagination:
    def test_it_sorts_by_file_size_in_both_directions(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        case_id = make_case().id
        small = make_document(case_id=case_id, content=PDF_BYTES)
        large = make_document(case_id=case_id, content=PDF_BYTES + b"\x00" * 500)
        admin = make_user()

        ascending = service.list_documents(
            DocumentListQuery(sort_by=DocumentSortField.FILE_SIZE, sort_order=SortOrder.ASC),
            actor=admin,
        )
        descending = service.list_documents(
            DocumentListQuery(sort_by=DocumentSortField.FILE_SIZE, sort_order=SortOrder.DESC),
            actor=admin,
        )

        assert [entry.id for entry in ascending.documents] == [small.id, large.id]
        assert [entry.id for entry in descending.documents] == [large.id, small.id]

    def test_it_sorts_by_filename_case_insensitively(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        case_id = make_case().id
        apple = make_document(case_id=case_id, original_filename="apple.pdf")
        banana = make_document(case_id=case_id, original_filename="Banana.pdf")

        page = service.list_documents(
            DocumentListQuery(
                sort_by=DocumentSortField.ORIGINAL_FILENAME, sort_order=SortOrder.ASC
            ),
            actor=make_user(),
        )

        # A case-sensitive sort would put every capitalised name first, which is
        # not what a reader of a file list expects.
        assert [entry.id for entry in page.documents] == [apple.id, banana.id]

    def test_it_sorts_by_category_in_the_declared_order(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        case_id = make_case().id
        other = make_document(case_id=case_id, category=DocumentCategory.OTHER)
        contract = make_document(case_id=case_id, category=DocumentCategory.CONTRACT)

        page = service.list_documents(
            DocumentListQuery(sort_by=DocumentSortField.CATEGORY, sort_order=SortOrder.ASC),
            actor=make_user(),
        )

        # Alphabetically "contract" also precedes "other", so the discriminating
        # part is that "other" sorts *last* rather than in the middle — see
        # test_document_utils.TestCategories.
        assert [entry.id for entry in page.documents] == [contract.id, other.id]

    def test_pages_do_not_overlap(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
        make_document: Callable[..., Document],
    ) -> None:
        case_id = make_case().id
        for _ in range(5):
            make_document(case_id=case_id)
        admin = make_user()

        first = service.list_documents(DocumentListQuery(page=1, page_size=2), actor=admin)
        second = service.list_documents(DocumentListQuery(page=2, page_size=2), actor=admin)

        assert first.total == 5
        assert not {entry.id for entry in first.documents} & {
            entry.id for entry in second.documents
        }


class TestVersionHistory:
    def test_it_records_the_uploader_and_date_of_each_version(
        self,
        service: DocumentService,
        make_user: Callable[..., User],
        make_case: Callable[..., Case],
    ) -> None:
        author = make_user(email="author@example.com")
        reviser = make_user(email="reviser@example.com")
        document = service.upload_document(_form(make_case().id), _upload(), actor=author)
        service.replace_document(document.id, _upload(), actor=reviser)

        history = service.list_versions(document.id, actor=author)

        assert [entry.version for entry in history] == [1, 2]
        assert [entry.uploaded_by for entry in history] == [author.id, reviser.id]
        assert all(isinstance(entry.created_at, datetime) for entry in history)
