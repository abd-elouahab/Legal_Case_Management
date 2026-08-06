"""Document-indexing domain utilities.

Small, pure helpers shared by the indexing schemas, repository, service, chunker,
embedder, and vector store: the lifecycle policy, the failure vocabulary, the
chunk identity, and the language heuristic applied to every chunk.

They live here rather than inside a service method for the same reason
:mod:`core.cases`, :mod:`core.documents`, :mod:`core.timeline`, and
:mod:`core.ocr` exist — the same rules must apply however a run arrives
(scheduled by a completed extraction, or requested as a re-index), and they can
be unit-tested without a database, a request, a running Qdrant, or a downloaded
embedding model.

**Nothing here splits text or produces a vector.** Chunking lives behind
:mod:`services.chunking`, embedding behind :mod:`services.embedding`, and vector
persistence behind :mod:`services.vector_store` — the three seams
``10-document-indexing.md`` needs so that a different splitter, a different
embedding model, or a different vector database is one class rather than a
rewrite. This module holds only the decisions that are the *platform's* rather
than a library's.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from models.indexing import IndexStatus

#: Namespace for deriving a vector's identifier from what it *is*.
#:
#: A fixed UUID, so :func:`chunk_point_id` is a pure function of (document,
#: version, page, chunk) on every host and in every process. That is what makes
#: writing the same chunk twice an overwrite rather than a duplicate — see the
#: function's docstring, where the whole re-indexing guarantee is explained.
CHUNK_ID_NAMESPACE = uuid.UUID("6f3b1c8e-4a52-5d17-9b0e-2c7a8d4f1e30")

#: Longest failure message stored on an index. Messages are ours, not a
#: library's, so this is a guard against a future call site rather than against
#: user input.
MAX_ERROR_MESSAGE_LENGTH = 500

#: Smallest chunk worth embedding, in characters after trimming.
#:
#: A splitter's last fragment is routinely a page number, a stray footer, or a
#: single misrecognised glyph. Embedding those costs a vector each and pollutes
#: future retrieval with matches that carry no meaning, which is the opposite of
#: the spec's "preserve semantic meaning".
MIN_CHUNK_CHARS = 20


class IndexFailureCode(StrEnum):
    """Why a run ended without a usable index.

    Machine-readable, so a client can branch on the cause and the monitoring view
    can group failures, without either parsing a sentence. The members cover
    exactly the failures ``10-document-indexing.md`` lists under "Failure
    Handling", plus the two the platform can be at fault for.
    """

    #: The document has no completed extraction to index, or its text is empty.
    #: The spec's "invalid OCR output".
    INVALID_OCR_OUTPUT = "invalid_ocr_output"
    #: The text could not be split into anything worth embedding.
    CHUNKING_FAILURE = "chunking_failure"
    #: The embedding model failed, or is not installed in this deployment.
    EMBEDDING_FAILURE = "embedding_failure"
    #: Qdrant could not be reached, or refused the write.
    VECTOR_STORE_UNAVAILABLE = "vector_store_unavailable"
    #: The run exceeded ``INDEXING_TIMEOUT_SECONDS``.
    TIMEOUT = "timeout"
    #: Anything the branches above do not describe.
    UNKNOWN = "unknown"


#: Human-readable explanation per failure code.
#:
#: Kept here rather than raised with the exception so the message a user reads is
#: written once and cannot vary between call sites. Every one is deliberately
#: free of the document's *contents* — it says what went wrong with the indexing,
#: never what was being indexed.
FAILURE_MESSAGES: Mapping[IndexFailureCode, str] = MappingProxyType(
    {
        IndexFailureCode.INVALID_OCR_OUTPUT: (
            "There is no extracted text to index for this document version."
        ),
        IndexFailureCode.CHUNKING_FAILURE: (
            "The extracted text could not be divided into searchable passages."
        ),
        IndexFailureCode.EMBEDDING_FAILURE: (
            "The embedding service could not process this document."
        ),
        IndexFailureCode.VECTOR_STORE_UNAVAILABLE: (
            "The search index is unavailable. The document and its text are unaffected."
        ),
        IndexFailureCode.TIMEOUT: (
            "Indexing took too long and was stopped. Try again, or split the document."
        ),
        IndexFailureCode.UNKNOWN: "Indexing did not complete.",
    }
)


def failure_message(code: IndexFailureCode) -> str:
    """The message shown for a failure code.

    Falls back to the generic explanation for a code with no entry, so a future
    member added without a message still reads as a sentence rather than as an
    identifier.
    """
    return FAILURE_MESSAGES.get(code, FAILURE_MESSAGES[IndexFailureCode.UNKNOWN])


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

#: The legal moves between index states, declared once.
#:
#: Read-only (``MappingProxyType`` over ``frozenset``s) so the policy cannot be
#: widened by mutation at runtime, exactly as ``STATUS_TRANSITIONS`` in
#: :mod:`core.cases` and :mod:`core.ocr` are.
#:
#: The shape is a cycle rather than a chain: ``INDEXED`` and ``FAILED`` both
#: return to ``PENDING``, which is what a re-index *is* — the same run, queued
#: again. Neither returns directly to ``INDEXING``, because a run must be claimed
#: by a worker to enter it, and that claim is the platform's concurrency
#: guarantee (see :meth:`~repositories.indexing.IndexingRepository.claim`).
STATUS_TRANSITIONS: Mapping[IndexStatus, frozenset[IndexStatus]] = MappingProxyType(
    {
        IndexStatus.PENDING: frozenset({IndexStatus.INDEXING, IndexStatus.FAILED}),
        IndexStatus.INDEXING: frozenset({IndexStatus.INDEXED, IndexStatus.FAILED}),
        IndexStatus.INDEXED: frozenset({IndexStatus.PENDING}),
        IndexStatus.FAILED: frozenset({IndexStatus.PENDING}),
    }
)

#: States a run may be re-indexed from: the terminal ones.
#:
#: Derived from :data:`STATUS_TRANSITIONS` rather than listed again, so the two
#: cannot disagree — a state that can move to ``PENDING`` *is* a state a re-index
#: may start from, and that is the definition rather than a coincidence.
REINDEXABLE_STATUSES: frozenset[IndexStatus] = frozenset(
    status for status, targets in STATUS_TRANSITIONS.items() if IndexStatus.PENDING in targets
)


def can_transition(current: IndexStatus, target: IndexStatus) -> bool:
    """Whether a run may move from ``current`` to ``target``.

    A move to the state a run is already in is **not** a transition and is
    refused: "start indexing" arriving twice is a concurrency bug, not a no-op,
    and treating it as one is what would let two workers believe they own the
    same run.
    """
    return target in STATUS_TRANSITIONS.get(current, frozenset())


def can_reindex(current: IndexStatus) -> bool:
    """Whether a run in this state may be re-indexed."""
    return current in REINDEXABLE_STATUSES


# --------------------------------------------------------------------------- #
# Chunk identity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChunkLocation:
    """Where one chunk came from.

    The five references ``10-document-indexing.md`` requires every chunk to
    carry — document, document version, case, page, and chunk number — as one
    value rather than five parameters threaded through the pipeline. Frozen,
    because a chunk's provenance is fixed the moment it is cut.
    """

    document_id: uuid.UUID
    document_version: int
    case_id: uuid.UUID
    #: 1-based page of the document this chunk's text was taken from. Preserved
    #: end to end, because it is what a future citation points at.
    page_number: int
    #: 0-based position of this chunk **within the whole document**, in reading
    #: order. Document-wide rather than per page, so ordering the chunks of a
    #: document is a sort on one field.
    chunk_number: int

    @property
    def point_id(self) -> uuid.UUID:
        """The vector identifier this chunk always maps to."""
        return chunk_point_id(
            self.document_id, self.document_version, self.page_number, self.chunk_number
        )


def chunk_point_id(
    document_id: uuid.UUID, document_version: int, page_number: int, chunk_number: int
) -> uuid.UUID:
    """Derive a vector's identifier from the chunk's position, not from chance.

    **This is the whole of the "avoid duplicate vectors" requirement.** A random
    identifier per write would mean that re-indexing a document adds a second
    copy of every passage, and that the only way to avoid it is to remember to
    delete the old ones first — a rule that holds exactly as long as nobody
    forgets it. A *derived* identifier makes the same write an overwrite: Qdrant
    upserts by point id, so indexing the same (document, version, page, chunk)
    twice produces one point whichever order the two runs happen in.

    Deletion of the previous version's vectors still happens (see
    :meth:`~services.vector_store.VectorStore.delete_document_version`), because
    a re-index that produces *fewer* chunks would otherwise leave the tail of the
    old one behind. The two mechanisms cover different halves: the derived id
    makes a repeat write idempotent, the delete makes a shorter rewrite complete.

    UUID5 over a fixed namespace rather than a hash truncated into a UUID: it is
    the standard construction for "a name that is a UUID", it is stable across
    Python versions and hosts, and Qdrant accepts a UUID point id natively.
    """
    name = f"{document_id}:{document_version}:{page_number}:{chunk_number}"
    return uuid.uuid5(CHUNK_ID_NAMESPACE, name)


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #

#: Arabic script, including the Arabic Supplement and Presentation Forms blocks
#: an OCR engine can emit.
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

#: Latin letters carrying the diacritics French uses. English has none of these,
#: which is exactly what makes them the discriminator between the two.
_FRENCH_DIACRITICS = re.compile(r"[àâäçéèêëîïôöùûüÿœæÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸŒÆ]")

_LATIN = re.compile(r"[A-Za-z]")

#: What the language heuristic can answer. ISO 639-1 codes, matching the
#: platform's locale identifiers rather than Tesseract's three-letter codes.
LANGUAGE_ARABIC = "ar"
LANGUAGE_FRENCH = "fr"
LANGUAGE_ENGLISH = "en"
LANGUAGE_UNKNOWN = "und"

#: Share of letters that must be Arabic for a chunk to be called Arabic. Well
#: below half, because a legal filing in Arabic quotes French party names, case
#: references, and article numbers in Latin script throughout.
_ARABIC_THRESHOLD = 0.25


def detect_language(text: str) -> str:
    """Best-effort language of a passage, from its script.

    The spec requires a ``language`` in every vector's metadata so that a future
    search can filter by it, and this platform's languages are Arabic and French
    (with English as the third OCR pack). Script tells the three apart cheaply
    and deterministically:

    * **Arabic** is a different script, so a proportion of Arabic letters is
      conclusive;
    * **French vs. English** share the Latin alphabet, so the discriminator is
      the diacritics French has and English does not;
    * anything else — a page of digits, a table of case numbers, a chunk of
      punctuation — is ``und`` rather than a guess. A wrong label would silently
      exclude a passage from a filtered search, which is worse than no label.

    Deliberately a heuristic and not a language-detection dependency: it runs on
    every chunk of every document, it needs no model to load, and its answer is
    a *filter hint*, not a fact anything depends on being right. The embedding
    model is multilingual, so retrieval quality does not rest on this.
    """
    if not text:
        return LANGUAGE_UNKNOWN

    normalized = unicodedata.normalize("NFC", text)

    arabic = len(_ARABIC.findall(normalized))
    latin = len(_LATIN.findall(normalized))
    letters = arabic + latin

    if letters == 0:
        return LANGUAGE_UNKNOWN

    if arabic / letters >= _ARABIC_THRESHOLD:
        return LANGUAGE_ARABIC

    if _FRENCH_DIACRITICS.search(normalized):
        return LANGUAGE_FRENCH

    return LANGUAGE_ENGLISH


def dominant_language(languages: Iterable[str]) -> str | None:
    """The most common language across a run's chunks, or ``None`` if none.

    ``und`` chunks are **excluded** rather than counted: a document whose pages
    are mostly numeric tables would otherwise be labelled "undetermined" while
    its actual prose is in French. If every chunk is undetermined the answer is
    ``None``, which is the honest form of "no language could be read".
    """
    counts: dict[str, int] = {}
    for language in languages:
        if language == LANGUAGE_UNKNOWN:
            continue
        counts[language] = counts.get(language, 0) + 1

    if not counts:
        return None

    # Ties broken alphabetically rather than by iteration order, so the answer is
    # reproducible for the same input.
    return max(sorted(counts), key=lambda language: counts[language])


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def normalize_chunk_text(value: str) -> str:
    """Trim a chunk to the text that is actually embedded and stored.

    The OCR pages this reads have already been normalised once
    (:func:`~core.ocr.normalize_extracted_text` handles NFC, line endings, and
    control characters), so this is only about the edges a splitter leaves:
    leading and trailing whitespace, and the blank lines a page break introduces
    mid-chunk. Collapsing them keeps the stored payload equal to what was
    embedded, which is what makes a future search result quotable.
    """
    if not value:
        return ""
    return "\n".join(line.rstrip() for line in value.strip().splitlines()).strip()


def is_indexable_chunk(value: str) -> bool:
    """Whether a fragment carries enough meaning to be worth a vector.

    See :data:`MIN_CHUNK_CHARS`. A fragment that is only whitespace, digits, and
    punctuation is rejected regardless of length: a chunk reading "— 14 —" is a
    page number, and a vector for it can only ever be noise in a search result.
    """
    trimmed = value.strip()
    if len(trimmed) < MIN_CHUNK_CHARS:
        return False
    return any(character.isalpha() for character in trimmed)


def normalize_error_message(value: str | None) -> str | None:
    """Collapse a failure message and truncate it to the stored ceiling."""
    if value is None:
        return None

    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    return collapsed[:MAX_ERROR_MESSAGE_LENGTH]


def success_rate(indexed: int, failed: int) -> float:
    """Share of finished runs that produced an index, as a percentage.

    Only *terminal* runs are counted, which is what makes the number meaningful:
    including queued and in-flight runs would make the success rate fall every
    time a document is uploaded and recover as it was indexed, which measures
    traffic rather than quality. No terminal runs at all reports ``0.0`` — there
    is nothing to be successful at yet. Identical in shape and reasoning to
    :func:`~core.ocr.success_rate`.
    """
    total = indexed + failed
    if total <= 0:
        return 0.0
    return round(indexed / total * 100, 2)
