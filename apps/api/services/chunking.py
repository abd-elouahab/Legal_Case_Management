"""The text-chunking boundary.

``10-document-indexing.md``: *"Use mature libraries […] LangChain Text Splitters
(or equivalent)"* and *"Do not implement custom embedding or chunking
algorithms"*. This module is that boundary, and it is the **only** place in the
platform that imports a text splitter.

The shape mirrors :mod:`services.ocr_engine`, which is the seam this codebase
already uses for a replaceable third-party capability:

* :class:`Chunker` is the protocol :mod:`services.indexing` depends on — a name,
  the two settings that decide what a chunk means, and one ``split`` call;
* :class:`RecursiveCharacterChunker` is the implementation, wrapping LangChain's
  ``RecursiveCharacterTextSplitter``;
* :func:`get_chunker` resolves the configured strategy by identifier, so a
  second one (sentence-aware, layout-aware, token-based) is one class plus one
  registry entry.

**Pages are split, never the joined document.** The spec requires that a chunk
reference the *page* it came from, and OCR stores the text one row per page
precisely so that boundary survives. Concatenating the pages and splitting the
result would produce chunks straddling two pages with no honest answer to "which
page is this?" — so each page is split on its own, and the chunk numbering runs
across the document in reading order.

The library's own failures are translated here into a
:class:`ChunkingError` carrying an :class:`~core.indexing.IndexFailureCode`, so
the service above records a cause without knowing what a splitter is — and so a
library message, which can echo the text it was splitting, never leaves this
module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

import structlog

from core.config import settings
from core.indexing import (
    IndexFailureCode,
    detect_language,
    is_indexable_chunk,
    normalize_chunk_text,
)

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ChunkingError(Exception):
    """The text could not be divided into passages.

    Deliberately **not** an :class:`~core.exceptions.AppException`: chunking runs
    in a background worker with no request behind it, and the failure's
    destination is the run's ``error_code`` column, not a status line. The
    service translates it — exactly as it does an
    :class:`~services.ocr_engine.OcrEngineError`.
    """

    #: The cause, recorded on the index and used to group failures in the
    #: monitoring view.
    code: IndexFailureCode = IndexFailureCode.CHUNKING_FAILURE

    def __init__(self, message: str, *, code: IndexFailureCode | None = None) -> None:
        self.code = code or self.code
        super().__init__(message)


class ChunkerUnavailableError(ChunkingError):
    """The splitter library is not installed in this deployment.

    An operational fault rather than a fault of the document: the same text will
    chunk correctly once ``langchain-text-splitters`` is on the host. It is still
    recorded as a failed run, because the spec requires a failure to update the
    status — and the run stays retryable, which is exactly the right recovery.
    """


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TextChunk:
    """One passage, and where in the document it came from.

    Frozen, because a chunk's text and its provenance are fixed the moment it is
    cut: everything downstream — the embedding, the vector id, the payload a
    future search reads — is derived from exactly these values, and letting them
    be edited in flight is how a vector ends up describing a passage that is not
    the one it was built from.
    """

    #: 1-based page of the document this text was taken from.
    page_number: int
    #: 0-based position within the whole document, in reading order.
    chunk_number: int
    #: The passage itself, normalised (see
    #: :func:`~core.indexing.normalize_chunk_text`).
    text: str
    #: Best-effort language of this passage. Per chunk rather than per document
    #: because a Moroccan filing routinely carries an Arabic body and a French
    #: annex, and a single document-level label would mislabel one of them.
    language: str

    @property
    def character_count(self) -> int:
        """How long this passage is."""
        return len(self.text)


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class Chunker(Protocol):
    """What the indexing service requires of a splitter.

    Four members, and none of them mentions a document, a database row, a case,
    or a request — a chunker is handed pages of text and returns passages. That
    narrowness is what makes the seam real: a replacement has nothing to
    reimplement beyond splitting itself.
    """

    @property
    def name(self) -> str:
        """Stable identifier for the strategy ("recursive-character")."""
        ...

    @property
    def chunk_size(self) -> int:
        """Target passage length, in characters. Recorded on every run."""
        ...

    @property
    def chunk_overlap(self) -> int:
        """Characters each passage repeats from its predecessor."""
        ...

    def is_available(self) -> bool:
        """Whether the splitter can run here, right now.

        Probed rather than assumed, so a missing library is reported as an
        actionable failed run instead of surfacing as a stack trace on the first
        completed extraction.
        """
        ...

    def split(self, pages: Sequence[str]) -> list[TextChunk]:
        """Divide a document's pages into passages, in reading order.

        Args:
            pages: the document's text, one entry per page, in reading order —
                exactly the shape :class:`~models.ocr.OcrPage` rows are read in.

        Raises:
            ChunkingError: the text could not be split.
        """
        ...


# --------------------------------------------------------------------------- #
# LangChain recursive character splitting
# --------------------------------------------------------------------------- #


class RecursiveCharacterChunker:
    """LangChain's ``RecursiveCharacterTextSplitter``, configured for legal prose.

    Recursive character splitting is the right default here, and the reason is
    the spec's first chunking requirement — *"preserve semantic meaning"*. The
    splitter tries a list of separators **in order of how much structure they
    carry**, falling back only when a passage is still too long: paragraphs
    first, then lines, then sentences, then words, then characters. A fixed-width
    split would cut mid-sentence on every boundary; this one only does so when a
    single word exceeds the chunk size.

    The separator list is extended beyond the library's Latin-centric default
    with the **Arabic full stop (U+06D4), Arabic comma (U+060C), and Arabic
    question mark (U+061F)**, and with the ideographic and French spacing forms.
    Without them an Arabic page has no sentence separator at all and degrades
    straight to word splitting, which is precisely the language the platform
    exists to serve.

    ``length_function`` is left as ``len`` — characters, not tokens. Tokens would
    be a better unit for a model's context window, but counting them needs the
    model's tokenizer loaded, which would couple the chunker to the embedder and
    make chunk boundaries change when the model does. Characters are stable,
    cheap, and what ``INDEX_CHUNK_SIZE`` is documented in.
    """

    #: The identifier recorded for this strategy.
    name = "recursive-character"

    #: Separators tried in order, most structural first.
    #:
    #: Kept as class data rather than inline so a test can assert the Arabic
    #: punctuation is present — the one part of this list that is a deliberate
    #: platform decision rather than the library's default.
    SEPARATORS: tuple[str, ...] = (
        "\n\n",  # paragraph
        "\n",  # line
        # RUF001 flags these three as "ambiguous" characters — it is warning that
        # U+06D4 resembles a hyphen and U+060C a comma. That is precisely the
        # point: they *are* Arabic sentence punctuation, and this platform's
        # documents are written with them. Suppressed per line rather than
        # globally, so the check keeps working everywhere else.
        "۔",  # noqa: RUF001 - Arabic full stop (U+06D4)
        "؟",  # Arabic question mark (U+061F)
        "،",  # Arabic comma (U+060C)
        ". ",  # sentence (Latin)
        "! ",
        "? ",
        "; ",
        ", ",
        " ",  # word
        "",  # character, the last resort
    )

    def __init__(self, *, chunk_size: int | None = None, chunk_overlap: int | None = None) -> None:
        self._chunk_size = chunk_size or settings.INDEX_CHUNK_SIZE
        self._chunk_overlap = (
            chunk_overlap if chunk_overlap is not None else settings.INDEX_CHUNK_OVERLAP
        )
        # Built lazily and cached: importing the library costs nothing much, but
        # constructing a splitter per document would still be pointless work, and
        # a chunker instance is per run.
        self._splitter: object | None = None

    # ------------------------------------------------------------ identity #

    @property
    def chunk_size(self) -> int:
        """Target passage length, in characters."""
        return self._chunk_size

    @property
    def chunk_overlap(self) -> int:
        """Characters each passage repeats from its predecessor.

        The spec requires overlap to be configurable, and the reason it exists at
        all is that a passage answering a question can straddle a boundary: with
        no overlap, a definition split across two chunks is retrievable from
        neither.
        """
        return self._chunk_overlap

    def is_available(self) -> bool:
        """Whether ``langchain-text-splitters`` can be imported."""
        try:
            self._build()
        except ChunkingError:
            return False
        return True

    # ------------------------------------------------------------ splitting #

    def split(self, pages: Sequence[str]) -> list[TextChunk]:
        """Divide each page into passages, numbering them across the document.

        Page order and page boundaries are preserved by construction: pages are
        processed in the order given, each is split on its own, and every chunk
        records the page it came from. A page that yields nothing indexable —
        a blank separator sheet, a page holding only a photograph — contributes
        no chunk and **does not renumber the pages after it**, because the page
        number travels on the chunk rather than being inferred from a position.

        Raises:
            ChunkerUnavailableError: the splitter library is not installed.
            ChunkingError: the splitter itself failed.
        """
        splitter = self._build()

        chunks: list[TextChunk] = []
        chunk_number = 0

        for index, page_text in enumerate(pages, start=1):
            normalized = normalize_chunk_text(page_text)
            if not normalized:
                continue

            for piece in self._split_text(splitter, normalized):
                text = normalize_chunk_text(piece)
                if not is_indexable_chunk(text):
                    # A page number, a footer, or a fragment of a table rule.
                    # Dropped rather than embedded: a vector for it can only ever
                    # be noise in a search result.
                    continue

                chunks.append(
                    TextChunk(
                        page_number=index,
                        chunk_number=chunk_number,
                        text=text,
                        language=detect_language(text),
                    )
                )
                chunk_number += 1

        return chunks

    # ------------------------------------------------------------- helpers #

    def _build(self) -> object:
        """Construct (and cache) the underlying splitter.

        Raises:
            ChunkerUnavailableError: the library is not installed.
            ChunkingError: the configuration was rejected.
        """
        if self._splitter is not None:
            return self._splitter

        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter
        except ImportError as exc:
            # Logged without the exception's own message: an import error names a
            # file path on the host, which belongs in a traceback rather than in
            # a structured log an operator scrapes.
            logger.error("chunker_unavailable", chunker=self.name)
            raise ChunkerUnavailableError(
                "The text splitter library is not installed."
            ) from exc

        try:
            self._splitter = RecursiveCharacterTextSplitter(
                chunk_size=self._chunk_size,
                chunk_overlap=self._chunk_overlap,
                separators=list(self.SEPARATORS),
                # `"end"`, not `True`. Both keep the separator, but `True` moves
                # it to the *front* of the following passage — so a chunk ends
                # "Article 2" and the next begins ". Le loyer", which reads as a
                # fragment in a search result and puts a stray full stop at the
                # head of an embedded passage. `"end"` leaves the sentence with
                # the punctuation that closes it, and the payload a future search
                # returns stays quotable.
                keep_separator="end",
                length_function=len,
            )
        except Exception as exc:
            logger.error(
                "chunker_configuration_rejected",
                chunker=self.name,
                chunk_size=self._chunk_size,
                chunk_overlap=self._chunk_overlap,
            )
            raise ChunkingError("The text splitter could not be configured.") from exc

        return self._splitter

    def _split_text(self, splitter: object, text: str) -> list[str]:
        """Run the library's splitter, translating whatever it raises.

        Raises:
            ChunkingError: the splitter failed on this text.
        """
        try:
            pieces = splitter.split_text(text)  # type: ignore[attr-defined]
        except Exception as exc:
            # The library's message can echo the text it was splitting, so it is
            # deliberately not carried into the exception or the log.
            logger.error("chunking_failed", chunker=self.name, character_count=len(text))
            raise ChunkingError("The extracted text could not be divided.") from exc

        return [str(piece) for piece in pieces]


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every chunking strategy this build can be configured to use.
#:
#: Adding one is a class implementing :class:`Chunker` plus an entry here — the
#: extensibility the spec asks for, in the same shape as
#: :data:`~services.ocr_engine.ENGINE_FACTORIES`.
CHUNKER_FACTORIES: Mapping[str, type[RecursiveCharacterChunker]] = MappingProxyType(
    {RecursiveCharacterChunker.name: RecursiveCharacterChunker}
)


def available_chunkers() -> list[str]:
    """Every chunker identifier this build can be configured to use."""
    return sorted(CHUNKER_FACTORIES)


def get_chunker(identifier: str | None = None) -> Chunker:
    """Build the configured chunker.

    Falls back to recursive character splitting for an unrecognised identifier
    rather than failing startup: a strategy name is deployment configuration, and
    an unreadable one should degrade to the documented default with a warning,
    not take the API down. The fallback is logged, so the misconfiguration is
    visible.
    """
    wanted = (identifier or settings.INDEX_CHUNKER).strip().lower()

    factory = CHUNKER_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "chunker_unknown", requested=wanted, fallback=RecursiveCharacterChunker.name
        )
        factory = RecursiveCharacterChunker

    return factory()


__all__ = [
    "CHUNKER_FACTORIES",
    "Chunker",
    "ChunkerUnavailableError",
    "ChunkingError",
    "RecursiveCharacterChunker",
    "TextChunk",
    "available_chunkers",
    "get_chunker",
]
