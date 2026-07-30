"""Document request and response schemas.

Two responsibilities, the same two the case schemas carry:

* **Validation** — the category enum, the description limit, the list query's
  page bounds, filters, and date coherence are all enforced here, so routes stay
  thin and every rejection comes back in the standard envelope with a per-field
  message.
* **Serialization** — a document is returned with its uploader and its case as
  *objects* rather than bare identifiers, because a document list that showed a
  UUID in the "Uploaded By" column would force every client to resolve them
  itself.

What is deliberately **not** here: anything about the file's bytes. Size and type
validation needs the upload stream, so it lives in
:mod:`services.document_validation`; whether a caller may reach the document's
case lives in :mod:`services.document_access`.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from core.documents import (
    MAX_DESCRIPTION_LENGTH,
    MAX_EXTENSION_LENGTH,
    InvalidFilenameError,
    format_file_size,
    is_previewable,
    normalize_description,
    normalize_extension,
)
from models.document import DocumentCategory
from schemas.case import CaseUserSummary, SortOrder

#: Default and maximum page sizes for the document list. The ceiling exists so a
#: single request cannot be used to enumerate every document on the platform.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: A person referenced by a document — currently its uploader.
#:
#: Deliberately the *same* schema the case module uses rather than a second copy:
#: it carries exactly what a document screen displays (name, email, role) and
#: nothing from the directory record that a lawyer without ``users:view`` should
#: not receive. Duplicating it would let the two drift, and the reasoning behind
#: the narrow shape is recorded once, on :class:`~schemas.case.CaseUserSummary`.
DocumentUserSummary = CaseUserSummary

#: Re-exported so the document API's sort direction is literally the same type as
#: the case API's, rather than a second enum that happens to agree today.
__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "DocumentCaseSummary",
    "DocumentListQuery",
    "DocumentPage",
    "DocumentRead",
    "DocumentSortField",
    "DocumentUpdate",
    "DocumentUploadForm",
    "DocumentUserSummary",
    "DocumentVersionRead",
    "SortOrder",
]


def _validate_optional_description(value: str | None) -> str | None:
    """Trim a description, treating a blank string as absent."""
    if value is None:
        return None

    normalized = normalize_description(value)
    if not normalized:
        # An empty field in a form means "not recorded", not "a description of
        # length zero" — the same rule the case and user schemas apply.
        return None
    if len(normalized) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Description must be at most {MAX_DESCRIPTION_LENGTH} characters.")
    return normalized


OptionalDescription = Annotated[
    str | None,
    Field(
        default=None,
        max_length=MAX_DESCRIPTION_LENGTH,
        description="Free-text note describing the document.",
    ),
]


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class DocumentCaseSummary(BaseModel):
    """The case a document is filed under, as shown beside the document.

    Narrow on purpose, for the same reason as :data:`DocumentUserSummary`: the
    documents list is reachable by every role, and embedding the full case record
    would hand its court information and audit trail to a caller who asked only
    for a file listing.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique case identifier.")
    case_number: str = Field(description="Unique, human-facing case reference.")
    title: str = Field(description="Short name identifying the case.")


class DocumentVersionRead(BaseModel):
    """One entry in a document's version history.

    Carries what the spec's version history displays — version number, upload
    date, and uploader — plus the file's own identity, so a client can label the
    download it is about to request.
    """

    model_config = ConfigDict(from_attributes=True)

    version: int = Field(description="Version number, starting at 1.")
    original_filename: str = Field(description="Name the file was uploaded under.")
    file_extension: str = Field(description="Lowercase file extension, without the dot.")
    mime_type: str = Field(description="Media type the file is served as.")
    file_size: int = Field(description="Size of the file in bytes.")
    uploaded_by: uuid.UUID | None = Field(
        default=None, description="Identifier of the uploader, if the account still exists."
    )
    uploader: DocumentUserSummary | None = Field(
        default=None, description="Who uploaded this version, if recorded."
    )
    created_at: datetime = Field(description="When this version was uploaded.")

    @computed_field(  # type: ignore[prop-decorator]
        description="Human-readable file size, e.g. `1.4 MB`.",
    )
    @property
    def file_size_label(self) -> str:
        """The size rendered for display.

        Formatted server-side so every client — and every log line quoting a
        rejection — expresses a size the same way.
        """
        return format_file_size(self.file_size)


class DocumentRead(BaseModel):
    """A document as returned to authorized clients.

    Describes the **current** version: the binary fields mirror the version whose
    number is :attr:`version`, and the whole history is available on
    :attr:`versions`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique document identifier, stable across versions.")
    case_id: uuid.UUID = Field(description="Case this document is filed under.")
    case: DocumentCaseSummary | None = Field(
        default=None, description="The case this document is filed under."
    )

    original_filename: str = Field(description="Name the current file was uploaded under.")
    stored_filename: str = Field(
        description="Generated name the object is stored under. Opaque; never shown to users."
    )
    file_extension: str = Field(description="Lowercase file extension, without the dot.")
    mime_type: str = Field(description="Media type the file is served as.")
    file_size: int = Field(description="Size of the current file in bytes.")

    storage_bucket: str = Field(description="Object-storage bucket holding the file.")
    storage_key: str = Field(description="Object key of the current version.")

    category: DocumentCategory = Field(description="Classification of the document.")
    description: str | None = Field(default=None, description="Free-text note about the document.")

    version: int = Field(description="Current version number.")
    uploaded_by: uuid.UUID | None = Field(
        default=None, description="Identifier of the uploader of the current version, if recorded."
    )
    uploader: DocumentUserSummary | None = Field(
        default=None, description="Who uploaded the current version, if recorded."
    )
    uploaded_at: datetime = Field(
        description=(
            "When the current version was uploaded. Equals `created_at` until the document "
            "is replaced."
        )
    )

    created_at: datetime = Field(description="When the document was first added to the case.")
    updated_at: datetime = Field(description="When the document was last modified.")
    deleted_at: datetime | None = Field(
        default=None, description="When the document was deleted, if it has been."
    )

    versions: list[DocumentVersionRead] = Field(
        default_factory=list, description="Complete version history, oldest first."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Whether the document has been deleted (`deleted_at` is set).",
    )
    @property
    def is_deleted(self) -> bool:
        """Derived rather than stored, so it cannot disagree with ``deleted_at``."""
        return self.deleted_at is not None

    @computed_field(  # type: ignore[prop-decorator]
        description="Number of versions this document has.",
    )
    @property
    def version_count(self) -> int:
        """How many versions exist, so a client can show a history affordance."""
        return len(self.versions)

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Whether a browser can render this file type inline. When false, clients should "
            "offer the download endpoint instead — the preview endpoint answers 415."
        )
    )
    @property
    def is_previewable(self) -> bool:
        """Computed from the extension, so the UI cannot offer a preview the API refuses."""
        return is_previewable(self.file_extension)

    @computed_field(  # type: ignore[prop-decorator]
        description="Human-readable file size, e.g. `1.4 MB`.",
    )
    @property
    def file_size_label(self) -> str:
        """The size rendered for display, formatted once, server-side."""
        return format_file_size(self.file_size)


class DocumentPage(BaseModel):
    """One page of the document list.

    Carries the totals the spec requires so a client can render pagination
    controls without a second count query.
    """

    items: list[DocumentRead] = Field(description="Documents on this page.")
    total_records: int = Field(description="Total documents matching the filters, across all pages.")
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of documents per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(
        cls, items: list[DocumentRead], *, total: int, page: int, page_size: int
    ) -> DocumentPage:
        """Assemble a page, deriving ``total_pages`` from the total and size.

        An empty result still reports one page, so a client never renders
        "page 1 of 0".
        """
        return cls(
            items=items,
            total_records=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)) if page_size else 1,
        )


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class DocumentUploadForm(BaseModel):
    """The non-file part of ``POST /documents/upload``.

    A model rather than loose ``Form(...)`` parameters so the category and
    description rules are declared once and appear in OpenAPI. Note that
    ``extra`` is left at its default: the ``file`` part travels in the *same*
    multipart body, so forbidding extra fields here would reject every upload.
    """

    case_id: uuid.UUID = Field(description="Case the document is filed under.")
    category: DocumentCategory = Field(
        default=DocumentCategory.OTHER,
        description="Classification of the document. Defaults to `other` when omitted.",
    )
    description: OptionalDescription

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, value: str | None) -> str | None:
        return _validate_optional_description(value)


class DocumentUpdate(BaseModel):
    """Body for ``PATCH /documents/{id}``.

    Only the two metadata fields the spec allows. The binary — filename,
    extension, MIME type, size, storage location, version — is absent from this
    schema entirely rather than validated and rejected, so there is no field here
    to forget to guard: with ``extra="forbid"``, sending one is a 422. Changing
    the file is a *replacement*, which has its own endpoint and its own
    permission.
    """

    model_config = ConfigDict(extra="forbid")

    category: DocumentCategory | None = Field(default=None, description="New classification.")
    description: OptionalDescription

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, value: str | None) -> str | None:
        return _validate_optional_description(value)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self

    def provided_fields(self) -> dict[str, object]:
        """The fields the client actually sent.

        ``exclude_unset`` is what separates "leave the description alone" from
        "clear the description": both serialize identically otherwise, and a
        plain ``model_dump`` would silently wipe the field the client omitted.
        """
        return self.model_dump(exclude_unset=True)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class DocumentSortField(StrEnum):
    """Sortable columns of the document list, per the spec."""

    #: "Upload Date" — when the current version was added.
    CREATED_AT = "created_at"
    #: "File Name".
    ORIGINAL_FILENAME = "original_filename"
    FILE_SIZE = "file_size"
    CATEGORY = "category"
    VERSION = "version"


class DocumentListQuery(BaseModel):
    """Validated query parameters for ``GET /documents``.

    Every filter is optional and they combine with AND, which is what the spec's
    "filters should be combinable" requires.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Documents per page (max {MAX_PAGE_SIZE}).",
    )
    search: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Case-insensitive match against the original filename, the description, or a "
            "category name."
        ),
    )
    case_id: uuid.UUID | None = Field(
        default=None, description="Only documents filed under this case."
    )
    category: DocumentCategory | None = Field(
        default=None, description="Only documents in this category."
    )
    uploaded_by: uuid.UUID | None = Field(
        default=None, description="Only documents whose current version this user uploaded."
    )
    file_extension: str | None = Field(
        default=None,
        max_length=MAX_EXTENSION_LENGTH,
        description="Only documents of this file type, e.g. `pdf`. Accepts `.PDF` too.",
    )
    uploaded_from: date | None = Field(
        default=None, description="Only documents uploaded on or after this date."
    )
    uploaded_to: date | None = Field(
        default=None, description="Only documents uploaded on or before this date."
    )
    include_deleted: bool = Field(
        default=False,
        description=(
            "Include soft-deleted documents. Off by default, so a deleted document leaves the "
            "working set without leaving the record."
        ),
    )
    sort_by: DocumentSortField = Field(
        default=DocumentSortField.CREATED_AT, description="Column to sort by."
    )
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="Sort direction.")

    @field_validator("search")
    @classmethod
    def _normalize_search(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("file_extension")
    @classmethod
    def _normalize_file_extension(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            return normalize_extension(value)
        except InvalidFilenameError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        # An inverted range matches nothing, which reads as "the filter is
        # broken" rather than as the input error it is.
        if (
            self.uploaded_from is not None
            and self.uploaded_to is not None
            and self.uploaded_to < self.uploaded_from
        ):
            raise ValueError("`uploaded_to` cannot be before `uploaded_from`.")
        return self

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size
