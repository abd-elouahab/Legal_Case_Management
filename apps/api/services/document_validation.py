"""Upload validation for documents.

The five checks ``07-document-management.md`` lists — missing file, empty file,
maximum size, allowed type, corrupted upload — in one place, so an upload and a
replacement can never disagree about what is acceptable.

Deliberately framework-independent: it takes a filename and a binary stream, not
a Starlette ``UploadFile``. That keeps the rule set testable with a
``BytesIO`` and means a future importer or background job validates its input
through exactly the same code path rather than a second, drifting copy.

Every rejection raises :class:`~core.exceptions.InvalidDocumentFileError` with a
per-field detail naming ``file``, so it reaches the client in the standard error
envelope beside any other field error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, NoReturn

import structlog

from core.config import settings
from core.documents import (
    FILE_HEADER_BYTES,
    InvalidFilenameError,
    build_stored_filename,
    content_matches_extension,
    format_file_size,
    mime_type_for,
    normalize_extension,
    sanitize_original_filename,
    sorted_allowed_extensions,
)
from core.exceptions import InvalidDocumentFileError
from schemas.errors import ErrorDetail

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """An accepted upload, described well enough to store it.

    ``stream`` is rewound to the start, so the caller can hand it straight to
    object storage.
    """

    original_filename: str
    stored_filename: str
    extension: str
    mime_type: str
    size: int
    stream: BinaryIO


def validate_upload(*, filename: str | None, stream: BinaryIO | None) -> ValidatedUpload:
    """Check an uploaded file and describe how it should be stored.

    Raises:
        InvalidDocumentFileError: the file is missing, unnamed, of a type this
            deployment does not accept, empty, larger than the configured
            maximum, or does not begin the way its extension says it should.
    """
    if stream is None or not filename or not filename.strip():
        _reject("missing_file", "No file was provided.")

    original_filename = _safe_filename(filename)
    extension = _accepted_extension(original_filename)
    size = _measure(stream)
    _check_header(stream, extension=extension)

    # Rewind so the caller reads the whole body, not the tail left behind by the
    # size measurement and the header read.
    stream.seek(0)

    return ValidatedUpload(
        original_filename=original_filename,
        stored_filename=build_stored_filename(extension),
        extension=extension,
        mime_type=mime_type_for(extension),
        size=size,
        stream=stream,
    )


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #


def _safe_filename(filename: str) -> str:
    """Sanitize the client-supplied name, rejecting one that leaves nothing."""
    try:
        return sanitize_original_filename(filename)
    except InvalidFilenameError as exc:
        _reject("invalid_filename", str(exc))


def _accepted_extension(filename: str) -> str:
    """Resolve the file type and check it against this deployment's policy."""
    try:
        extension = normalize_extension(filename)
    except InvalidFilenameError as exc:
        _reject("invalid_extension", str(exc))

    allowed = sorted_allowed_extensions()
    if extension not in allowed:
        _reject(
            "unsupported_type",
            f"Files of type {extension!r} are not accepted. Supported types: "
            f"{', '.join(allowed)}.",
            extension=extension,
        )

    return extension


def _measure(stream: BinaryIO) -> int:
    """Measure the upload, rejecting an empty or oversized one.

    Seek-and-tell rather than counting a read: by the time a handler runs,
    Starlette has already parsed the multipart body into a spooled temporary
    file, so the length is known without moving the bytes again.

    Note that the ceiling is enforced *after* the body has been received. The
    outer guard belongs at the edge — ``client_max_body_size`` in Nginx — so an
    oversized upload is refused before it is transferred; this check is what
    makes the limit true regardless of how the API is deployed.
    """
    stream.seek(0, 2)  # SEEK_END
    size = stream.tell()
    stream.seek(0)

    if size <= 0:
        _reject("empty_file", "The uploaded file is empty.")

    maximum = settings.max_document_size_bytes
    if size > maximum:
        _reject(
            "file_too_large",
            f"The file is {format_file_size(size)}; the maximum is {format_file_size(maximum)}.",
            size=size,
        )

    return size


def _check_header(stream: BinaryIO, *, extension: str) -> None:
    """Reject a file whose leading bytes contradict its extension.

    This is the "corrupted uploads" check: a transfer that stopped part-way, a
    zero-padded placeholder, or a file renamed to slip past the type filter. It
    is a consistency check, not a security control — content is never executed,
    and the MIME type served comes from the extension mapping rather than from
    what the bytes suggest.
    """
    header = stream.read(FILE_HEADER_BYTES)
    stream.seek(0)

    if not content_matches_extension(extension, header):
        _reject(
            "corrupted_file",
            f"The file does not appear to be a valid {extension.upper()} file. "
            "It may be corrupted or incorrectly named.",
            extension=extension,
        )


def _reject(reason: str, message: str, **context: object) -> NoReturn:
    """Log the rejection and fail with a 422 naming the ``file`` field.

    The filename is **not** logged: it can identify a client or a matter, and
    `07-document-management.md` forbids logging sensitive document information.
    The reason and the numbers are enough to diagnose a failing upload.
    """
    logger.info("document_upload_rejected", reason=reason, **context)
    raise InvalidDocumentFileError(
        message, details=[ErrorDetail(field="file", message=message)]
    )
