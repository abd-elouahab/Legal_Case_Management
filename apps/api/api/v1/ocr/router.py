"""OCR processing endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.ocr.OcrService`, and shape the HTTP response. No business
logic lives here — and, in particular, **no route ever runs an extraction**. A
retry queues work and returns; the reading endpoints report what a background
worker recorded.

Authorization is layered, and the two layers answer different questions:

* the ``ocr:*`` **capability** is required by the reusable dependencies in
  :mod:`api.authorization`, declared next to the route so it appears in the
  OpenAPI schema. Because ``CurrentUser`` resolves first, an anonymous caller
  gets **401** and only an authenticated-but-unentitled one gets **403**;
* the **resource** — whether this caller is party to the document's *case* — is
  checked by the service, which is the only layer that has the case row.

Two routers, because the paths live under different prefixes: the per-document
endpoints belong under ``/documents``, while the list and the monitoring view
belong under ``/ocr``. Keeping both here rather than adding the first to the
document router keeps every OCR endpoint in the OCR module, exactly as the
timeline module does with ``GET /cases/{case_id}/timeline``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from api.authorization import require_permission
from api.deps import OcrServiceDep
from core.permissions import Permission
from models.user import User
from schemas.errors import ErrorResponse
from schemas.ocr import (
    OcrListQuery,
    OcrMetricsQuery,
    OcrMetricsRead,
    OcrPageRead,
    OcrResultPage,
    OcrResultRead,
    OcrTextRead,
)

#: Mounted under ``/documents`` — an extraction is always *of* a document.
document_ocr_router = APIRouter()

#: Mounted under ``/ocr`` — the cross-document list and the monitoring view.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
#
# Each dependency yields the *authorized* user, so an endpoint that needs to
# record who acted does not have to depend on CurrentUser as well.
# --------------------------------------------------------------------------- #

OcrViewer = Annotated[User, Depends(require_permission(Permission.OCR_VIEW))]
OcrRetrier = Annotated[User, Depends(require_permission(Permission.OCR_RETRY))]
OcrMonitor = Annotated[User, Depends(require_permission(Permission.OCR_MONITOR))]

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
            "case the document belongs to."
        ),
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "No live document has this identifier, it has no such version, or no extraction "
            "record exists for it."
        ),
    }
}
_CONFLICT: dict[int | str, dict[str, object]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "Extraction is already queued or running for this document version.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "Request validation failed, or text extraction does not apply to this file type."
        ),
    }
}
_DISABLED: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "Text extraction is disabled on this deployment.",
    }
}

VersionQuery = Annotated[
    int | None,
    Query(
        ge=1,
        description=(
            "Document version to address. Defaults to the current version; each version keeps "
            "its own extraction, so earlier ones stay readable after a replacement."
        ),
    ),
]


# --------------------------------------------------------------------------- #
# Per-document endpoints
# --------------------------------------------------------------------------- #


@document_ocr_router.get(
    "/{document_id}/ocr",
    response_model=OcrResultRead,
    status_code=status.HTTP_200_OK,
    summary="Get a document's text-extraction status",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_document_ocr(
    actor: OcrViewer,
    ocr: OcrServiceDep,
    document_id: uuid.UUID,
    version: VersionQuery = None,
) -> OcrResultRead:
    """Return the OCR run for a document, current version by default.

    Carries everything the extraction recorded — status, start and finish times,
    duration, engine, detected language, page count, and confidence — and
    **deliberately carries no text**: a client polling for completion should not
    pull a hundred pages of prose on every tick. The text is one request further
    on, at `GET /documents/{document_id}/ocr/text`.

    `is_active` says whether to keep polling and `can_retry` whether to offer a
    Retry, both computed from the same rules the API enforces — so a client never
    offers an action the server is about to refuse.

    A document of a type extraction does not apply to — a Word file, a
    plain-text note — has no run and answers **404** saying so.
    """
    return OcrResultRead.model_validate(
        ocr.get_result(document_id, actor=actor, version=version)
    )


@document_ocr_router.get(
    "/{document_id}/ocr/text",
    response_model=OcrTextRead,
    status_code=status.HTTP_200_OK,
    summary="Get a document's extracted text",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_document_ocr_text(
    actor: OcrViewer,
    ocr: OcrServiceDep,
    document_id: uuid.UUID,
    version: VersionQuery = None,
) -> OcrTextRead:
    """Return the text extracted from a document, page by page.

    **Page order and page boundaries are preserved**: `pages` is one entry per
    page of the document in reading order, and `full_text` is those pages joined
    with `page_separator` (U+000C FORM FEED), so splitting the joined form
    recovers exactly the array. Unicode is NFC-normalised, so Arabic and French
    text compares equal however it was recognised.

    **The extracted text inherits the document's permissions**: a caller who
    cannot read the document cannot read its text, and the check is the same one,
    not a second copy of it.

    A run that has not finished returns its status with an empty `pages` array
    rather than an error — a client rendering a document that is still being
    processed needs the state more than it needs a failure.
    """
    result = ocr.get_text(document_id, actor=actor, version=version)
    return OcrTextRead(
        ocr_result_id=result.id,
        document_id=result.document_id,
        document_version=result.document_version,
        status=result.status,
        detected_language=result.detected_language,
        pages=[OcrPageRead.model_validate(page) for page in result.pages],
    )


@document_ocr_router.get(
    "/{document_id}/ocr/history",
    response_model=list[OcrResultRead],
    status_code=status.HTTP_200_OK,
    summary="List a document's text-extraction runs",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def list_document_ocr_history(
    actor: OcrViewer, ocr: OcrServiceDep, document_id: uuid.UUID
) -> list[OcrResultRead]:
    """Return every extraction recorded for a document, oldest version first.

    A document accumulates one run per version: replacing it is new bytes that
    need their own reading, while the previous version keeps the text that was
    read from *it*. This is that history, and it is what makes "what did version
    1 of this contract say?" answerable after a replacement.
    """
    return [
        OcrResultRead.model_validate(result)
        for result in ocr.list_document_results(document_id, actor=actor)
    ]


@document_ocr_router.post(
    "/{document_id}/ocr/retry",
    response_model=OcrResultRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry text extraction for a document",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_VALIDATION,
        **_DISABLED,
    },
)
def retry_document_ocr(
    actor: OcrRetrier,
    ocr: OcrServiceDep,
    document_id: uuid.UUID,
    version: VersionQuery = None,
) -> OcrResultRead:
    """Queue a fresh extraction for a document already uploaded.

    **No file is uploaded and nothing stored is touched.** The document, its
    metadata, and every stored version are left exactly as they are; only the
    extraction record is reset and re-queued.

    **202, not 200**: the work has been accepted, not done. The response is the
    run in its `pending` state, and a client polls
    `GET /documents/{document_id}/ocr` for the outcome.

    **Idempotent per document version.** The existing run is re-used rather than
    replaced — same identifier, timing and failure fields cleared,
    `attempt_count` incremented — so retrying can never produce a second,
    contradictory record of the same bytes. A run already queued or extracting
    answers **409** rather than silently queueing a duplicate.

    A version that has never been processed — uploaded while extraction was
    disabled, or before the feature existed — is queued for the first time here
    rather than refused.
    """
    return OcrResultRead.model_validate(ocr.retry(document_id, actor=actor, version=version))


# --------------------------------------------------------------------------- #
# Cross-document endpoints
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=OcrResultPage,
    status_code=status.HTTP_200_OK,
    summary="List text-extraction runs",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def list_ocr_results(
    actor: OcrViewer,
    ocr: OcrServiceDep,
    query: Annotated[OcrListQuery, Query()],
) -> OcrResultPage:
    """Return a page of extraction runs the caller is entitled to see.

    Supports **filtering** by status, document, case, and failure cause,
    **sorting** by queue time, start, finish, duration, or status in either
    direction, and **pagination**. Filters combine, so `?status=failed&case_id=…`
    is a valid narrowing — which is how an administrator finds every document on
    a case whose text could not be read.

    Callers holding `cases:view-all` see every run. Everyone else sees only runs
    for documents on the cases they are assigned to — the scope is applied in the
    query, so `total_records` counts only what the caller may access.

    Carries no extracted text, for the same reason the status endpoint does not.
    """
    result = ocr.list_results(query, actor=actor)
    return OcrResultPage.build(
        [OcrResultRead.model_validate(item) for item in result.results],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get(
    "/metrics",
    response_model=OcrMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Text-extraction metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def get_ocr_metrics(
    actor: OcrMonitor,
    ocr: OcrServiceDep,
    query: Annotated[OcrMetricsQuery, Query()],
) -> OcrMetricsRead:
    """Return platform-wide extraction health.

    **Success rate, failure rate, and average processing time**, plus the counts
    behind them and a breakdown of failures by cause. Rates are computed over
    *finished* runs only: counting queued work would make the success rate dip on
    every upload and recover as it processed, which measures traffic rather than
    quality.

    Also reports whether the configured engine is actually installed and
    reachable — the difference between "documents are unreadable" and "the
    platform cannot read anything", which the rates alone cannot tell apart.

    An operational view of the pipeline, so it is gated on `ocr:monitor` and
    reports **counts and timings only** — never a document, a case, or a
    character of extracted text. Pass `?window_days=N` to restrict it to recent
    runs.
    """
    metrics = ocr.metrics(window_days=query.window_days)
    return OcrMetricsRead(
        window_days=metrics.window_days,
        total_runs=metrics.counts.total,
        pending=metrics.counts.pending,
        processing=metrics.counts.processing,
        completed=metrics.counts.completed,
        failed=metrics.counts.failed,
        success_rate=metrics.success_rate,
        failure_rate=metrics.failure_rate,
        average_duration_ms=metrics.counts.average_duration_ms,
        failures_by_code=metrics.failures_by_code,
        engine=metrics.engine,
        engine_available=metrics.engine_available,
        enabled=metrics.enabled,
    )
