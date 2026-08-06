"""Semantic-search domain utilities.

Small, pure helpers shared by the search schemas, repository, service, vector
searcher, ranker, and metrics recorder: how a query is normalised, what counts as
a searchable query, how a query is referred to in a log line *without being
written to it*, the failure vocabulary, and the arithmetic behind relevance
scores.

They live here rather than inside a service method for the same reason
:mod:`core.cases`, :mod:`core.documents`, :mod:`core.timeline`, :mod:`core.ocr`,
and :mod:`core.indexing` exist — the same rules must apply however a search
arrives, and they can be unit-tested without a database, a request, a running
Qdrant, or a downloaded embedding model.

**Nothing here embeds, searches, ranks, or reaches a network.** Query embedding
lives behind :mod:`services.embedding` (the *same* module indexing uses, because
``ai-architecture.md`` requires one model for both sides), vector retrieval
behind :mod:`services.vector_search`, and ranking behind
:mod:`services.search_ranking`. This module holds only the decisions that are the
*platform's* rather than a library's.

**And nothing here — or anywhere in this feature — generates an answer.**
``11-semantic-search.md`` is explicit that retrieval must not summarize, must not
assemble a prompt, and must not invoke an LLM. The boundary is structural: no
module in this feature imports a provider, a prompt template, or an orchestrator.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType

from core.config import settings

#: Shortest query worth embedding, in characters after normalisation.
#:
#: One character is never a question, and bge-m3 will happily return a vector for
#: it — a vector that is near nothing in particular and produces a page of
#: arbitrary passages. Refusing is more useful than answering badly.
MIN_QUERY_LENGTH = 2

#: Characters of the query digest carried in a log line. Long enough that two
#: different queries in one deployment will not collide in practice, short enough
#: that it reads as a correlation id rather than as data.
QUERY_FINGERPRINT_LENGTH = 12

#: Scores are rounded before they leave the service, so the same search reports
#: the same number on every surface and a float's last bits never leak into a
#: comparison a client makes.
SCORE_PRECISION = 4


class SearchFailureCode(StrEnum):
    """Why a search could not be answered.

    Machine-readable, so the monitoring view can group failures and a client can
    branch on the cause without parsing a sentence. Deliberately short: unlike
    OCR and indexing, a search has no persisted run and no retry — it either
    answers or it does not, and the only interesting question is *which*
    dependency was unavailable.
    """

    #: The query embedding could not be produced — the model is missing, or it
    #: failed. Nothing about the query is wrong; the same text will work once the
    #: model is available.
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    #: Qdrant could not be reached, or refused the query.
    VECTOR_STORE_UNAVAILABLE = "vector_store_unavailable"
    #: Anything the branches above do not describe.
    UNKNOWN = "unknown"


#: Human-readable explanation per failure code.
#:
#: Kept here rather than raised with the exception so the message a user reads is
#: written once and cannot vary between call sites. Every one is deliberately
#: free of the *query* and of any document contents — it says what went wrong
#: with the search, never what was being searched for.
FAILURE_MESSAGES: Mapping[SearchFailureCode, str] = MappingProxyType(
    {
        SearchFailureCode.EMBEDDING_UNAVAILABLE: (
            "The search service could not process this query. Indexed documents are unaffected."
        ),
        SearchFailureCode.VECTOR_STORE_UNAVAILABLE: (
            "The search index is unavailable. Indexed documents are unaffected."
        ),
        SearchFailureCode.UNKNOWN: "The search could not be completed.",
    }
)


def failure_message(code: SearchFailureCode) -> str:
    """The message shown for a failure code.

    Falls back to the generic explanation for a code with no entry, so a future
    member added without a message still reads as a sentence rather than as an
    identifier.
    """
    return FAILURE_MESSAGES.get(code, FAILURE_MESSAGES[SearchFailureCode.UNKNOWN])


# --------------------------------------------------------------------------- #
# The query
# --------------------------------------------------------------------------- #


def normalize_query(value: str) -> str:
    """Reduce a query to the exact text that is embedded.

    Three steps, each with a reason:

    * **NFC**, because the indexed passages were normalised the same way
      (:func:`~core.ocr.normalize_extracted_text`). An Arabic or French query
      typed with decomposed characters must embed to the same vector as the
      composed form, or a search for a word would miss the page containing it.
    * **whitespace collapsed**, so ``"loyer   commercial"`` and
      ``"loyer commercial"`` are one query rather than two — which matters
      because the fingerprint below is what tells repeated searches apart.
    * **control characters dropped**, because a query reaches a log's structured
      fields and a JSON response, and a stray ``\\x00`` belongs in neither.

    The result is *not* truncated here: length is a validation concern, and
    silently shortening a query would answer a question nobody asked. The schema
    rejects an over-long query instead.
    """
    if not value:
        return ""

    normalized = unicodedata.normalize("NFC", value)
    cleaned = "".join(
        character
        for character in normalized
        if character in {"\t", "\n"} or unicodedata.category(character)[0] != "C"
    )
    return " ".join(cleaned.split())


def is_searchable_query(value: str) -> bool:
    """Whether a normalised query carries enough to search for.

    A query of punctuation, or one shorter than :data:`MIN_QUERY_LENGTH`, is not
    a question — it is an accidental submit. Rejecting it costs the caller a 422
    and saves an embedding, a Qdrant round trip, and a page of arbitrary
    passages. Digits count as content: a case or article number is a perfectly
    good thing to search for.
    """
    trimmed = value.strip()
    if len(trimmed) < MIN_QUERY_LENGTH:
        return False
    return any(character.isalnum() for character in trimmed)


def query_fingerprint(value: str) -> str:
    """A stable, non-reversible handle for a query, for logs and metrics.

    ``11-semantic-search.md``: *"Never log user queries containing sensitive
    legal information unless existing project logging policies explicitly allow
    it."* This platform's policy (`code-standards.md`, and the practice OCR and
    indexing already established) forbids logging document contents — and a
    lawyer's query is at least as revealing as the passage it finds, because it
    names what they are looking for.

    So the log carries a **digest**, not the text. Two searches for the same
    thing share a fingerprint, which is what makes "this query fails every time"
    and "this query is the platform's most common" answerable — while the
    fingerprint itself yields nothing about the matter to an operator reading the
    log.

    Salted with the deployment's signing secret so the digest cannot be reversed
    by hashing a dictionary of likely legal terms and comparing. Without the
    salt, a twelve-character SHA-256 prefix of ``"divorce"`` is the same on every
    installation on earth, and a rainbow table of a few thousand legal phrases
    would undo the whole point.
    """
    digest = hashlib.sha256(
        f"{settings.JWT_SECRET_KEY}:{normalize_query(value)}".encode()
    ).hexdigest()
    return digest[:QUERY_FINGERPRINT_LENGTH]


def loggable_query(value: str) -> str | None:
    """The query text, only if this deployment has explicitly opted in.

    ``SEARCH_LOG_QUERIES`` is the "unless existing project logging policies
    explicitly allow it" clause of the spec, made into a switch an operator sets
    rather than a decision a call site makes. It is off by default, and every
    call site logs :func:`query_fingerprint` regardless — so turning it on adds
    the text beside the handle instead of replacing it.
    """
    if not settings.SEARCH_LOG_QUERIES:
        return None
    return normalize_query(value)


# --------------------------------------------------------------------------- #
# Scores
# --------------------------------------------------------------------------- #


def round_score(value: float) -> float:
    """Round a similarity score for transport.

    Rounded once, here, rather than at each surface: an unrounded cosine score
    differs in its last bits between a comparison made in the ranker and one made
    by a client, and a result list that reorders between two renderings of the
    same response is a bug nobody can reproduce.
    """
    return round(float(value), SCORE_PRECISION)


def average_score(scores: Iterable[float]) -> float | None:
    """Mean relevance across a result set, or ``None`` when there are none.

    ``None`` rather than ``0.0`` for an empty set: an average over nothing is
    undefined, and reporting zero would read as "the platform returns
    irrelevant results" when what happened is that it returned none. The same
    distinction :func:`~schemas.indexing.IndexMetricsRead.average_chunks_per_document`
    draws.
    """
    values = [float(score) for score in scores]
    if not values:
        return None
    return round_score(sum(values) / len(values))


def top_score(scores: Sequence[float]) -> float | None:
    """The best relevance in a result set, or ``None`` when it is empty.

    Reported alongside the average because they answer different questions: the
    top score says whether the corpus contains anything relevant at all, and the
    average says whether the *page* is worth reading.
    """
    if not scores:
        return None
    return round_score(max(scores))


__all__ = [
    "FAILURE_MESSAGES",
    "MIN_QUERY_LENGTH",
    "QUERY_FINGERPRINT_LENGTH",
    "SCORE_PRECISION",
    "SearchFailureCode",
    "average_score",
    "failure_message",
    "is_searchable_query",
    "loggable_query",
    "normalize_query",
    "query_fingerprint",
    "round_score",
    "top_score",
]
