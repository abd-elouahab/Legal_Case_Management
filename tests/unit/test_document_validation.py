"""Unit tests for :mod:`services.document_validation`.

The five checks the spec lists — missing file, empty file, maximum size, allowed
type, corrupted upload. The validator takes a stream rather than a Starlette
``UploadFile``, which is what lets these run against a ``BytesIO`` with no
request in sight.
"""

from __future__ import annotations

import io

import pytest

from core.config import settings
from core.exceptions import InvalidDocumentFileError
from services.document_validation import validate_upload
from tests.helpers import PDF_BYTES, PNG_BYTES, TXT_BYTES


def _stream(payload: bytes) -> io.BytesIO:
    return io.BytesIO(payload)


class TestAcceptedUploads:
    def test_it_describes_how_to_store_a_valid_file(self) -> None:
        result = validate_upload(filename="Contrat de bail.pdf", stream=_stream(PDF_BYTES))

        assert result.original_filename == "Contrat de bail.pdf"
        assert result.extension == "pdf"
        assert result.mime_type == "application/pdf"
        assert result.size == len(PDF_BYTES)
        # A generated name, never derived from the original.
        assert result.stored_filename != result.original_filename
        assert result.stored_filename.endswith(".pdf")

    def test_the_stream_is_rewound_for_the_caller(self) -> None:
        result = validate_upload(filename="scan.png", stream=_stream(PNG_BYTES))

        # The size measurement and the header read both moved the cursor; the
        # caller must still get the whole body.
        assert result.stream.read() == PNG_BYTES

    def test_it_accepts_plain_text(self) -> None:
        result = validate_upload(filename="notes.txt", stream=_stream(TXT_BYTES))

        assert result.extension == "txt"
        assert result.mime_type.startswith("text/plain")

    def test_the_type_is_taken_from_the_extension_not_the_content(self) -> None:
        # A PNG named .png is served as image/png even though the validator only
        # ever inspects its first bytes — the browser's Content-Type never
        # participates.
        assert validate_upload(filename="x.png", stream=_stream(PNG_BYTES)).mime_type == "image/png"

    def test_a_path_in_the_filename_is_discarded(self) -> None:
        result = validate_upload(filename="../../etc/contract.pdf", stream=_stream(PDF_BYTES))

        assert result.original_filename == "contract.pdf"


class TestRejectedUploads:
    def test_a_missing_file_is_rejected(self) -> None:
        with pytest.raises(InvalidDocumentFileError) as error:
            validate_upload(filename=None, stream=None)

        assert error.value.status_code == 422
        assert error.value.details[0].field == "file"

    def test_an_unnamed_file_is_rejected(self) -> None:
        with pytest.raises(InvalidDocumentFileError):
            validate_upload(filename="   ", stream=_stream(PDF_BYTES))

    def test_an_empty_file_is_rejected(self) -> None:
        with pytest.raises(InvalidDocumentFileError) as error:
            validate_upload(filename="empty.pdf", stream=_stream(b""))

        assert "empty" in str(error.value).lower()

    def test_an_oversized_file_is_rejected(self) -> None:
        oversized = PDF_BYTES + b"\x00" * settings.max_document_size_bytes

        with pytest.raises(InvalidDocumentFileError) as error:
            validate_upload(filename="huge.pdf", stream=_stream(oversized))

        # The message quotes both numbers, so an administrator does not have to
        # convert bytes in their head.
        assert "maximum" in str(error.value)

    def test_an_unsupported_type_is_rejected_and_names_what_is_accepted(self) -> None:
        with pytest.raises(InvalidDocumentFileError) as error:
            validate_upload(filename="script.exe", stream=_stream(b"MZ\x90\x00"))

        assert "pdf" in str(error.value)

    def test_a_file_with_no_extension_is_rejected(self) -> None:
        with pytest.raises(InvalidDocumentFileError):
            validate_upload(filename="README", stream=_stream(PDF_BYTES))

    def test_a_corrupted_file_is_rejected(self) -> None:
        # The "corrupted uploads" rule: a .pdf whose bytes are not a PDF.
        with pytest.raises(InvalidDocumentFileError) as error:
            validate_upload(filename="broken.pdf", stream=_stream(b"not a pdf at all"))

        assert "corrupted" in str(error.value).lower()

    def test_a_renamed_executable_is_rejected(self) -> None:
        with pytest.raises(InvalidDocumentFileError):
            validate_upload(filename="invoice.png", stream=_stream(b"MZ\x90\x00" * 8))

    def test_binary_content_named_txt_is_rejected(self) -> None:
        with pytest.raises(InvalidDocumentFileError):
            validate_upload(filename="notes.txt", stream=_stream(b"\x00\x01\x02\x03"))

    def test_every_rejection_names_the_file_field(self) -> None:
        # So the client renders it beside the file input rather than as a
        # top-level error.
        with pytest.raises(InvalidDocumentFileError) as error:
            validate_upload(filename="broken.pdf", stream=_stream(b"nope"))

        assert [detail.field for detail in error.value.details] == ["file"]
