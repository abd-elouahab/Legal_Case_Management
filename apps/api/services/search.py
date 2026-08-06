"""Semantic search business logic.

Owns the workflow ``11-semantic-search.md`` defines, in exactly the order its
flow diagram gives:

.. code-block:: text

    User Query → Generate Query Embedding → Search Qdrant
               → Filter Results → Rank Results → Return Relevant Chunks

with one step the diagram leaves implicit and this module makes explicit: the
caller's **authorization scope is resolved before the embedding**, and becomes
part of the filter rather than a pass over the results.

Scope boundaries, kept deliberately sharp:

* **Query embedding is not done here.** :mod:`services.embedding` owns it —
  *the same module Document Indexing uses*, which is not a convenience but the
  requirement: ``ai-architecture.md`` states that the same model must embed
  documents and user queries, and a second embedder would be a second model the
  day either is reconfigured.
* **Vector retrieval is not done here.** :mod:`services.vector_search` owns it,
  behind a protocol, so an alternative vector database is one class.
* **Ranking is not done here.** :mod:`services.search_ranking` owns it, behind a
  protocol, so a future cross-encoder reranker is one class.
* **Capability authorization is not decided here.** Whether the caller may search
  at all is settled by the dependencies in :mod:`api.authorization` before a
  request reaches this service.
* **Per-resource authorization is applied here**, through
  :class:`~services.search_access.SearchAccessPolicy` and the case scope it
  yields, because it needs the caller's assignments — which a dependency cannot
  know without loading them.
* **Nothing about answer generation, summarization, citation prose, a prompt, a
  conversation, or an LLM appears anywhere in this module — or anywhere in this
  feature.** The spec puts all of them out of scope and requires retrieval to
  stay independent from answer generation. The boundary is structural: this
  service's collaborators are an embedder, a vector searcher, a ranker, and two
  repositories, and none of them can generate text.

What it does own are the rules nothing else can express: that a caller retrieves
only passages of documents they could already open, that a filter can narrow that
set but never widen it, that a deleted document's passages disappear with it,
that a search which matches nothing is a *success*, and that no query text
reaches a log.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

import structlog

from core.config import settings
from core.exceptions import (
    CaseNotFoundError,
    DocumentNotFoundError,
    SearchDisabledError,
    SearchFilterTooBroadError,
    SearchUnavailableError,
)
from core.search import (
    SearchFailureCode,
    average_score,
    failure_message,
    loggable_query,
    query_fingerprint,
    round_score,
    top_score,
)
from models.document import Document
from models.user import User
from repositories.case import CaseRepository
from repositories.document import DocumentRepository
from repositories.search import SearchRepository
from schemas.search import (
    SearchDocumentSummary,
    SearchFilterInput,
    SearchRequest,
    SearchResultRead,
    result_from_payload,
)
from services.embedding import Embedder, EmbeddingError, get_embedder
from services.search_access import SearchAccessPolicy
from services.search_metrics import (
    NullSearchMetrics,
    SearchMetricsRecorder,
    SearchMetricsSnapshot,
)
from services.search_ranking import Ranker, get_ranker
from services.vector_search import (
    SearchFilters,
    VectorMatch,
    VectorSearcher,
    VectorSearchError,
    get_vector_searcher,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """One answered search: the passages, and how the search went.

    Returned instead of the schema so the router owns serialization and the
    service owns the workflow — the same split every other service here makes.
    """

    query: str
    results: list[SearchResultRead]
    limit: int
    offset: int
    duration_ms: int
    top_score: float | None
    average_score: float | None
    #: How many points the vector database returned, **before** results whose
    #: document has since been deleted were dropped. Kept separately from
    #: ``results`` because it is what :attr:`has_more` has to be computed from —
    #: see there.
    retrieved: int = 0

    @property
    def has_more(self) -> bool:
        """Whether another page may exist.

        A full page is the only honest signal a similarity search can give
        without a second query: an exact total would mean counting every point
        above the threshold, which is another full scan of the collection to
        produce a number nobody acts on.

        Computed from what the **database returned**, not from what survived
        filtering afterwards. A page of ten that lost two to deleted documents
        has eight results and *still* has more behind it; reading the surviving
        count would say otherwise and strand the reader on page one with no way
        forward.
        """
        return self.retrieved >= self.limit


@dataclass(frozen=True, slots=True)
class SearchHealth:
    """Platform-wide search health, as the monitoring endpoint reports it."""

    metrics: SearchMetricsSnapshot
    embedding_model: str
    embedding_dimensions: int
    embedding_available: bool
    vector_collection: str
    vector_store_available: bool
    ranker: str
    enabled: bool


class SearchService:
    """Coordinates natural-language retrieval over indexed documents."""

    def __init__(
        self,
        searches: SearchRepository,
        documents: DocumentRepository,
        cases: CaseRepository,
        embedder: Embedder | None = None,
        searcher: VectorSearcher | None = None,
        ranker: Ranker | None = None,
        access: SearchAccessPolicy | None = None,
        *,
        metrics: SearchMetricsRecorder | None = None,
    ) -> None:
        self._searches = searches
        self._documents = documents
        self._cases = cases
        self._embedder = embedder or get_embedder()
        self._searcher = searcher or get_vector_searcher()
        self._ranker = ranker or get_ranker()
        self._access = access or SearchAccessPolicy()
        # See `TimelineService`'s null recorder for why the default records
        # nothing; the application wires the process-wide one in
        # `api.deps.get_search_service`.
        self._metrics: SearchMetricsRecorder = metrics or NullSearchMetrics()

    # ------------------------------------------------------------ searching #

    def search(self, request: SearchRequest, *, actor: User) -> SearchOutcome:
        """Answer one natural-language search.

        The spec's flow, in order, with the authorization scope resolved first:

        1. **Resolve the scope** — the cases this caller is party to, plus any
           case or document they explicitly filtered by. A caller party to
           nothing short-circuits here, before a vector is ever produced.
        2. **Generate the query embedding**, with the same model the corpus was
           indexed with.
        3. **Search Qdrant**, with every filter applied *by the database*.
        4. **Rank**, which today orders by similarity and breaks ties by position
           in the document so the same query returns the same page.
        5. **Return the passages**, each with its document, version, case, page,
           chunk number, and score.

        Raises:
            SearchDisabledError: searching is switched off for this deployment.
            SearchAccessDeniedError: the caller filtered by a case or document
                they are not party to.
            CaseNotFoundError: the case filter names no case.
            DocumentNotFoundError: the document filter names no live document.
            SearchFilterTooBroadError: a metadata filter resolved to more
                documents than can be pushed into one vector query.
            SearchUnavailableError: the embedding model or the vector database
                could not be reached.
        """
        if not settings.SEARCH_ENABLED:
            logger.info(
                "search_rejected",
                reason="disabled",
                actor_id=str(actor.id),
                query_fingerprint=query_fingerprint(request.query),
            )
            raise SearchDisabledError

        started = time.monotonic()

        logger.info(
            "search_requested",
            **self._event(request, actor=actor),
        )

        # Resolved before anything expensive: a caller with no accessible cases,
        # or a filter that cannot match, costs neither an embedding nor a round
        # trip. It is also the *security* order — the scope is decided before the
        # query exists, so nothing about the query can influence it.
        filters = self._resolve_filters(request.filters, actor=actor)

        if filters.matches_nothing():
            return self._empty(request, started=started, actor=actor, reason="no_scope")

        vector = self._embed(request, actor=actor, started=started)
        matches = self._retrieve(request, vector=vector, filters=filters, actor=actor, started=started)

        ranked = self._ranker.rank(request.query, matches)
        results = self._build_results(ranked)

        duration_ms = self._elapsed_ms(started)
        scores = [result.score for result in results]
        mean = average_score(scores)

        self._metrics.record_success(
            duration_ms=duration_ms, result_count=len(results), average_score=mean
        )

        logger.info(
            "search_completed",
            result_count=len(results),
            retrieved_count=len(matches),
            duration_ms=duration_ms,
            top_score=top_score(scores),
            average_score=mean,
            **self._event(request, actor=actor),
        )

        return SearchOutcome(
            query=request.query,
            results=results,
            limit=request.limit,
            offset=request.offset,
            duration_ms=duration_ms,
            top_score=top_score(scores),
            average_score=mean,
            retrieved=len(matches),
        )

    # ------------------------------------------------------------- the scope #

    def _resolve_filters(self, wanted: SearchFilterInput, *, actor: User) -> SearchFilters:
        """Turn the request's filters and the caller's scope into one vector filter.

        **The authorization scope is not one filter among several.** It is
        computed here from the caller alone, it is intersected with — never
        replaced by — whatever the request asked for, and it lands in the
        vector query's ``must`` list, which has no ``or`` branch. That is the
        spec's *"metadata filtering cannot bypass permissions"*, enforced by the
        shape of the value rather than by the order of a series of ``if``\\ s.

        The two empty forms of ``case_ids`` are kept distinct throughout: ``None``
        means "this caller holds ``cases:view-all``", an empty set means "this
        caller may search nothing". Collapsing them would turn an unassigned
        lawyer into a platform-wide reader.
        """
        case_ids = self._scope_case_ids(actor)
        document_ids: frozenset[uuid.UUID] | None = None

        if wanted.case_id is not None:
            legal_case = self._cases.get_by_id(wanted.case_id)
            if legal_case is None:
                logger.info(
                    "search_case_lookup_failed",
                    case_id=str(wanted.case_id),
                    actor_id=str(actor.id),
                )
                raise CaseNotFoundError
            # 403 rather than an empty page: an inaccessible case and a case with
            # no matching passages must not be indistinguishable.
            self._access.require_case_access(actor, legal_case)
            case_ids = frozenset({legal_case.id})

        if wanted.document_id is not None:
            document = self._require_document(wanted.document_id)
            self._access.require_document_access(actor, document)
            document_ids = frozenset({document.id})
            # Narrowed to the document's own case as well, so the two conditions
            # agree: a request naming a document *and* a different case would
            # otherwise be silently satisfiable by neither.
            case_ids = frozenset({document.case_id})

        if wanted.needs_document_lookup:
            document_ids = self._resolve_document_filters(
                wanted, case_ids=case_ids, document_ids=document_ids, actor=actor
            )

        return SearchFilters(
            case_ids=case_ids,
            document_ids=document_ids,
            document_version=wanted.document_version,
            languages=frozenset(wanted.languages or ()),
            indexed_from=wanted.indexed_from,
            indexed_to=wanted.indexed_to,
        )

    def _scope_case_ids(self, actor: User) -> frozenset[uuid.UUID] | None:
        """The cases this caller may search, as a set — or ``None`` for all of them.

        ``None`` is reachable only for a caller holding ``cases:view-all``; every
        other caller gets the set of cases they are assigned to, **including the
        empty set**, which matches nothing. The two are different values on
        purpose and are never collapsed: an unassigned lawyer must search nothing,
        not everything.
        """
        scope = self._access.visibility_scope(actor)
        if scope is None:
            return None
        return frozenset(self._searches.accessible_case_ids(scope))

    def _resolve_document_filters(
        self,
        wanted: SearchFilterInput,
        *,
        case_ids: frozenset[uuid.UUID] | None,
        document_ids: frozenset[uuid.UUID] | None,
        actor: User,
    ) -> frozenset[uuid.UUID]:
        """Resolve category and file-type filters to a bounded set of document ids.

        The vector payload carries neither: Document Indexing stores what a
        *chunk* is, not what its document is. Rather than re-index the corpus to
        add two fields — an absurd price for a dropdown — the filter is answered
        in PostgreSQL and pushed into the vector query as one ``document_id``
        condition.

        Bounded by ``SEARCH_MAX_FILTER_DOCUMENTS``, and **refused** rather than
        truncated when it overflows: a silently shortened set would drop matching
        documents with nothing to indicate it, and the caller can always narrow
        by case. The overflow is detected by asking for one row more than the
        ceiling, so it costs no extra query.
        """
        ceiling = settings.SEARCH_MAX_FILTER_DOCUMENTS
        resolved = self._searches.document_ids_matching(
            case_ids=case_ids,
            document_ids=document_ids,
            categories=wanted.categories,
            file_types=wanted.file_types,
            visible_to=self._access.visibility_scope(actor),
            limit=ceiling + 1,
        )

        if len(resolved) > ceiling:
            logger.info(
                "search_rejected",
                reason="filter_too_broad",
                actor_id=str(actor.id),
                limit=ceiling,
            )
            raise SearchFilterTooBroadError(ceiling)

        return frozenset(resolved)

    # -------------------------------------------------------------- the work #

    def _embed(self, request: SearchRequest, *, actor: User, started: float) -> list[float]:
        """Embed the query with the model the corpus was indexed with.

        ``ai-architecture.md`` requires the *same* model for documents and
        queries, and this is where that requirement is honoured — by calling the
        same :mod:`services.embedding` module rather than by remembering to
        configure two settings identically.

        The embedder normalises to unit length, so the score Qdrant returns is a
        cosine similarity directly comparable with every other search.

        Raises:
            SearchUnavailableError: the model is missing, or failed.
        """
        try:
            batch = self._embedder.embed([request.query])
        except EmbeddingError:
            # The library's message can echo the text it was encoding — which
            # here is the user's query — so it is deliberately neither logged nor
            # carried into the response.
            self._fail(
                request,
                actor=actor,
                started=started,
                code=SearchFailureCode.EMBEDDING_UNAVAILABLE,
            )

        if not batch.vectors:  # pragma: no cover - embedder contract
            self._fail(
                request,
                actor=actor,
                started=started,
                code=SearchFailureCode.EMBEDDING_UNAVAILABLE,
            )

        return batch.vectors[0]

    def _retrieve(
        self,
        request: SearchRequest,
        *,
        vector: Sequence[float],
        filters: SearchFilters,
        actor: User,
        started: float,
    ) -> list[VectorMatch]:
        """Retrieve the nearest passages, with every filter applied by Qdrant.

        Raises:
            SearchUnavailableError: the vector database could not answer.
        """
        threshold = request.min_score if request.min_score is not None else settings.SEARCH_MIN_SCORE

        try:
            return self._searcher.search(
                vector,
                filters=filters,
                limit=request.limit,
                offset=request.offset,
                # `None` rather than 0.0 when no floor is configured: Qdrant
                # treats a threshold as a hard cut, and 0.0 would silently
                # discard legitimately weak-but-relevant matches on a corpus
                # where nothing scores highly — an Arabic query against a French
                # filing, for instance.
                score_threshold=threshold if threshold else None,
            )
        except VectorSearchError as exc:
            self._fail(request, actor=actor, started=started, code=exc.code)

    def _build_results(self, matches: Sequence[VectorMatch]) -> list[SearchResultRead]:
        """Project ranked matches onto the response shape, resolving documents once.

        Two things happen here that are not projection, and both are deliberate:

        * **the document summaries are loaded in one query** for the whole page,
          never one per hit — the spec's "minimize unnecessary database requests"
          on the platform's most frequent operation;
        * **a passage whose document no longer exists is dropped.** Deletion is
          logical (``documents.deleted_at``), so the vectors outlive it and would
          otherwise keep surfacing the contents of a withdrawn document. This is
          the point at which a search result stops being more visible than the
          document it came from.

        A match whose payload is malformed is skipped rather than raising: one bad
        point, written by an older build or a partially-failed run, must not make
        the whole search fail.
        """
        projected: list[tuple[dict[str, Any], uuid.UUID]] = []

        for rank, match in enumerate(matches, start=1):
            payload = dict(match.payload)
            try:
                fields = result_from_payload(payload, score=round_score(match.score), rank=rank)
                document_id = uuid.UUID(str(fields["document_id"]))
            except (KeyError, TypeError, ValueError):
                logger.warning("search_result_payload_invalid", point_id=match.point_id)
                continue
            projected.append((fields, document_id))

        documents = self._searches.documents_by_id([document_id for _, document_id in projected])

        results: list[SearchResultRead] = []
        for fields, document_id in projected:
            document = documents.get(document_id)
            if document is None:
                # Deleted between indexing and searching. Dropped rather than
                # returned without a summary: the passage is the document's
                # contents, and the document is no longer readable.
                logger.info("search_result_document_unavailable", document_id=str(document_id))
                continue
            results.append(
                SearchResultRead(
                    **fields,
                    document=SearchDocumentSummary.model_validate(document),
                )
            )

        # Re-ranked after the drop, so the positions a client cites are
        # contiguous and start at 1 — a result list beginning at rank 3 would
        # read as though two results had been withheld.
        return [result.model_copy(update={"rank": rank}) for rank, result in enumerate(results, 1)]

    # --------------------------------------------------------------- health #

    def health(self) -> SearchHealth:
        """Aggregate search health for the monitoring endpoint.

        No per-resource scope is applied, deliberately: this is an operational
        view of the *retrieval pipeline*, gated on the administrative
        ``search:monitor`` permission, and it reports counts, timings, and
        configuration only — never a query, a document, a case, or a passage.
        Scoping it per caller would make the platform's latency mean something
        different for each of them, which is not a health metric.

        Availability is probed rather than inferred from the counters, for the
        same reason the indexing metrics probe it: a platform answering no
        searches because no model is installed, one answering none because Qdrant
        is down, and one nobody has searched yet all show the same zeros.
        """
        return SearchHealth(
            metrics=self._metrics.snapshot(),
            embedding_model=self._embedder.model,
            embedding_dimensions=self._embedder.dimensions,
            embedding_available=self._embedder.is_available(),
            vector_collection=self._searcher.collection,
            vector_store_available=self._searcher.is_available(),
            ranker=self._ranker.name,
            enabled=settings.SEARCH_ENABLED,
        )

    # ------------------------------------------------------------- helpers #

    def _empty(
        self, request: SearchRequest, *, started: float, actor: User, reason: str
    ) -> SearchOutcome:
        """Answer a search that cannot match, without touching a dependency.

        Recorded as a **success**, because it is one: the caller asked a valid
        question and the honest answer is "nothing". Counting it as a failure
        would make the failure rate a measure of who is assigned to what.
        """
        duration_ms = self._elapsed_ms(started)
        self._metrics.record_success(
            duration_ms=duration_ms, result_count=0, average_score=None
        )
        logger.info(
            "search_completed",
            result_count=0,
            retrieved_count=0,
            duration_ms=duration_ms,
            reason=reason,
            **self._event(request, actor=actor),
        )
        return SearchOutcome(
            query=request.query,
            results=[],
            limit=request.limit,
            offset=request.offset,
            duration_ms=duration_ms,
            top_score=None,
            average_score=None,
        )

    def _fail(
        self,
        request: SearchRequest,
        *,
        actor: User,
        started: float,
        code: SearchFailureCode,
    ) -> NoReturn:
        """Record a failed search and raise the 503 that describes it.

        One method rather than a ``record`` call beside each ``raise``, because
        the two must not come apart: a failure path that raised without recording
        would make the failure rate quietly under-report the exact outages the
        metric exists to surface. ``NoReturn`` is what lets the call sites be
        statements and still satisfy the type checker that the code after them is
        unreachable.

        The failure is a *dependency* outage, never a fault of the query: the
        message names neither the query nor anything it might have matched.

        Raises:
            SearchUnavailableError: always.
        """
        duration_ms = self._elapsed_ms(started)
        self._metrics.record_failure(duration_ms=duration_ms, code=code)
        logger.error(
            "search_failed",
            error_code=code.value,
            duration_ms=duration_ms,
            **self._event(request, actor=actor),
        )
        raise SearchUnavailableError(code.value, failure_message(code))

    def _require_document(self, document_id: uuid.UUID) -> Document:
        """Load a live document, or fail with a 404.

        Deliberately excludes soft-deleted documents, exactly as every other read
        path does: a withdrawn document is not searchable, because a search
        result is a reading of it.
        """
        document = self._documents.get_by_id(document_id)
        if document is None:
            logger.info("search_document_lookup_failed", document_id=str(document_id))
            raise DocumentNotFoundError
        return document

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """Milliseconds since the search began.

        From :func:`time.monotonic` rather than from wall-clock timestamps: a
        clock adjustment mid-request would produce a negative latency, and a
        negative sample in a monitoring average is worse than a slightly
        imprecise one.
        """
        return max(0, int((time.monotonic() - started) * 1000))

    @staticmethod
    def _event(request: SearchRequest, *, actor: User) -> dict[str, Any]:
        """The fields every search log entry carries.

        **The query itself is not one of them.** ``11-semantic-search.md`` forbids
        logging queries carrying sensitive legal information, and a lawyer's query
        is at least as revealing as the passage it finds — it names what they are
        looking for. What is logged instead is a salted, non-reversible
        *fingerprint*, which is enough to answer "this query fails every time" and
        "this is the platform's most common search" while telling an operator
        nothing about the matter.

        ``SEARCH_LOG_QUERIES`` is the spec's "unless existing project logging
        policies explicitly allow it" clause, made into a switch an operator sets
        rather than a decision this call site makes. It is off by default and
        adds the text *beside* the fingerprint rather than replacing it.

        The filters are reported as a shape — how many of each — rather than as
        values, for the same reason: a list of case identifiers is a list of the
        caller's matters.
        """
        return {
            "actor_id": str(actor.id),
            "role": actor.role.value,
            "query_fingerprint": query_fingerprint(request.query),
            "query_length": len(request.query),
            "query": loggable_query(request.query),
            "limit": request.limit,
            "offset": request.offset,
            "filtered": not request.filters.is_empty,
        }


__all__ = ["SearchHealth", "SearchOutcome", "SearchService"]
