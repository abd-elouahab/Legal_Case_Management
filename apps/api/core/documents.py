"""Document-domain utilities.

Small, pure helpers shared by the document schemas, repository, service, and
storage layer: the category ordering, the file-type policy, filename
sanitisation, and storage-key construction.

They live here rather than inside a service method for the same reason
:mod:`core.cases` and :mod:`core.users` exist — the same rules must apply however
a document arrives (upload, replacement, and any future import), and they can be
unit-tested without a database, a request, or a running MinIO.

Nothing here reads file *content* beyond the leading bytes needed to tell a
truncated upload from a real one. Text extraction, OCR, and indexing are
explicitly out of scope for this feature.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Mapping
from types import MappingProxyType

from core.config import settings
from models.document import DocumentCategory

#: Longest ``documents.original_filename`` retained, matching the column. Longer
#: names are truncated rather than rejected: the name is a label, and refusing an
#: otherwise valid file because its name is verbose would be hostile.
MAX_ORIGINAL_FILENAME_LENGTH = 255

#: Longest ``documents.description`` accepted. The column is unbounded ``Text``;
#: the ceiling exists so an upload cannot be used to store an arbitrary payload.
MAX_DESCRIPTION_LENGTH = 2_000

#: Longest extension accepted, matching the column. Anything longer is not a
#: file type, it is a filename with a dot in it.
MAX_EXTENSION_LENGTH = 20

#: Bytes of the uploaded file inspected to detect a corrupted or mislabelled
#: upload. Every signature below fits comfortably within this.
FILE_HEADER_BYTES = 512


class InvalidFilenameError(ValueError):
    """Raised when a filename cannot yield a usable, safe name."""


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #

#: Sort weight per category, in the order ``07-document-management.md`` lists
#: them.
#:
#: Sorting on the stored value would order alphabetically — contract,
#: correspondence, court_decision, evidence, identity_document, invoice, other,
#: pleading — which buries "Other" in the middle and separates the two
#: court-facing kinds. Derived from the enum's declaration order, so adding a
#: category places itself and no second list has to be kept in step.
CATEGORY_RANK: Mapping[DocumentCategory, int] = MappingProxyType(
    {category: rank for rank, category in enumerate(DocumentCategory, start=1)}
)


# --------------------------------------------------------------------------- #
# File-type policy
# --------------------------------------------------------------------------- #

#: Extension → the MIME type the platform serves the file as.
#:
#: This mapping is the *only* source of a document's MIME type. The browser's
#: ``Content-Type`` on the upload part is attacker-controlled, and it is the
#: value that decides how the preview endpoint's response is rendered — trusting
#: it would let a caller have an HTML payload served inline from the platform's
#: own origin.
#:
#: The set of supported types is the spec's minimum; it is configurable through
#: ``ALLOWED_DOCUMENT_EXTENSIONS``, which can only ever *narrow* this mapping —
#: a type with no MIME entry here cannot be served, so it cannot be enabled by
#: configuration alone.
EXTENSION_MIME_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "txt": "text/plain; charset=utf-8",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }
)

#: Extensions supported unless configuration narrows the list.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(EXTENSION_MIME_TYPES)

#: Types a browser renders natively, so the preview endpoint can return them
#: inline. Word documents are deliberately absent: no browser displays them, and
#: the spec's "if preview is unavailable, allow download instead" is exactly this
#: distinction.
PREVIEWABLE_EXTENSIONS: frozenset[str] = frozenset({"pdf", "jpg", "jpeg", "png", "txt"})

#: Leading bytes that identify a file format.
#:
#: Used to catch a **corrupted or mislabelled upload** — a truncated PDF, a
#: renamed executable, a zero-length placeholder that a browser still submitted.
#: It is a sanity check on the declared extension, not a security boundary:
#: content is never executed, and the MIME type served comes from the mapping
#: above rather than from what the bytes suggest.
FILE_SIGNATURES: Mapping[str, tuple[bytes, ...]] = MappingProxyType(
    {
        "pdf": (b"%PDF-",),
        "png": (b"\x89PNG\r\n\x1a\n",),
        "jpg": (b"\xff\xd8\xff",),
        "jpeg": (b"\xff\xd8\xff",),
        # DOCX is an OOXML package, i.e. a ZIP archive.
        "docx": (b"PK\x03\x04",),
        # Legacy .doc is an OLE2 compound file.
        "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    }
)

def allowed_extensions() -> frozenset[str]:
    """The file types this deployment accepts.

    The configured list intersected with the types the platform can actually
    serve, so a stray entry in ``ALLOWED_DOCUMENT_EXTENSIONS`` cannot enable a
    format with no MIME mapping — configuration may narrow the policy, never
    widen it.
    """
    return frozenset(settings.ALLOWED_DOCUMENT_EXTENSIONS) & SUPPORTED_EXTENSIONS


def sorted_allowed_extensions() -> list[str]:
    """The accepted file types in a stable order, for messages and OpenAPI."""
    return sorted(allowed_extensions())


_EXTENSION_ALLOWED = re.compile(r"^[a-z0-9]+$")

_WHITESPACE = re.compile(r"\s+")

#: Characters that must never survive into a stored filename: path separators,
#: the Windows-reserved set, and anything a shell or a ``Content-Disposition``
#: header would treat specially.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def normalize_extension(value: str) -> str:
    """Reduce a filename or a bare suffix to a lowercase extension.

    Accepts ``"Report.PDF"``, ``".pdf"``, and ``"pdf"`` alike, so the same
    function serves the upload path and the "file type" query filter.

    Raises:
        InvalidFilenameError: the value carries no usable extension.
    """
    suffix = value.rsplit(".", 1)[-1] if "." in value else value
    extension = suffix.strip().lower()

    if not extension:
        raise InvalidFilenameError("The file has no extension.")
    if len(extension) > MAX_EXTENSION_LENGTH or not _EXTENSION_ALLOWED.match(extension):
        raise InvalidFilenameError("The file extension is not a recognised format.")

    return extension


def sanitize_original_filename(value: str) -> str:
    """Reduce a client-supplied filename to a safe, displayable name.

    The name is *preserved*, as the spec requires — it is what the document is
    called to a human, and it is the name a download is served under. But it
    arrives from the client, so before it is stored:

    * any directory component is discarded (``../../etc/passwd`` becomes
      ``passwd``), because the name reaches a ``Content-Disposition`` header and
      must never be able to describe a path;
    * path separators, control characters, and the characters a filesystem or a
      header would treat specially are replaced;
    * Unicode is normalized to NFC so the same name typed on two platforms is one
      string rather than two;
    * the result is truncated to the column width, keeping the extension.

    Raises:
        InvalidFilenameError: nothing usable remains.
    """
    # Split on both separators: a Windows client sends backslashes, and
    # `PurePosixPath` would keep them as part of the name.
    base = re.split(r"[\\/]", value)[-1]
    base = unicodedata.normalize("NFC", base)
    base = _UNSAFE_FILENAME_CHARS.sub("_", base)
    base = _WHITESPACE.sub(" ", base).strip().strip(".")

    if not base:
        raise InvalidFilenameError("The file name is empty.")

    if len(base) > MAX_ORIGINAL_FILENAME_LENGTH:
        stem, _, suffix = base.rpartition(".")
        if stem and len(suffix) < MAX_ORIGINAL_FILENAME_LENGTH:
            keep = MAX_ORIGINAL_FILENAME_LENGTH - len(suffix) - 1
            base = f"{stem[:keep]}.{suffix}"
        else:
            base = base[:MAX_ORIGINAL_FILENAME_LENGTH]

    return base


def build_stored_filename(extension: str) -> str:
    """Generate the collision-free name an object is stored under.

    A fresh UUID rather than anything derived from the original name: two users
    uploading ``contract.pdf`` to the same case must not contend for one key, and
    the storage layout must not be influenceable by a crafted filename.
    """
    return f"{uuid.uuid4().hex}.{extension}"


def build_storage_key(
    *, case_id: uuid.UUID, document_id: uuid.UUID, version: int, stored_filename: str
) -> str:
    """Build the object key for one version of one document.

    Shaped ``cases/{case}/documents/{document}/v{version}/{stored_filename}`` so
    that the bucket is browsable by case during an incident, and so that a
    version's key is unique by construction — which is what makes "never
    overwrite previous files" a property of the layout rather than a rule the
    service has to remember.
    """
    return f"cases/{case_id}/documents/{document_id}/v{version}/{stored_filename}"


def mime_type_for(extension: str) -> str:
    """The MIME type a document with this extension is served as.

    Falls back to ``application/octet-stream`` for an extension with no entry,
    which forces a download rather than any attempt at rendering.
    """
    return EXTENSION_MIME_TYPES.get(extension, "application/octet-stream")


def is_previewable(extension: str) -> bool:
    """Whether a browser can render this file type inline."""
    return extension in PREVIEWABLE_EXTENSIONS


def content_matches_extension(extension: str, header: bytes) -> bool:
    """Whether the file's leading bytes are consistent with its extension.

    Catches the "corrupted uploads" the spec asks to validate: a truncated
    transfer, a file renamed to get past the type filter, or a placeholder that
    never had content.

    Plain text has no signature, so it is checked negatively — a NUL byte in the
    first block means the file is binary, whatever it is called. Any extension
    with neither a signature nor a rule is accepted; this function narrows the
    allowed set, it is not the thing that defines it.
    """
    if not header:
        return False

    signatures = FILE_SIGNATURES.get(extension)
    if signatures is not None:
        return any(header.startswith(signature) for signature in signatures)

    if extension == "txt":
        return b"\x00" not in header

    return True


def normalize_description(value: str) -> str:
    """Trim a description without flattening its paragraphs.

    Same rule as a case description: the text is prose, and collapsing its line
    breaks would destroy the author's structure.
    """
    return value.strip()


def format_file_size(size: int) -> str:
    """Render a byte count for a log or an error message.

    Used in validation messages ("maximum 25.0 MB"), so an administrator reading
    a rejection does not have to convert bytes in their head.
    """
    if size < 1024:
        return f"{size} B"

    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"

    return f"{value:.1f} GB"  # pragma: no cover - unreachable, the loop returns first
