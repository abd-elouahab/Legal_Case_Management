"""Unit tests for :mod:`core.documents`.

Pure functions, so these need no database, no request, and no MinIO — which is
exactly why the file-type policy, the filename rules, and the storage layout live
there rather than inside a service method.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.engine.interfaces import Compiled

from core.documents import (
    CATEGORY_RANK,
    EXTENSION_MIME_TYPES,
    MAX_ORIGINAL_FILENAME_LENGTH,
    PREVIEWABLE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    InvalidFilenameError,
    allowed_extensions,
    build_storage_key,
    build_stored_filename,
    content_matches_extension,
    format_file_size,
    is_previewable,
    mime_type_for,
    normalize_description,
    normalize_extension,
    sanitize_original_filename,
    sorted_allowed_extensions,
)
from models.document import DocumentCategory


class TestCategories:
    def test_every_category_the_spec_lists_exists(self) -> None:
        assert {category.value for category in DocumentCategory} == {
            "contract",
            "evidence",
            "court_decision",
            "pleading",
            "correspondence",
            "invoice",
            "identity_document",
            "other",
        }

    def test_rank_is_derived_from_declaration_order(self) -> None:
        # Derived, not a second hand-maintained list — adding a category places
        # itself.
        assert list(CATEGORY_RANK) == list(DocumentCategory)

    def test_other_sorts_last(self) -> None:
        # Alphabetical ordering would bury it in the middle, which is the whole
        # reason the rank exists.
        assert CATEGORY_RANK[DocumentCategory.OTHER] == max(CATEGORY_RANK.values())

    def test_rank_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            CATEGORY_RANK[DocumentCategory.OTHER] = 0  # type: ignore[index]


class TestFileTypePolicy:
    def test_every_type_the_spec_requires_is_supported(self) -> None:
        assert {"pdf", "docx", "doc", "txt", "jpg", "jpeg", "png"} <= SUPPORTED_EXTENSIONS

    def test_every_supported_type_has_a_mime_type(self) -> None:
        assert frozenset(EXTENSION_MIME_TYPES) == SUPPORTED_EXTENSIONS

    def test_configuration_cannot_widen_the_policy(self) -> None:
        # The intersection is the point: a stray entry in the environment cannot
        # enable a format the platform has no MIME mapping for.
        assert allowed_extensions() <= SUPPORTED_EXTENSIONS

    def test_sorted_allowed_extensions_is_stable(self) -> None:
        assert sorted_allowed_extensions() == sorted(allowed_extensions())

    @pytest.mark.parametrize(
        ("extension", "expected"),
        [
            ("pdf", "application/pdf"),
            ("png", "image/png"),
            ("jpg", "image/jpeg"),
            ("jpeg", "image/jpeg"),
        ],
    )
    def test_mime_type_comes_from_the_extension(self, extension: str, expected: str) -> None:
        assert mime_type_for(extension) == expected

    def test_unknown_extension_falls_back_to_octet_stream(self) -> None:
        # Which forces a download rather than any attempt at rendering.
        assert mime_type_for("exe") == "application/octet-stream"

    def test_word_documents_are_not_previewable(self) -> None:
        # No browser renders them; the spec's "if preview is unavailable, allow
        # download instead" is exactly this distinction.
        assert not is_previewable("docx")
        assert not is_previewable("doc")

    @pytest.mark.parametrize("extension", sorted(PREVIEWABLE_EXTENSIONS))
    def test_previewable_types_are_supported_types(self, extension: str) -> None:
        assert extension in SUPPORTED_EXTENSIONS


class TestNormalizeExtension:
    @pytest.mark.parametrize(
        "value", ["Report.PDF", "report.pdf", ".pdf", "pdf", "PDF", "archive.tar.pdf"]
    )
    def test_it_reduces_to_a_lowercase_suffix(self, value: str) -> None:
        assert normalize_extension(value) == "pdf"

    @pytest.mark.parametrize("value", ["", ".", "report.", "report.p df", "report.???"])
    def test_it_rejects_anything_unusable(self, value: str) -> None:
        with pytest.raises(InvalidFilenameError):
            normalize_extension(value)


class TestSanitizeOriginalFilename:
    def test_it_preserves_an_ordinary_name(self) -> None:
        assert sanitize_original_filename("Contrat de bail.pdf") == "Contrat de bail.pdf"

    def test_it_preserves_non_latin_names(self) -> None:
        # Arabic is a first-class interface language; mangling the name here
        # would surface as a broken download filename.
        assert sanitize_original_filename("عقد الإيجار.pdf") == "عقد الإيجار.pdf"

    @pytest.mark.parametrize(
        "value",
        [
            "../../etc/passwd.pdf",
            "..\\..\\windows\\system32\\passwd.pdf",
            "/absolute/path/passwd.pdf",
            "C:\\Users\\admin\\passwd.pdf",
        ],
    )
    def test_it_discards_every_directory_component(self, value: str) -> None:
        # The name reaches a Content-Disposition header, so it must never be
        # able to describe a path.
        assert sanitize_original_filename(value) == "passwd.pdf"

    def test_it_replaces_characters_a_header_would_treat_specially(self) -> None:
        assert '"' not in sanitize_original_filename('re"port.pdf')
        assert "\n" not in sanitize_original_filename("re\nport.pdf")

    def test_it_truncates_to_the_column_width_keeping_the_extension(self) -> None:
        result = sanitize_original_filename("a" * 400 + ".pdf")

        assert len(result) <= MAX_ORIGINAL_FILENAME_LENGTH
        assert result.endswith(".pdf")

    @pytest.mark.parametrize("value", ["", "   ", "...", "/", "\\"])
    def test_it_rejects_a_name_with_nothing_left(self, value: str) -> None:
        with pytest.raises(InvalidFilenameError):
            sanitize_original_filename(value)


class TestStorageLayout:
    def test_stored_filename_is_generated_not_derived(self) -> None:
        first = build_stored_filename("pdf")
        second = build_stored_filename("pdf")

        # Two users uploading `contract.pdf` must not contend for one key.
        assert first != second
        assert first.endswith(".pdf")

    def test_storage_key_contains_the_version(self) -> None:
        case_id, document_id = uuid.uuid4(), uuid.uuid4()

        first = build_storage_key(
            case_id=case_id, document_id=document_id, version=1, stored_filename="a.pdf"
        )
        second = build_storage_key(
            case_id=case_id, document_id=document_id, version=2, stored_filename="b.pdf"
        )

        # "Never overwrite previous files" is a property of the layout, not a
        # rule the service has to remember.
        assert first != second
        assert "/v1/" in first
        assert "/v2/" in second

    def test_storage_key_is_browsable_by_case(self) -> None:
        case_id = uuid.uuid4()
        key = build_storage_key(
            case_id=case_id, document_id=uuid.uuid4(), version=1, stored_filename="a.pdf"
        )

        assert key.startswith(f"cases/{case_id}/documents/")


class TestContentMatchesExtension:
    @pytest.mark.parametrize(
        ("extension", "header"),
        [
            ("pdf", b"%PDF-1.7\n"),
            ("png", b"\x89PNG\r\n\x1a\n"),
            ("jpg", b"\xff\xd8\xff\xe0"),
            ("jpeg", b"\xff\xd8\xff\xe1"),
            ("docx", b"PK\x03\x04"),
            ("doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
            ("txt", "Procès-verbal".encode()),
        ],
    )
    def test_it_accepts_a_genuine_file(self, extension: str, header: bytes) -> None:
        assert content_matches_extension(extension, header)

    @pytest.mark.parametrize(
        ("extension", "header"),
        [
            ("pdf", b"MZ\x90\x00"),  # a renamed executable
            ("png", b"%PDF-1.7"),  # a PDF called .png
            ("docx", b"not a zip"),
            ("txt", b"\x00\x01\x02"),  # binary with a .txt name
        ],
    )
    def test_it_rejects_a_mislabelled_file(self, extension: str, header: bytes) -> None:
        assert not content_matches_extension(extension, header)

    def test_an_empty_header_is_never_valid(self) -> None:
        assert not content_matches_extension("pdf", b"")


class TestFormatFileSize:
    @pytest.mark.parametrize(
        ("size", "expected"),
        [(0, "0 B"), (512, "512 B"), (1024, "1.0 KB"), (1536, "1.5 KB"), (1024 * 1024, "1.0 MB")],
    )
    def test_it_renders_a_readable_size(self, size: int, expected: str) -> None:
        assert format_file_size(size) == expected


class TestNormalizeDescription:
    def test_it_trims_without_flattening_paragraphs(self) -> None:
        assert normalize_description("  first\n\nsecond  ") == "first\n\nsecond"


class TestCategorySortSql:
    """The category ORDER BY must be valid PostgreSQL, not just valid SQLite.

    The rest of the suite runs on SQLite, which is untyped enough to accept a
    comparison between an enum column and a plain string. PostgreSQL is not:
    ``case({...}, value=Document.category)`` binds its keys as ``VARCHAR``, and
    there is no ``document_category = character varying`` operator. That exact
    bug shipped in the case repository's priority sort and reached a live
    database before anything noticed, so it is guarded here from the start.
    """

    @staticmethod
    def _compiled() -> Compiled:
        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql

        from models.document import Document
        from repositories.document import DocumentRepository
        from schemas.document import DocumentListQuery, DocumentSortField

        statement = select(Document.id).order_by(
            *DocumentRepository._order_by(DocumentListQuery(sort_by=DocumentSortField.CATEGORY))
        )
        return statement.compile(dialect=postgresql.dialect())

    def test_no_category_value_is_bound_as_a_plain_string(self) -> None:
        bound = {type(bind.type).__name__ for bind in self._compiled().binds.values()}

        assert "String" not in bound
        assert "Enum" in bound

    def test_it_is_a_searched_case_over_the_column(self) -> None:
        sql = str(self._compiled())

        assert "CASE WHEN (documents.category = " in sql
        assert "CASE documents.category WHEN" not in sql
