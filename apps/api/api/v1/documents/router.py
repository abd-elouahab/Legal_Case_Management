"""Document management endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas and the
upload validator, delegate to :class:`~services.document.DocumentService`, and
shape the HTTP response. No business logic lives here.

Authorization is layered, and the two layers answer different questions:

* the ``documents:*`` **capability** is required by the reusable dependencies in
  :mod:`api.authorization`, declared next to the route so it appears in the
  OpenAPI schema. Because ``CurrentUser`` resolves first, an anonymous caller
  gets **401** and only an authenticated-but-unentitled one gets **403**;
* the **resource** — whether this caller is party to the document's *case* — is
  checked by the service, which is the only layer that has the case row.

The two file-serving endpoints stream from object storage rather than buffering,
and set the security headers a response carrying user-supplied bytes needs. See
:func:`_file_response`.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from api.authorization import require_permission
from api.deps import DocumentServiceDep
from core.permissions import Permission
from models.document import DocumentCategory
from models.user import User
from schemas.document import (
    DocumentListQuery,
    DocumentPage,
    DocumentRead,
    DocumentUpdate,
    DocumentUploadForm,
    DocumentVersionRead,
)
from schemas.errors import ErrorResponse
from services.document import DocumentDownload
from services.document_validation import validate_upload

router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
#
# Each dependency yields the *authorized* user, so an endpoint that needs to
# record who acted does not have to depend on CurrentUser as well.
# --------------------------------------------------------------------------- #

DocumentViewer = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_VIEW))]
DocumentUploader = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_UPLOAD))]
DocumentEditor = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_UPDATE))]
DocumentDeleter = Annotated[User, Depends(require_permission(Permission.DOCUMENTS_DELETE))]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": (
            "The account is disabled, lacks the required permission, or is not assigned to the "
            "case this document belongs to."
        ),
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No live document has this identifier, or it has no such version.",
    }
}
_CASE_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No case has this identifier.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "Request validation failed, or the file is missing, empty, too large, of an "
            "unsupported type, or corrupted; `details` names the offending field."
        ),
    }
}
_UNSUPPORTED_PREVIEW: dict[int | str, dict[str, object]] = {
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
        "model": ErrorResponse,
        "description": "This file type cannot be rendered inline; use the download endpoint.",
    }
}
_STORAGE_UNAVAILABLE: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "Object storage is unreachable or refused the operation.",
    }
}

#: A binary response, described for OpenAPI. `response_class` alone would still
#: advertise `application/json`, which is what a generated client would try to
#: parse.
_FILE_RESPONSE: dict[int | str, dict[str, object]] = {
    status.HTTP_200_OK: {
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        "description": "The file, streamed from object storage.",
    }
}

VersionQuery = Annotated[
    int | None,
    Query(
        ge=1,
        description=(
            "Version to serve. Defaults to the current version; earlier versions remain "
            "available for the lifetime of the document."
        ),
    ),
]


def _upload_form(
    *, case_id: uuid.UUID, category: DocumentCategory, description: str | None
) -> DocumentUploadForm:
    """Validate the non-file part of an upload through the schema layer.

    The form fields are declared individually on the endpoint rather than as a
    single ``Annotated[DocumentUploadForm, Form()]`` parameter, because FastAPI
    does not flatten a form model when the same request also carries a separate
    ``File`` part — the model arrives as one missing field called ``payload``
    and every upload is a 422. Declaring the fields and assembling the model here
    keeps the *rules* (description trimming and its length limit) in
    :mod:`schemas.document`, where every other request's rules live, instead of
    duplicating them as endpoint constraints.

    A Pydantic failure is re-raised as FastAPI's own validation error so it
    reaches the client through the standard envelope with the offending field
    named, exactly like a JSON body would.
    """
    try:
        return DocumentUploadForm(case_id=case_id, category=category, description=description)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def _content_disposition(filename: str, *, inline: bool) -> str:
    """Build a ``Content-Disposition`` header that survives a non-ASCII filename.

    Both forms are emitted, per RFC 6266: a quoted ASCII fallback for clients
    that do not understand the extended syntax, and ``filename*`` with
    percent-encoded UTF-8 for those that do. Without the fallback an Arabic or
    French filename is mangled; without the extended form it is lost entirely.

    Quotes and backslashes are stripped from the fallback rather than escaped:
    the filename reaches this point already sanitised (see
    :func:`~core.documents.sanitize_original_filename`), and a header value is
    the last place to be clever about quoting.
    """
    disposition = "inline" if inline else "attachment"
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "").replace(
        "\\", ""
    )
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


def _file_response(download: DocumentDownload, *, inline: bool) -> StreamingResponse:
    """Stream a stored file back to the caller.

    Streamed rather than buffered so a 25 MB document never sits in the API
    process's memory, and the body is released as it is consumed (see
    :class:`~services.document_storage.ObjectStream`).

    Three security headers, all necessary because the bytes are user-supplied:

    * ``X-Content-Type-Options: nosniff`` stops a browser from second-guessing
      the declared type — content sniffing is how an "image" gets executed as
      HTML.
    * ``Content-Security-Policy: default-src 'none'; sandbox`` neutralises an
      inline preview: even if something scriptable were served, it can reach
      nothing and runs in an opaque origin.
    * ``Cache-Control: private, no-store`` keeps a legal document out of shared
      caches and off disk.
    """
    return StreamingResponse(
        download.stream,
        media_type=download.mime_type,
        headers={
            "Content-Disposition": _content_disposition(download.filename, inline=inline),
            "Content-Length": str(download.size),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "",
    response_model=DocumentPage,
    status_code=status.HTTP_200_OK,
    summary="List documents",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def list_documents(
    actor: DocumentViewer,
    documents: DocumentServiceDep,
    query: Annotated[DocumentListQuery, Query()],
) -> DocumentPage:
    """Return a page of documents the caller is entitled to see.

    Supports **search** (case-insensitive, across the original filename, the
    description, and the category name), **filtering** by case, category,
    uploader, file type, and upload-date range, **sorting** by upload date, file
    name, file size, category, or version in either direction, and
    **pagination**. Filters combine, so
    `?case_id=…&category=evidence&file_extension=pdf` is a valid narrowing.

    Callers holding `cases:view-all` see every document. Everyone else sees only
    documents on the cases they are assigned to — the scope is applied in the
    query, so `total_records` counts only what the caller may access.

    Deleted documents are excluded unless `include_deleted=true` is passed.

    The response carries `total_records`, `page`, `page_size`, and `total_pages`,
    so pagination controls can be rendered without a second request.
    """
    result = documents.list_documents(query, actor=actor)
    return DocumentPage.build(
        [DocumentRead.model_validate(document) for document in result.documents],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/upload",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_CASE_NOT_FOUND,
        **_VALIDATION,
        **_STORAGE_UNAVAILABLE,
    },
)
def upload_document(
    actor: DocumentUploader,
    documents: DocumentServiceDep,
    file: Annotated[UploadFile, File(description="The file to store.")],
    case_id: Annotated[uuid.UUID, Form(description="Case the document is filed under.")],
    category: Annotated[
        DocumentCategory, Form(description="Classification of the document.")
    ] = DocumentCategory.OTHER,
    description: Annotated[
        str | None, Form(description="Free-text note describing the document.")
    ] = None,
) -> DocumentRead:
    """Attach a file to a case as version 1 of a new document.

    A `multipart/form-data` request carrying the file plus `case_id`, an optional
    `category` (defaults to `other`), and an optional `description`.

    The file is validated before anything is stored: it must be present,
    non-empty, no larger than the configured maximum, of an accepted type, and
    its leading bytes must match its extension — a truncated or renamed file is
    refused with **422** naming the `file` field.

    The binary is written to MinIO under a generated, collision-free key; the
    original filename is preserved as metadata and is what a download is served
    under. Only the caller's own identity is recorded as the uploader; it cannot
    be set by the request.
    """
    payload = _upload_form(case_id=case_id, category=category, description=description)
    upload = validate_upload(filename=file.filename, stream=file.file)
    return DocumentRead.model_validate(
        documents.upload_document(payload, upload, actor=actor)
    )


@router.get(
    "/{document_id}",
    response_model=DocumentRead,
    status_code=status.HTTP_200_OK,
    summary="Get a document",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_document(
    actor: DocumentViewer, documents: DocumentServiceDep, document_id: uuid.UUID
) -> DocumentRead:
    """Return the complete metadata record for one document.

    Describes the **current** version — filename, type, size, and storage
    location — together with the category, the description, the uploader, and the
    full version history on `versions`.

    `is_previewable` says whether the preview endpoint will serve this file
    inline, so a client never offers a preview the API is about to refuse.

    A caller without `cases:view-all` must be assigned to the document's case;
    otherwise the request is refused with **403**. A deleted document answers
    **404**.
    """
    return DocumentRead.model_validate(documents.get_document(document_id, actor=actor))


@router.get(
    "/{document_id}/versions",
    response_model=list[DocumentVersionRead],
    status_code=status.HTTP_200_OK,
    summary="List a document's versions",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def list_document_versions(
    actor: DocumentViewer, documents: DocumentServiceDep, document_id: uuid.UUID
) -> list[DocumentVersionRead]:
    """Return the document's complete version history, oldest first.

    Each entry carries the version number, the upload date, and who uploaded it —
    the three things the spec's version history displays — plus the file's name,
    type, and size. Every version listed here remains downloadable through
    `GET /documents/{document_id}/download?version=N`; a replacement never
    overwrites its predecessor.
    """
    return [
        DocumentVersionRead.model_validate(version)
        for version in documents.list_versions(document_id, actor=actor)
    ]


@router.get(
    "/{document_id}/download",
    summary="Download a document",
    response_class=StreamingResponse,
    responses={
        **_FILE_RESPONSE,
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_STORAGE_UNAVAILABLE,
    },
)
def download_document(
    actor: DocumentViewer,
    documents: DocumentServiceDep,
    document_id: uuid.UUID,
    version: VersionQuery = None,
) -> StreamingResponse:
    """Return the original file as an attachment.

    Serves the current version by default; pass `?version=N` to download an
    earlier one, which stays available for the lifetime of the document.

    The response is streamed from object storage, carries the document's original
    filename in `Content-Disposition` (with a UTF-8 form, so Arabic and French
    names survive), and is marked `no-store` so a legal document is not written
    to a shared cache.
    """
    return _file_response(
        documents.open_download(document_id, actor=actor, version=version), inline=False
    )


@router.get(
    "/{document_id}/preview",
    summary="Preview a document in the browser",
    response_class=StreamingResponse,
    responses={
        **_FILE_RESPONSE,
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_UNSUPPORTED_PREVIEW,
        **_STORAGE_UNAVAILABLE,
    },
)
def preview_document(
    actor: DocumentViewer,
    documents: DocumentServiceDep,
    document_id: uuid.UUID,
    version: VersionQuery = None,
) -> StreamingResponse:
    """Return the file for inline display.

    Identical to the download endpoint except for `Content-Disposition: inline`,
    so a browser renders the file rather than saving it. Supported for PDF, PNG,
    JPEG, and plain text.

    A file type no browser can render — a Word document — answers **415**; the
    document's `is_previewable` flag says so in advance, and the client should
    offer the download endpoint instead.
    """
    return _file_response(
        documents.open_preview(document_id, actor=actor, version=version), inline=True
    )


@router.patch(
    "/{document_id}",
    response_model=DocumentRead,
    status_code=status.HTTP_200_OK,
    summary="Update document metadata",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_VALIDATION},
)
def update_document(
    actor: DocumentEditor,
    documents: DocumentServiceDep,
    document_id: uuid.UUID,
    payload: DocumentUpdate,
) -> DocumentRead:
    """Change a document's category or description.

    Only the fields present in the body are written, so an omitted field is left
    untouched while an explicit `null` clears the description.

    **The binary content is never modified by this endpoint.** No field of this
    schema can describe a file, and sending one — `original_filename`,
    `storage_key`, `version` — is a **422**. Changing the file is a *replacement*,
    which has its own endpoint.
    """
    return DocumentRead.model_validate(
        documents.update_document(document_id, payload, actor=actor)
    )


@router.post(
    "/{document_id}/replace",
    response_model=DocumentRead,
    status_code=status.HTTP_200_OK,
    summary="Replace a document with a new version",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_VALIDATION,
        **_STORAGE_UNAVAILABLE,
    },
)
def replace_document(
    actor: DocumentEditor,
    documents: DocumentServiceDep,
    document_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="The replacement file.")],
) -> DocumentRead:
    """Upload a new version of an existing document.

    The version number is incremented and the **previous file is preserved**: it
    keeps its own storage key and stays downloadable through
    `GET /documents/{document_id}/download?version=N`. Nothing is ever
    overwritten.

    The document's identifier does not change, so existing links keep working,
    and its category and description are carried over — they describe the
    document, not the particular file. The replacement is validated exactly as an
    upload is, and may be of a different supported type from the file it
    replaces.
    """
    upload = validate_upload(filename=file.filename, stream=file.file)
    return DocumentRead.model_validate(
        documents.replace_document(document_id, upload, actor=actor)
    )


@router.delete(
    "/{document_id}",
    response_model=DocumentRead,
    status_code=status.HTTP_200_OK,
    summary="Delete a document (soft delete)",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def delete_document(
    actor: DocumentDeleter, documents: DocumentServiceDep, document_id: uuid.UUID
) -> DocumentRead:
    """Withdraw a document from its case.

    A **soft delete**: `deleted_at` is set, and neither the metadata row nor any
    stored file is removed — permanently destroying a legal document is
    forbidden, and a future cleanup job is what eventually reclaims the storage.

    The document disappears from the list (unless `include_deleted=true`) and can
    no longer be read, previewed, or downloaded. Returns the updated document, so
    a client can refresh its view without a second request. Idempotent —
    deleting an already-deleted document succeeds and leaves the original
    deletion time intact.
    """
    return DocumentRead.model_validate(documents.delete_document(document_id, actor=actor))
