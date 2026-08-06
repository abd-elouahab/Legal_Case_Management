"""The embedding-model boundary.

``10-document-indexing.md``: *"Use mature libraries […] sentence-transformers"*
and *"Do not implement custom embedding or chunking algorithms"*.
`ai-architecture.md` names the model: **BAAI/bge-m3**, chosen for multilingual
retrieval quality with Arabic, French, and English support, and required to be
*"the same embedding model […] for document indexing and user queries"*.

This module is that boundary, and it is the **only** place in the platform that
imports ``sentence_transformers`` or touches a model file. The shape mirrors
:mod:`services.ocr_engine` and :mod:`services.chunking`:

* :class:`Embedder` is the protocol :mod:`services.indexing` depends on — a
  model name, a vector width, an availability probe, and one ``embed`` call;
* :class:`SentenceTransformerEmbedder` is the implementation;
* :func:`get_embedder` resolves the configured backend by identifier, so a
  second one (an ONNX runtime, a hosted embedding API) is one class plus one
  registry entry — the spec's "multiple embedding models" extensibility.

Three properties are load-bearing and are asserted by tests rather than assumed:

* **Deterministic.** The spec requires it. The model is loaded in evaluation
  mode with no sampling anywhere in the path, so the same text embeds to the same
  vector — which is what makes a re-index of unchanged text a no-op rather than a
  silent drift, and what makes a search's query vector comparable with vectors
  written months earlier.
* **Multilingual, covering Arabic and French.** bge-m3 is trained across 100+
  languages in one shared space, so an Arabic passage and its French translation
  land near each other. A per-language model would need a language decision
  *before* retrieval and would make cross-language search impossible.
* **Normalised to unit length.** Cosine similarity is then a dot product, which
  is what the Qdrant collection is created for, and vectors from two different
  runs stay directly comparable.

**The model is loaded lazily and once.** bge-m3 is roughly 2 GB; loading it at
import time would make the API's startup depend on a model download, and loading
it per document would make indexing unusable. It is loaded on first use and
cached on the instance, and the worker holds one embedder for the whole run.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

import structlog

from core.config import settings
from core.indexing import IndexFailureCode

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class EmbeddingError(Exception):
    """Embeddings could not be produced.

    Deliberately **not** an :class:`~core.exceptions.AppException`: embedding
    runs in a background worker with no request behind it, and the failure's
    destination is the run's ``error_code`` column, not a status line.
    """

    #: The cause, recorded on the index and used to group failures in the
    #: monitoring view.
    code: IndexFailureCode = IndexFailureCode.EMBEDDING_FAILURE

    def __init__(self, message: str, *, code: IndexFailureCode | None = None) -> None:
        self.code = code or self.code
        super().__init__(message)


class EmbedderUnavailableError(EmbeddingError):
    """The embedding library or the model itself is not available here.

    An operational fault rather than a fault of the document: the same text will
    embed correctly once ``sentence-transformers`` is installed and the model has
    been fetched. It is still recorded as a failed run, because the spec requires
    a failure to update the status — and the run stays retryable, which is
    exactly the right recovery, and the same posture
    :class:`~services.ocr_engine.OcrEngineUnavailableError` takes for a missing
    Tesseract.
    """


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """The vectors produced for one batch of passages, and what produced them.

    The model identity travels with the vectors rather than being read from
    configuration afterwards, because configuration is current and an index is
    historical: `ai-architecture.md` states that changing embedding models
    requires re-indexing everything, and that comparison is only possible if each
    index records the model it was actually built with.
    """

    vectors: list[list[float]]
    model: str
    dimensions: int

    def __len__(self) -> int:
        """How many vectors this batch holds."""
        return len(self.vectors)


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class Embedder(Protocol):
    """What the indexing service requires of an embedding model.

    Four members, and none of them mentions a document, a chunk's provenance, a
    database row, or a request — an embedder is handed strings and returns
    vectors. That narrowness is what makes the seam real, and it is what will let
    Semantic Search embed a *query* through this same interface later without
    indexing knowing anything about it.
    """

    @property
    def model(self) -> str:
        """Stable identifier of the model ("BAAI/bge-m3")."""
        ...

    @property
    def dimensions(self) -> int:
        """Width of the vectors this model produces.

        Known before any text is embedded, because the Qdrant collection has to
        be created for a fixed width and cannot accept another.
        """
        ...

    def is_available(self) -> bool:
        """Whether the model can run here, right now."""
        ...

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed passages, returning one vector per input in the same order.

        Order is part of the contract: the caller pairs the vectors back with the
        chunks they came from by position, and a reordering would attach every
        passage's vector to a different passage.

        Raises:
            EmbeddingError: the vectors could not be produced.
        """
        ...


# --------------------------------------------------------------------------- #
# sentence-transformers
# --------------------------------------------------------------------------- #


class SentenceTransformerEmbedder:
    """BAAI/bge-m3 via ``sentence-transformers``.

    The model is a process-wide resource: it is large, its load is slow, and it
    is stateless once loaded. It is therefore cached on the instance behind a
    lock — two worker threads reaching first use simultaneously must not both
    load two gigabytes.
    """

    #: The identifier recorded for this backend.
    name = "sentence-transformers"

    def __init__(
        self,
        *,
        model: str | None = None,
        dimensions: int | None = None,
        batch_size: int | None = None,
        device: str | None = None,
    ) -> None:
        self._model_name = model or settings.EMBEDDING_MODEL
        self._dimensions = dimensions or settings.EMBEDDING_DIMENSIONS
        self._batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE
        self._device = device if device is not None else settings.EMBEDDING_DEVICE
        self._model: object | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ identity #

    @property
    def model(self) -> str:
        """The model identifier recorded on every index this produces."""
        return self._model_name

    @property
    def dimensions(self) -> int:
        """Configured vector width.

        Read from configuration rather than from the loaded model so the Qdrant
        collection can be created before the model is ever loaded — and so a
        deployment that has not downloaded the model still reports a coherent
        collection shape. :meth:`embed` verifies the two agree and fails loudly
        if they do not, because a mismatch would otherwise surface as vectors
        Qdrant rejects one at a time.
        """
        return self._dimensions

    def is_available(self) -> bool:
        """Whether the library is installed and the model can be loaded.

        Probed rather than assumed, so a missing model is reported as an
        actionable failed run and a ``false`` on the monitoring endpoint, instead
        of surfacing as a stack trace on the first completed extraction.
        """
        try:
            self._load()
        except EmbeddingError:
            return False
        return True

    # ------------------------------------------------------------ embedding #

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed passages in batches, preserving input order.

        Batching is the spec's "batch insertion" requirement on the embedding
        side: a 100-page document is several hundred passages, and encoding them
        one at a time pays the model's fixed per-call cost for each. The library
        batches internally when handed a list, and ``EMBEDDING_BATCH_SIZE``
        bounds how much is resident at once.

        Raises:
            EmbedderUnavailableError: the library or the model is unavailable.
            EmbeddingError: encoding failed, or produced the wrong shape.
        """
        if not texts:
            return EmbeddingBatch(vectors=[], model=self._model_name, dimensions=self._dimensions)

        model = self._load()

        try:
            encoded = model.encode(  # type: ignore[attr-defined]
                list(texts),
                batch_size=self._batch_size,
                # Unit-length vectors, so Qdrant's cosine distance is a dot
                # product and vectors written by different runs stay comparable.
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            # The library's message can echo the text it was encoding, so it is
            # deliberately not carried into the exception or the log.
            logger.error(
                "embedding_failed",
                model=self._model_name,
                batch=len(texts),
            )
            raise EmbeddingError("The embedding model could not process this text.") from exc

        vectors = [[float(value) for value in vector] for vector in encoded]

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"The embedding model returned {len(vectors)} vectors for {len(texts)} passages."
            )

        width = len(vectors[0])
        if width != self._dimensions:
            # Configuration and the model disagree. Failing here, once, is far
            # better than letting Qdrant reject every point individually with a
            # message about vector shapes.
            logger.error(
                "embedding_dimension_mismatch",
                model=self._model_name,
                configured=self._dimensions,
                actual=width,
            )
            raise EmbeddingError(
                f"The embedding model produces {width}-dimensional vectors, "
                f"but this deployment is configured for {self._dimensions}."
            )

        return EmbeddingBatch(vectors=vectors, model=self._model_name, dimensions=width)

    # ------------------------------------------------------------- helpers #

    def _load(self) -> object:
        """Load (and cache) the model.

        Raises:
            EmbedderUnavailableError: the library is not installed, or the model
                could not be loaded.
        """
        if self._model is not None:
            return self._model

        with self._lock:
            # Re-checked inside the lock: two threads can both pass the check
            # above, and loading two gigabytes twice is exactly what the lock is
            # here to prevent.
            if self._model is not None:
                return self._model

            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                logger.error("embedder_unavailable", reason="library_missing", model=self._model_name)
                raise EmbedderUnavailableError(
                    "The embedding library is not installed."
                ) from exc

            try:
                logger.info("embedding_model_loading", model=self._model_name, device=self._device)
                self._model = SentenceTransformer(self._model_name, device=self._device)
                logger.info("embedding_model_loaded", model=self._model_name)
            except Exception as exc:
                logger.error(
                    "embedder_unavailable", reason="load_failed", model=self._model_name
                )
                raise EmbedderUnavailableError(
                    "The embedding model could not be loaded."
                ) from exc

        return self._model


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every embedding backend this build can be configured to use.
#:
#: Adding one is a class implementing :class:`Embedder` plus an entry here — the
#: spec's "multiple embedding models" extensibility, in the same shape as
#: :data:`~services.ocr_engine.ENGINE_FACTORIES`.
EMBEDDER_FACTORIES: Mapping[str, type[SentenceTransformerEmbedder]] = MappingProxyType(
    {SentenceTransformerEmbedder.name: SentenceTransformerEmbedder}
)

#: The one embedder the process shares, built on first use.
#:
#: A module-level singleton because the model it holds is a process-wide
#: resource: one copy of a two-gigabyte model, shared by every worker thread, is
#: the whole point. Guarded by a lock for the same reason the model load is.
_shared: dict[str, Embedder] = {}
_shared_lock = threading.Lock()


def available_embedders() -> list[str]:
    """Every embedder identifier this build can be configured to use."""
    return sorted(EMBEDDER_FACTORIES)


def get_embedder(identifier: str | None = None) -> Embedder:
    """Return the configured embedder, shared across the process.

    Shared rather than constructed per call, unlike
    :func:`~services.ocr_engine.get_ocr_engine`: an OCR engine is a thin wrapper
    around a subprocess, while an embedder owns a large in-memory model, and
    building a second one would double the platform's memory for nothing.

    Falls back to the default backend for an unrecognised identifier rather than
    failing startup, with the fallback logged so the misconfiguration is visible.
    """
    wanted = (identifier or settings.EMBEDDING_BACKEND).strip().lower()

    factory = EMBEDDER_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "embedder_unknown",
            requested=wanted,
            fallback=SentenceTransformerEmbedder.name,
        )
        wanted = SentenceTransformerEmbedder.name
        factory = SentenceTransformerEmbedder

    cached = _shared.get(wanted)
    if cached is not None:
        return cached

    with _shared_lock:
        existing = _shared.get(wanted)
        if existing is not None:
            return existing
        built: Embedder = factory()
        _shared[wanted] = built
        return built


def reset_embedder_cache() -> None:
    """Discard the shared embedder.

    For tests, and for a deployment that reconfigures the model at runtime. Not
    called by the application: an embedder is stateless once loaded, so there is
    nothing to invalidate in normal operation.
    """
    with _shared_lock:
        _shared.clear()


__all__ = [
    "EMBEDDER_FACTORIES",
    "Embedder",
    "EmbedderUnavailableError",
    "EmbeddingBatch",
    "EmbeddingError",
    "SentenceTransformerEmbedder",
    "available_embedders",
    "get_embedder",
    "reset_embedder_cache",
]
