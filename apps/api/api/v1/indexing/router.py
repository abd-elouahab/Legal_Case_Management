"""Document indexing endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.indexing.IndexingService`, and shape the HTTP response. No
business logic lives here — and, in particular, **no route ever builds an
index**. A re-index queues work and returns; the reading endpoints report what a
background worker recorded.

**There is no search endpoint here, and there will not be one.**
``10-document-indexing.md`` puts Semantic Search out of scope and requires
indexing to stay independent from retrieval; this router therefore exposes
status, history, re-index, a list, and metrics, and nothing that reads a vector
back. Search will be its own module against the same collection.

Authorization is layered, and the two layers answer different questions:

* the ``indexing:*`` **capability** is required by the reusable dependencies in
  :mod:`api.authorization`, declared next to the route so it appears in the
  OpenAPI schema. Because ``CurrentUser`` resolves first, an anonymous caller
  gets **401** and only an authenticated-but-unentitled one gets **403**;
* the **resource** — whether this caller is party to the document's *case* — is
  checked by the service, which is the only layer that has the case row.

Two routers, because the paths live under different prefixes: the per-document
endpoints belong under ``/documents``, while the list and the monitoring view
belong under ``/indexing``. Keeping both here rather than adding the first to the
document router keeps every indexing endpoint in the indexing module, exactly as
the OCR and timeline modules do.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from api.authorization import require_permission
from api.deps import IndexingServiceDep
from core.permissions import Permission
from models.user import User
from schemas.errors import ErrorResponse
from schemas.indexing import (
    IndexListQuery,
    IndexMetricsQuery,
    IndexMetricsRead,
    IndexRead,
    IndexResultPage,
)

#: Mounted under ``/documents`` — an index is always *of* a document.
document_indexing_router = APIRouter()

#: Mounted under ``/indexing`` — the cross-document list and the monitoring view.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
#
# Each dependency yields the *authorized* user, so an endpoint that needs to
# record who acted does not have to depend on CurrentUser as well.
# --------------------------------------------------------------------------- #

IndexViewer = Annotated[User, Depends(require_permission(Permission.INDEXING_VIEW))]
IndexRebuilder = Annotated[User, Depends(require_permission(Permission.INDEXING_REINDEX))]
IndexMonitor = Annotated[User, Depends(require_permission(Permission.INDEXING_MONITOR))]

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
            "No live document has this identifier, it has no such version, or no index record "
            "exists for it."
        ),
    }
}
_CONFLICT: dict[int | str, dict[str, object]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": (
            "Indexing is already queued or running for this document version, or its text has "
            "not been extracted yet."
        ),
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "Request validation failed.",
    }
}
_DISABLED: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "Document indexing is disabled on this deployment.",
    }
}

VersionQuery = Annotated[
    int | None,
    Query(
        ge=1,
        description=(
            "Document version to address. Defaults to the current version; each version keeps "
            "its own index, so earlier ones stay searchable after a replacement."
        ),
    ),
]


# --------------------------------------------------------------------------- #
# Per-document endpoints
# --------------------------------------------------------------------------- #


@document_indexing_router.get(
    "/{document_id}/index",
    response_model=IndexRead,
    status_code=status.HTTP_200_OK,
    summary="Get a document's search-index status",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_document_index(
    actor: IndexViewer,
    indexing: IndexingServiceDep,
    document_id: uuid.UUID,
    version: VersionQuery = None,
) -> IndexRead:
    """Return the indexing run for a document, current version by default.

    Carries everything the run recorded — status, start and finish times,
    duration, how many passages and pages it produced, which embedding model and
    collection, and the chunk settings it used — and **carries no passages and no
    vectors**: those live in the vector database, and reading them back is
    Semantic Search's job rather than this feature's.

    `is_active` says whether to keep polling and `can_reindex` whether to offer a
    Rebuild, both computed from the same rules the API enforces — so a client
    never offers an action the server is about to refuse.

    A document whose text has never been extracted has no index and answers
    **404** saying so.
    """
    return IndexRead.model_validate(
        indexing.get_index(document_id, actor=actor, version=version)
    )


@document_indexing_router.get(
    "/{document_id}/index/history",
    response_model=list[IndexRead],
    status_code=status.HTTP_200_OK,
    summary="List a document's indexing runs",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def list_document_index_history(
    actor: IndexViewer, indexing: IndexingServiceDep, document_id: uuid.UUID
) -> list[IndexRead]:
    """Return every index recorded for a document, oldest version first.

    A document accumulates one index per version: replacing it produces new text
    that needs its own vectors, while the previous version keeps the index built
    from *it*. This is that history, and it is what makes "which model was
    version 1 of this contract indexed with?" answerable after a replacement — the
    question that matters when the embedding model changes and everything built
    with the old one needs rebuilding.
    """
    return [
        IndexRead.model_validate(index)
        for index in indexing.list_document_indexes(document_id, actor=actor)
    ]


@document_indexing_router.post(
    "/{document_id}/index/reindex",
    response_model=IndexRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Rebuild a document's search index",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_VALIDATION,
        **_DISABLED,
    },
)
def reindex_document(
    actor: IndexRebuilder,
    indexing: IndexingServiceDep,
    document_id: uuid.UUID,
    version: VersionQuery = None,
) -> IndexRead:
    """Queue a fresh index for a document whose text is already extracted.

    **Nothing is uploaded and nothing stored is touched.** The document, its
    metadata, every stored version, and the extracted text are left exactly as
    they are; only the index is rebuilt from that text.

    **202, not 200**: the work has been accepted, not done. The response is the
    run in its `pending` state, and a client polls
    `GET /documents/{document_id}/index` for the outcome.

    **Idempotent per document version.** The existing run is re-used rather than
    replaced — same identifier, timing and failure fields cleared,
    `attempt_count` incremented — and the vectors it writes carry identifiers
    derived from their position in the document, so rebuilding overwrites rather
    than duplicating. The previous run's vectors for this version are removed
    first, so a rebuild that produces fewer passages leaves nothing behind. A run
    already queued or indexing answers **409** rather than silently queueing a
    duplicate.

    A version whose text has not been extracted answers **409** naming the
    extraction's state, because indexing begins where extraction ends. A version
    that has text but was never indexed — extracted while indexing was disabled,
    or before this feature existed — is queued for the first time here rather
    than refused.
    """
    return IndexRead.model_validate(
        indexing.reindex(document_id, actor=actor, version=version)
    )


# --------------------------------------------------------------------------- #
# Cross-document endpoints
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=IndexResultPage,
    status_code=status.HTTP_200_OK,
    summary="List indexing runs",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def list_indexes(
    actor: IndexViewer,
    indexing: IndexingServiceDep,
    query: Annotated[IndexListQuery, Query()],
) -> IndexResultPage:
    """Return a page of indexing runs the caller is entitled to see.

    Supports **filtering** by status, document, case, failure cause, and
    **embedding model**, **sorting** by queue time, start, finish, duration,
    passage count, or status in either direction, and **pagination**. Filters
    combine, so `?status=failed&case_id=…` is a valid narrowing — which is how an
    administrator finds every document on a case that is not searchable.

    The `embedding_model` filter is the one that is not merely convenience:
    changing the embedding model requires re-indexing every document, and this is
    how the documents still built with the previous one are found.

    Callers holding `cases:view-all` see every run. Everyone else sees only runs
    for documents on the cases they are assigned to — the scope is applied in the
    query, so `total_records` counts only what the caller may access.
    """
    result = indexing.list_indexes(query, actor=actor)
    return IndexResultPage.build(
        [IndexRead.model_validate(item) for item in result.results],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get(
    "/metrics",
    response_model=IndexMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Document indexing metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def get_index_metrics(
    actor: IndexMonitor,
    indexing: IndexingServiceDep,
    query: Annotated[IndexMetricsQuery, Query()],
) -> IndexMetricsRead:
    """Return platform-wide indexing health.

    The four figures the spec's Monitoring section names — **indexed documents,
    indexed chunks, average indexing duration, and failures** — plus the counts,
    the rates, and a breakdown of failures by cause. Rates are computed over
    *finished* runs only: counting queued work would make the success rate dip on
    every upload and recover as it processed, which measures traffic rather than
    quality.

    It also reports whether the **embedding model can actually load** and whether
    the **vector database answers**, and how many vectors the collection holds as
    the database itself reports them. Those are the three things the counts
    cannot tell apart: a platform indexing nothing because no model is installed,
    a platform indexing nothing because Qdrant is down, and a platform with
    nothing to index all show the same zero.

    An operational view of the pipeline, so it is gated on `indexing:monitor` and
    reports **counts, timings, and configuration only** — never a document, a
    case, or a passage of text. Pass `?window_days=N` to restrict it to recent
    runs.
    """
    metrics = indexing.metrics(window_days=query.window_days)
    return IndexMetricsRead(
        window_days=metrics.window_days,
        total_runs=metrics.counts.total,
        pending=metrics.counts.pending,
        indexing=metrics.counts.indexing,
        indexed=metrics.counts.indexed,
        failed=metrics.counts.failed,
        total_chunks=metrics.counts.total_chunks,
        success_rate=metrics.success_rate,
        failure_rate=metrics.failure_rate,
        average_duration_ms=metrics.counts.average_duration_ms,
        failures_by_code=metrics.failures_by_code,
        embedding_model=metrics.embedding_model,
        embedding_dimensions=metrics.embedding_dimensions,
        embedding_available=metrics.embedding_available,
        chunker=metrics.chunker,
        chunk_size=metrics.chunk_size,
        chunk_overlap=metrics.chunk_overlap,
        vector_collection=metrics.collection.name,
        vector_store_available=metrics.vector_store_available,
        vector_collection_exists=metrics.collection.exists,
        stored_vectors=metrics.collection.vector_count,
        enabled=metrics.enabled,
    )
