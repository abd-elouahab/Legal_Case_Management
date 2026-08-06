"""The ranking boundary.

``11-semantic-search.md``: *"Rank results by semantic similarity. The
implementation should be extensible to support future reranking models."* This
module is that extension point, and it exists **now**, with a trivial
implementation, precisely so the later one is a class rather than a redesign:
:class:`~services.search.SearchService` already calls a ranker, so adding a
cross-encoder means implementing :class:`Ranker` and changing one setting.

The shape mirrors every other seam in this codebase
(:mod:`services.ocr_engine`, :mod:`services.chunking`,
:mod:`services.embedding`, :mod:`services.vector_store`,
:mod:`services.vector_search`):

* :class:`Ranker` is the protocol the service depends on;
* :class:`SimilarityRanker` is what ships;
* :func:`get_ranker` resolves the configured strategy by identifier.

**Why a ranker at all, when Qdrant already returns results in score order.**
Three reasons, and the third is the one that matters today:

* a future cross-encoder reranks the top-K by a *second*, more expensive model,
  which cannot be expressed as a database sort;
* hybrid search fuses two ordered lists, which is a ranking step by definition;
* and the order Qdrant returns is **not fully determined** — two passages with
  identical scores may come back in either order, so the same query can produce
  two different pages. :class:`SimilarityRanker` breaks those ties by the
  chunk's position in the document, which makes the result set reproducible.

**Nothing here calls a model, and nothing here generates.** The shipped ranker is
pure arithmetic over values the searcher already returned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

import structlog

from core.config import settings
from services.vector_search import VectorMatch

logger = structlog.get_logger(__name__)


class Ranker(Protocol):
    """What the search service requires of a ranking strategy.

    Two members, and the narrowness is the point: a ranker is handed the query
    and the matches and returns the matches in an order. It has no database, no
    request, no user, and no way to *add* a result — which is what makes it
    impossible for a future reranker to become a second, unauthorized retrieval
    path.
    """

    @property
    def name(self) -> str:
        """Stable identifier for the strategy ("similarity")."""
        ...

    def rank(self, query: str, matches: Sequence[VectorMatch]) -> list[VectorMatch]:
        """Return the matches in relevance order, most relevant first.

        A ranker may reorder and may drop, but must never invent: every returned
        match must be one it was given. That is what keeps authorization intact —
        the filters were applied when the matches were retrieved, so anything
        added afterwards would be unscoped.
        """
        ...


class SimilarityRanker:
    """Order by the similarity score the vector database computed.

    The spec's baseline — *"rank results by semantic similarity"* — and nothing
    more. It exists as a class rather than as a ``sorted()`` in the service so
    the service depends on the *protocol*, which is what makes the cross-encoder
    a drop-in later.

    The tie-break is the part that earns its keep. Passages with equal scores
    would otherwise be ordered by whatever Qdrant returned, which is not
    guaranteed stable across queries; ordering ties by (document, version, page,
    chunk) makes the same search return the same page every time, and orders the
    ties the way a reader expects — in reading order within the document.
    """

    #: The identifier recorded for this strategy.
    name = "similarity"

    def rank(self, query: str, matches: Sequence[VectorMatch]) -> list[VectorMatch]:
        """Sort by score descending, breaking ties by position in the document.

        ``query`` is unused here and that is correct rather than an oversight:
        similarity ranking already consumed the query when the vector was
        searched. It is on the protocol because a reranker *does* need it — a
        cross-encoder scores (query, passage) pairs — and a signature that grew
        later would be a breaking change at every call site.
        """
        return sorted(matches, key=self._key)

    @staticmethod
    def _key(match: VectorMatch) -> tuple[float, str, int, int, int]:
        """Sort key: best score first, then the chunk's position in the document.

        The score is negated rather than sorting with ``reverse=True``, because
        reversing would also reverse the tie-break and put the *last* chunk of a
        document first.

        Payload values are read defensively: a point written by an older build,
        or by a future one, must still be rankable — a missing page number sorts
        as ``0`` rather than raising, because a search must not fail on one
        malformed point.
        """
        payload = match.payload
        return (
            -match.score,
            str(payload.get("document_id", "")),
            _as_int(payload.get("document_version")),
            _as_int(payload.get("page_number")),
            _as_int(payload.get("chunk_number")),
        )


def _as_int(value: Any) -> int:
    """Coerce a payload value to an int, defaulting to ``0``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every ranking strategy this build can be configured to use.
#:
#: Adding one is a class implementing :class:`Ranker` plus an entry here — the
#: spec's "extensible to support future reranking models", in the same shape as
#: :data:`~services.embedding.EMBEDDER_FACTORIES`.
RANKER_FACTORIES: Mapping[str, type[SimilarityRanker]] = MappingProxyType(
    {SimilarityRanker.name: SimilarityRanker}
)


def available_rankers() -> list[str]:
    """Every ranker identifier this build can be configured to use."""
    return sorted(RANKER_FACTORIES)


def get_ranker(identifier: str | None = None) -> Ranker:
    """Return the configured ranker.

    Falls back to the default for an unrecognised identifier rather than failing
    startup, with the fallback logged so the misconfiguration is visible — the
    same posture :func:`~services.embedding.get_embedder` and
    :func:`~services.ocr_engine.get_ocr_engine` take. A ranker is cheap to build,
    so unlike the embedder there is nothing to share across the process.
    """
    wanted = (identifier or settings.SEARCH_RANKER).strip().lower()

    factory = RANKER_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "search_ranker_unknown", requested=wanted, fallback=SimilarityRanker.name
        )
        factory = SimilarityRanker

    return factory()


__all__ = [
    "RANKER_FACTORIES",
    "Ranker",
    "SimilarityRanker",
    "available_rankers",
    "get_ranker",
]
