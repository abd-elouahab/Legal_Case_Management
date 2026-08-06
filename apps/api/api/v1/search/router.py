"""Semantic search endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.search.SearchService`, and shape the HTTP response. No
business logic lives here.

**Two endpoints, and there will not be a third in this module.**
``11-semantic-search.md`` is retrieval and nothing else: there is no endpoint
that answers a question, summarizes a document, or streams a response, because
none of those is retrieval — they belong to the RAG pipeline and the AI
assistant, which are separate features against this same service.

**Search is a POST, and that is a privacy decision rather than a stylistic one.**
A query string is written to the reverse proxy's access log, to the browser's
history, and to the ``Referer`` header of anything the page loads next. The spec
forbids logging user queries carrying sensitive legal information, and a `GET`
would put every query into three logs the application does not control. A body is
carried by none of them. It is still a read: it creates nothing, and it answers
**200** rather than 201.

Authorization is layered, and the two layers answer different questions:

* the ``search:*`` **capability** is required by the reusable dependencies in
  :mod:`api.authorization`, declared next to the route so it appears in the
  OpenAPI schema. Because ``CurrentUser`` resolves first, an anonymous caller
  gets **401** and only an authenticated-but-unentitled one gets **403**;
* the **resource** — which cases this caller is party to — is applied by the
  service, inside the vector query, because it needs the caller's assignments.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from api.authorization import require_permission
from api.deps import SearchServiceDep
from core.config import settings
from core.permissions import Permission
from models.user import User
from schemas.errors import ErrorResponse
from schemas.search import SearchMetricsRead, SearchRequest, SearchResponse

#: Mounted under ``/search``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

Searcher = Annotated[User, Depends(require_permission(Permission.SEARCH_QUERY))]
SearchMonitor = Annotated[User, Depends(require_permission(Permission.SEARCH_MONITOR))]

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
            "The account is disabled, lacks the required permission, or filtered by a case or "
            "document it is not assigned to."
        ),
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The case or document named by a filter does not exist.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "Request validation failed, or a metadata filter matches too many documents to "
            "search at once."
        ),
    }
}
_UNAVAILABLE: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "Search is disabled on this deployment, or the embedding model or vector database "
            "could not be reached."
        ),
    }
}


@router.post(
    "",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Search indexed documents in natural language",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_VALIDATION, **_UNAVAILABLE},
)
def search_documents(
    actor: Searcher, search: SearchServiceDep, request: SearchRequest
) -> SearchResponse:
    """Retrieve the passages most relevant to a natural-language query.

    The query is embedded with the **same model the documents were indexed
    with**, compared against the vector index, filtered, ranked by similarity,
    and returned as passages — each with the document, version, case, page, and
    chunk number it came from, plus its relevance score.

    **This endpoint retrieves; it does not answer.** No summary is generated, no
    prompt is assembled, and no language model is invoked. What comes back is
    text that already exists in the corpus, verbatim, with its provenance — which
    is exactly what a later RAG pipeline will ground an answer in.

    **Authorization is applied inside the vector query, never after it.** A caller
    retrieves only passages of documents they could already open: the cases they
    are assigned to, or every case if they hold `cases:view-all`. Filters narrow
    that set and can never widen it. A caller assigned to no cases receives an
    empty result set without a query being run.

    Filtering by a case or document the caller is not party to answers **403**
    rather than an empty page, so an inaccessible matter cannot be told apart
    from one with no matching text.

    **An empty result set is a success, not a 404**: the corpus holds nothing near
    the query, which is an answer. `has_more` reports whether another page may
    exist — a similarity search has no cheap exact total, and a wrong one would be
    worse than none.

    Arabic, French, and English share one embedding space, so a query in one
    language retrieves relevant passages written in the others.
    """
    outcome = search.search(request, actor=actor)

    return SearchResponse(
        query=outcome.query,
        results=outcome.results,
        result_count=len(outcome.results),
        limit=outcome.limit,
        offset=outcome.offset,
        has_more=outcome.has_more,
        duration_ms=outcome.duration_ms,
        top_score=outcome.top_score,
        average_score=outcome.average_score,
    )


@router.get(
    "/metrics",
    response_model=SearchMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Semantic search metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_search_metrics(actor: SearchMonitor, search: SearchServiceDep) -> SearchMetricsRead:
    """Return platform-wide retrieval health.

    The four figures the spec's Monitoring section names — **search count,
    average latency, average relevance score, and failed searches** — plus the
    rates, a breakdown of failures by cause, and the retrieval configuration.

    It also reports whether the **embedding model can actually load** and whether
    the **vector database answers**. Those are the three things the counters
    cannot tell apart: a platform answering no searches because no model is
    installed, one answering none because Qdrant is down, and one nobody has
    searched yet all show the same zeros.

    `since` is the honest caveat, not decoration: these counters live in the API
    process rather than in a table, so they reset when it restarts and each
    instance counts only its own traffic.

    An operational view of the pipeline, so it is gated on `search:monitor` and
    reports **counts, timings, and configuration only** — never a query, a
    document, a case, or a passage of text.
    """
    health = search.health()
    metrics = health.metrics

    return SearchMetricsRead(
        since=metrics.since,
        total_searches=metrics.total_searches,
        successful_searches=metrics.successful_searches,
        failed_searches=metrics.failed_searches,
        success_rate=metrics.success_rate,
        failure_rate=metrics.failure_rate,
        average_latency_ms=metrics.average_latency_ms,
        average_score=metrics.average_score,
        total_results=metrics.total_results,
        average_results=metrics.average_results,
        failures_by_code=metrics.failures_by_code,
        embedding_model=health.embedding_model,
        embedding_dimensions=health.embedding_dimensions,
        embedding_available=health.embedding_available,
        vector_collection=health.vector_collection,
        vector_store_available=health.vector_store_available,
        ranker=health.ranker,
        # Read at call time rather than captured at import, so a deployment that
        # reconfigures its limits does not need a restart for the monitoring view
        # to agree with what the endpoint will actually accept.
        default_limit=settings.SEARCH_DEFAULT_LIMIT,
        max_limit=settings.SEARCH_MAX_LIMIT,
        min_score=settings.SEARCH_MIN_SCORE,
        enabled=health.enabled,
    )
