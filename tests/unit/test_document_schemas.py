"""Unit tests for :mod:`schemas.document`.

Validation and serialization only — the rules that need the database (does this
case exist, may this caller reach it) live in the service and are tested there.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from core.documents import MAX_DESCRIPTION_LENGTH
from models.document import DocumentCategory
from schemas.case import SortOrder
from schemas.document import (
    MAX_PAGE_SIZE,
    DocumentListQuery,
    DocumentPage,
    DocumentRead,
    DocumentSortField,
    DocumentUpdate,
    DocumentUploadForm,
)


def _document_payload(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "id": uuid.uuid4(),
        "case_id": uuid.uuid4(),
        "original_filename": "contract.pdf",
        "stored_filename": "abc123.pdf",
        "file_extension": "pdf",
        "mime_type": "application/pdf",
        "file_size": 2048,
        "storage_bucket": "legal-documents",
        "storage_key": "cases/x/documents/y/v1/abc123.pdf",
        "category": DocumentCategory.CONTRACT,
        "description": None,
        "version": 1,
        "uploaded_by": None,
        "uploaded_at": now,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
        "versions": [],
    }
    payload.update(overrides)
    return payload


class TestDocumentUploadForm:
    def test_category_defaults_to_other(self) -> None:
        form = DocumentUploadForm(case_id=uuid.uuid4())

        assert form.category is DocumentCategory.OTHER

    def test_a_blank_description_is_stored_as_absent(self) -> None:
        # An empty field in a form means "not recorded", not "a description of
        # length zero".
        form = DocumentUploadForm(case_id=uuid.uuid4(), description="   ")

        assert form.description is None

    def test_a_description_is_trimmed_without_flattening_paragraphs(self) -> None:
        form = DocumentUploadForm(case_id=uuid.uuid4(), description="  first\n\nsecond  ")

        assert form.description == "first\n\nsecond"

    def test_an_overlong_description_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentUploadForm(case_id=uuid.uuid4(), description="x" * (MAX_DESCRIPTION_LENGTH + 1))

    def test_an_unknown_category_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentUploadForm(case_id=uuid.uuid4(), category="secret")  # type: ignore[arg-type]

    def test_extra_fields_are_tolerated(self) -> None:
        # Deliberately: the `file` part travels in the same multipart body, so
        # forbidding extras here would reject every upload.
        form = DocumentUploadForm.model_validate({"case_id": uuid.uuid4(), "file": "…"})

        assert form.category is DocumentCategory.OTHER


class TestDocumentUpdate:
    def test_it_distinguishes_omitted_from_null(self) -> None:
        cleared = DocumentUpdate(description=None)
        untouched = DocumentUpdate(category=DocumentCategory.EVIDENCE)

        assert cleared.provided_fields() == {"description": None}
        assert "description" not in untouched.provided_fields()

    def test_an_empty_body_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentUpdate()

    @pytest.mark.parametrize(
        "field",
        [
            "original_filename",
            "stored_filename",
            "file_extension",
            "mime_type",
            "file_size",
            "storage_bucket",
            "storage_key",
            "version",
            "case_id",
            "uploaded_by",
            "deleted_at",
        ],
    )
    def test_no_binary_or_immutable_field_can_be_sent(self, field: str) -> None:
        # The spec says metadata only: "Do not modify binary content." The fields
        # are absent from the schema rather than validated and rejected, so there
        # is nothing here to forget to guard.
        with pytest.raises(ValidationError):
            DocumentUpdate.model_validate({field: "anything"})


class TestDocumentListQuery:
    def test_defaults_are_the_documented_ones(self) -> None:
        query = DocumentListQuery()

        assert query.page == 1
        assert query.sort_by is DocumentSortField.CREATED_AT
        assert query.sort_order is SortOrder.DESC
        assert query.include_deleted is False

    def test_page_size_is_capped(self) -> None:
        with pytest.raises(ValidationError):
            DocumentListQuery(page_size=MAX_PAGE_SIZE + 1)

    def test_offset_follows_from_the_page(self) -> None:
        assert DocumentListQuery(page=3, page_size=20).offset == 40

    @pytest.mark.parametrize("value", ["PDF", ".pdf", "pdf", "report.PDF"])
    def test_the_file_type_filter_is_normalized(self, value: str) -> None:
        assert DocumentListQuery(file_extension=value).file_extension == "pdf"

    def test_a_blank_search_term_is_dropped(self) -> None:
        assert DocumentListQuery(search="   ").search is None

    def test_an_inverted_date_range_is_rejected(self) -> None:
        # It would match nothing, which reads as a broken filter rather than as
        # the input error it is.
        today = datetime.now(UTC).date()
        with pytest.raises(ValidationError):
            DocumentListQuery(uploaded_from=today, uploaded_to=today - timedelta(days=1))

    def test_unknown_query_parameters_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentListQuery.model_validate({"sort_direction": "asc"})


class TestDocumentRead:
    def test_previewability_is_computed_from_the_extension(self) -> None:
        # So the UI never offers a preview the API is about to refuse with 415.
        assert DocumentRead.model_validate(_document_payload()).is_previewable
        assert not DocumentRead.model_validate(
            _document_payload(file_extension="docx")
        ).is_previewable

    def test_deletion_is_derived_from_the_timestamp(self) -> None:
        assert not DocumentRead.model_validate(_document_payload()).is_deleted
        assert DocumentRead.model_validate(
            _document_payload(deleted_at=datetime.now(UTC))
        ).is_deleted

    def test_the_size_is_rendered_once_server_side(self) -> None:
        assert DocumentRead.model_validate(_document_payload(file_size=1536)).file_size_label == (
            "1.5 KB"
        )

    def test_version_count_follows_the_history(self) -> None:
        assert DocumentRead.model_validate(_document_payload()).version_count == 0


class TestDocumentPage:
    def test_totals_are_derived_from_the_count_and_size(self) -> None:
        page = DocumentPage.build([], total=45, page=2, page_size=20)

        assert page.total_pages == 3

    def test_an_empty_result_still_reports_one_page(self) -> None:
        # So a client never renders "page 1 of 0".
        assert DocumentPage.build([], total=0, page=1, page_size=20).total_pages == 1
