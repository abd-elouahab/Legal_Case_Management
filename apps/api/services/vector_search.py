"""The vector-retrieval boundary.

``11-semantic-search.md`` requires similarity search over the vectors Document
Indexing wrote, with metadata filtering applied *before* results are returned,
and requires the feature to stay usable if the database changes. This module is
that boundary: it is the only place in the platform that turns a query vector
into matches, and — with :mod:`services.vector_store` — one of only two that
speak Qdrant's data model.

**It is a new module rather than a method on the existing store, and that is the
load-bearing decision.** ``10-document-indexing.md`` required indexing to stay
independent from retrieval, and the codebase made that structural:
:class:`~services.vector_store.VectorStore` exposes write, delete, and count and
**no query method at all**, so a search feature *cannot* be smuggled in through
it. Honouring that boundary means retrieval arrives here, as its own read-side
protocol, and the two halves stay separable — an indexing deployment that never
searches wires only the first, and a future read-only replica search wires only
the second.

The shape mirrors :mod:`services.vector_store`, :mod:`services.chunking`, and
:mod:`services.embedding`:

* :class:`VectorSearcher` is the protocol :mod:`services.search` depends on;
* :class:`QdrantVectorSearcher` is the implementation;
* :func:`get_vector_searcher` resolves it, so an alternative vector database is
  one class plus one registry entry.

**Filtering happens in the database, never in Python.** The spec requires that
filtering occur before results are returned, and there is a security reason as
well as a performance one: the case scope is a *filter*, so applying it after
retrieval would mean asking Qdrant for the top ten passages on the platform and
then hiding most of them — which returns fewer results than asked for, leaks the
existence of matches through the count, and puts unauthorized text into the
process at all. Every restriction in :class:`SearchFilters` becomes a Qdrant
condition.

**Nothing here ranks, and nothing here generates.** Ordering by score is
Qdrant's; re-ordering is :mod:`services.search_ranking`'s; and no module in this
feature imports a prompt, a provider, or an orchestrator.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import structlog

from core.config import settings
from core.search import SearchFailureCode

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class VectorSearchError(Exception):
    """The vector database could not be reached, or refused the query.

    Deliberately **not** an :class:`~core.exceptions.AppException`, exactly as
    :class:`~services.vector_store.VectorStoreError` is not: this module is a
    library boundary, and translating its failure into a status line is the
    service's job. The service raises
    :class:`~core.exceptions.SearchUnavailableError` carrying this code.
    """

    #: The cause, reported to the caller and used to group failures in the
    #: monitoring view.
    code: SearchFailureCode = SearchFailureCode.VECTOR_STORE_UNAVAILABLE

    def __init__(self, message: str, *, code: SearchFailureCode | None = None) -> None:
        self.code = code or self.code
        super().__init__(message)


# --------------------------------------------------------------------------- #
# What is asked, and what comes back
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Every restriction applied to a vector query, in one value.

    Frozen and assembled by the service, so a searcher cannot be handed a
    half-built filter and cannot mutate one mid-query. Each field maps onto a
    payload key Document Indexing writes (see
    :func:`~services.vector_store.build_payload`) — which is exactly why this
    feature needs no schema change and no re-indexing to filter by any of them.

    ``case_ids`` is the security-bearing field and its two empty forms are **not
    interchangeable**:

    * ``None`` means *no case restriction*, and only a caller holding
      ``cases:view-all`` may produce it;
    * an **empty frozenset** means *no accessible cases*, and must match nothing.

    Conflating the two is the single most dangerous mistake this feature could
    make — it would turn "this lawyer is assigned to nothing" into "this lawyer
    searches everything" — so :meth:`matches_nothing` names the second case
    explicitly and the service short-circuits on it before a query is ever built.
    """

    #: Cases whose passages may be returned. ``None`` lifts the restriction.
    case_ids: frozenset[uuid.UUID] | None = None
    #: Documents whose passages may be returned. ``None`` lifts the restriction.
    document_ids: frozenset[uuid.UUID] | None = None
    #: A single document version. Absent searches every indexed version, which is
    #: the right default: a replacement's predecessor keeps its own index, and a
    #: clause that was removed in version 2 is still findable in version 1.
    document_version: int | None = None
    #: ISO 639-1 codes a passage may be labelled with.
    languages: frozenset[str] = field(default_factory=frozenset)
    #: Earliest and latest indexing timestamps a passage may carry. The spec's
    #: "date" filter: the platform stores no other date on a vector, and
    #: ``indexed_at`` is the one that is actually on the point.
    indexed_from: datetime | None = None
    indexed_to: datetime | None = None
    #: Only passages embedded with this model. Not a user-facing filter — it
    #: exists because comparing vectors from two different models is meaningless,
    #: so a deployment mid-migration can pin retrieval to the current one.
    embedding_model: str | None = None

    def matches_nothing(self) -> bool:
        """Whether these filters can match no point at all, without querying.

        True when the caller is party to no case, or when a document filter
        resolved to nothing. Checked before a query is built so the platform
        neither embeds a query it cannot use nor asks Qdrant a question whose
        answer is known.
        """
        if self.case_ids is not None and not self.case_ids:
            return True
        return self.document_ids is not None and not self.document_ids


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """One retrieved passage: how close it is, and everything indexing stored.

    The payload is passed through as the mapping Qdrant returned rather than
    unpacked into fields here, because this module's job is retrieval and the
    *shape* of a result belongs to :mod:`schemas.search`. Unpacking here would
    put the response contract in the driver boundary, which is where it is
    hardest to change.
    """

    #: Qdrant's point identifier — derived from the chunk's position by
    #: :func:`~core.indexing.chunk_point_id`, so it is stable across re-indexes.
    point_id: str
    #: Cosine similarity, higher is closer. The collection is created for cosine
    #: distance and the embedder returns unit-length vectors, so this is a dot
    #: product in ``[-1, 1]`` and is comparable across runs and across months.
    score: float
    payload: Mapping[str, Any]


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class VectorSearcher(Protocol):
    """What the search service requires of a vector database.

    Three members: the collection it reads, an availability probe, and one
    ``search`` call. **No write, no delete, and no upsert**, which is the mirror
    image of :class:`~services.vector_store.VectorStore` having no query — the
    two protocols together say that indexing writes and search reads, and neither
    can do the other's job by accident.
    """

    @property
    def collection(self) -> str:
        """Name of the collection searched."""
        ...

    def is_available(self) -> bool:
        """Whether the database can be reached right now."""
        ...

    def search(
        self,
        vector: Sequence[float],
        *,
        filters: SearchFilters,
        limit: int,
        offset: int = 0,
        score_threshold: float | None = None,
    ) -> list[VectorMatch]:
        """Return the closest passages to ``vector``, most similar first.

        Args:
            vector: the embedded query, produced by the *same* model that
                embedded the passages.
            filters: every restriction, applied in the database.
            limit: how many matches to return.
            offset: how many to skip, for pagination.
            score_threshold: minimum similarity, applied by the database.

        Raises:
            VectorSearchError: the query could not be answered.
        """
        ...


# --------------------------------------------------------------------------- #
# Qdrant
# --------------------------------------------------------------------------- #


class QdrantVectorSearcher:
    """Qdrant similarity search, through the shared client in :mod:`core.vector`.

    Every Qdrant exception is translated into a :class:`VectorSearchError` at this
    boundary, so the service above reports a cause without knowing what an
    ``UnexpectedResponse`` is — and so a driver message, which can quote a
    payload and therefore a document's text, never leaves this module.
    """

    #: The identifier recorded for this backend.
    name = "qdrant"

    def __init__(self, *, collection: str | None = None, client: Any | None = None) -> None:
        self._collection = collection or settings.QDRANT_COLLECTION
        # Injectable so a unit test can drive the translation and filter-building
        # logic with a stub and no running Qdrant — the same reason
        # `QdrantVectorStore` takes one.
        self._client = client

    # ------------------------------------------------------------ identity #

    @property
    def collection(self) -> str:
        """Name of the collection searched."""
        return self._collection

    def is_available(self) -> bool:
        """Whether Qdrant answers.

        Probed rather than assumed, so an unreachable database is reported as a
        503 naming the cause and a ``false`` on the monitoring endpoint, instead
        of surfacing as a stack trace on a user's first search.
        """
        try:
            self._qdrant().get_collections()
        except Exception:
            return False
        return True

    # -------------------------------------------------------------- search #

    def search(
        self,
        vector: Sequence[float],
        *,
        filters: SearchFilters,
        limit: int,
        offset: int = 0,
        score_threshold: float | None = None,
    ) -> list[VectorMatch]:
        """Retrieve the closest passages, with every filter applied by Qdrant.

        ``with_payload=True`` and ``with_vectors=False``: the payload is what a
        result *is* (the passage, its page, its document), while the stored
        vector is a thousand floats nobody reads — returning it would multiply
        the response size by two orders of magnitude for no reader.

        A missing collection returns ``[]`` rather than raising. Nothing has been
        indexed yet, and "no documents match" is the honest answer to a search
        over an empty corpus — the monitoring endpoint is where an operator finds
        out *why* it is empty.

        Raises:
            VectorSearchError: Qdrant is unreachable, or refused the query.
        """
        if filters.matches_nothing():
            # Defence in depth. The service short-circuits before embedding, but
            # a filter that can match nothing must never reach the database as an
            # *unfiltered* query, which is what an empty `MatchAny` could become
            # if a future edit dropped the condition.
            return []

        client = self._qdrant()

        try:
            if not client.collection_exists(self._collection):
                return []

            response = client.query_points(
                collection_name=self._collection,
                query=list(vector),
                query_filter=self.build_filter(filters),
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                score_threshold=score_threshold,
            )
        except Exception as exc:
            raise self._unreachable("query_points", exc) from exc

        return [
            VectorMatch(
                point_id=str(point.id),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in getattr(response, "points", [])
        ]

    # ------------------------------------------------------------- filters #

    @staticmethod
    def build_filter(filters: SearchFilters) -> Any | None:
        """Translate :class:`SearchFilters` into a Qdrant ``Filter``.

        Public rather than private because it is the part of this module worth
        asserting **against the driver's own models**: the payload keys, the
        condition types, and the datetime shape are a contract with a library,
        and the codebase has already been burned three times by a hand-written
        double that accepted a shape real Qdrant rejects (see the ``FilterSelector``
        note in :mod:`services.vector_store`). A test builds these filters and
        checks them against ``qdrant_client.models``.

        Every condition goes in ``must``, so they combine with AND and **the case
        scope cannot be widened by any combination of the rest**. That is the
        spec's *"metadata filtering cannot bypass permissions"*, and it holds
        structurally: there is no ``should`` branch here for a user filter to
        land in.

        Returns ``None`` when nothing is restricted, which is only reachable for
        a caller holding ``cases:view-all`` who filtered by nothing.
        """
        from qdrant_client.models import (
            DatetimeRange,
            FieldCondition,
            Filter,
            MatchAny,
            MatchValue,
        )

        conditions: list[Any] = []

        if filters.case_ids is not None:
            # Sorted, so the same scope produces the same request — which makes a
            # query cacheable upstream and a failure reproducible from a log.
            conditions.append(
                FieldCondition(
                    key="case_id",
                    match=MatchAny(any=sorted(str(case_id) for case_id in filters.case_ids)),
                )
            )

        if filters.document_ids is not None:
            conditions.append(
                FieldCondition(
                    key="document_id",
                    match=MatchAny(
                        any=sorted(str(document_id) for document_id in filters.document_ids)
                    ),
                )
            )

        if filters.document_version is not None:
            conditions.append(
                FieldCondition(
                    key="document_version",
                    match=MatchValue(value=filters.document_version),
                )
            )

        if filters.languages:
            conditions.append(
                FieldCondition(key="language", match=MatchAny(any=sorted(filters.languages)))
            )

        if filters.embedding_model is not None:
            conditions.append(
                FieldCondition(
                    key="embedding_model", match=MatchValue(value=filters.embedding_model)
                )
            )

        if filters.indexed_from is not None or filters.indexed_to is not None:
            # `DatetimeRange`, not `Range`: `indexed_at` is stored as an RFC 3339
            # string (see `build_payload`), and a numeric range over a string
            # field matches nothing at all rather than failing — the worst
            # possible outcome, because it looks like "no results".
            conditions.append(
                FieldCondition(
                    key="indexed_at",
                    range=DatetimeRange(gte=filters.indexed_from, lte=filters.indexed_to),
                )
            )

        if not conditions:
            return None

        return Filter(must=conditions)

    # ------------------------------------------------------------- helpers #

    def _qdrant(self) -> Any:
        """The shared Qdrant client, or the injected stub."""
        if self._client is not None:
            return self._client

        from core.vector import qdrant_client

        return qdrant_client

    def _unreachable(self, operation: str, exc: Exception) -> VectorSearchError:
        """Translate a driver failure, keeping its message out of the log.

        The exception's own text can include the filter it was given and the
        payload it was reading — the first names the caller's cases, the second
        is the document's contents. The *type* is logged instead, which is what
        an operator needs to tell a connection refusal from a rejected request.
        """
        logger.error(
            "search_vector_query_failed",
            collection=self._collection,
            operation=operation,
            error_type=type(exc).__name__,
        )
        return VectorSearchError("The search index could not answer this query.")


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every vector-search backend this build can be configured to use.
#:
#: Adding one is a class implementing :class:`VectorSearcher` plus an entry here
#: — the spec's "alternative vector databases" extensibility, in the same shape
#: as :data:`~services.embedding.EMBEDDER_FACTORIES`.
SEARCHER_FACTORIES: Mapping[str, type[QdrantVectorSearcher]] = {
    QdrantVectorSearcher.name: QdrantVectorSearcher
}


def get_vector_searcher(collection: str | None = None) -> VectorSearcher:
    """Build the configured vector searcher.

    A plain constructor call today, because Qdrant is the only implementation and
    `architecture.md` names it. It exists as a function so that adding a second
    searcher is a registry here rather than an edit at every call site — the same
    shape as :func:`~services.vector_store.get_vector_store`.
    """
    return QdrantVectorSearcher(collection=collection)


__all__ = [
    "SEARCHER_FACTORIES",
    "QdrantVectorSearcher",
    "SearchFilters",
    "VectorMatch",
    "VectorSearchError",
    "VectorSearcher",
    "get_vector_searcher",
]
