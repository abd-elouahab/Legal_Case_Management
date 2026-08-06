"""Unit tests for :mod:`services.embedding`.

The real model is never loaded here: ``BAAI/bge-m3`` is roughly two gigabytes and
is fetched from the network on first use, so a suite that loaded it would only run
on machines that had already downloaded it. What is tested is everything *around*
the model — the seam's contract, the lazy load, the failure translation, the
shape checks, and the process-wide sharing — because those are the parts this
platform wrote and the parts a replacement backend has to honour.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.indexing import IndexFailureCode
from services.embedding import (
    EMBEDDER_FACTORIES,
    EmbedderUnavailableError,
    EmbeddingBatch,
    EmbeddingError,
    SentenceTransformerEmbedder,
    available_embedders,
    get_embedder,
    reset_embedder_cache,
)


class StubModel:
    """A stand-in for a loaded ``SentenceTransformer``."""

    def __init__(self, width: int = 4, raises: Exception | None = None) -> None:
        self.width = width
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        self.calls.append({"texts": texts, **kwargs})
        if self.raises is not None:
            raise self.raises
        return [[float(index)] * self.width for index, _ in enumerate(texts)]


@pytest.fixture(autouse=True)
def _clear_shared_embedder() -> Any:
    """Keep the process-wide cache from leaking between tests."""
    reset_embedder_cache()
    yield
    reset_embedder_cache()


class TestConfiguration:
    def test_the_model_and_width_come_from_configuration(self) -> None:
        # Read from configuration rather than probed, so the Qdrant collection
        # can be created before the model is ever loaded.
        embedder = SentenceTransformerEmbedder(model="acme/tiny", dimensions=16)
        assert embedder.model == "acme/tiny"
        assert embedder.dimensions == 16

    def test_the_defaults_are_the_platform_s_configured_model(self) -> None:
        from core.config import settings

        embedder = SentenceTransformerEmbedder()
        assert embedder.model == settings.EMBEDDING_MODEL
        assert embedder.dimensions == settings.EMBEDDING_DIMENSIONS

    def test_the_configured_model_is_the_multilingual_one_the_architecture_names(
        self,
    ) -> None:
        # `ai-architecture.md` names BAAI/bge-m3 for its Arabic and French
        # retrieval quality, and requires the *same* model for indexing and for
        # future query embedding.
        from core.config import settings

        assert settings.EMBEDDING_MODEL == "BAAI/bge-m3"
        assert settings.EMBEDDING_DIMENSIONS == 1024


class TestLoading:
    def test_the_model_is_not_loaded_until_it_is_used(self) -> None:
        # Two gigabytes must not be pulled in at import time or per request.
        embedder = SentenceTransformerEmbedder()
        assert embedder._model is None

    def test_the_model_is_loaded_once_and_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Loading two gigabytes per document would make indexing unusable, so
        # the real `_load` is exercised here rather than replaced — with a fake
        # `sentence_transformers` module standing in for the library.
        import sys
        import types

        constructions = 0

        def construct(name: str, device: str | None = None) -> StubModel:
            nonlocal constructions
            constructions += 1
            return StubModel(width=4)

        fake = types.ModuleType("sentence_transformers")
        fake.SentenceTransformer = construct  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

        embedder = SentenceTransformerEmbedder(dimensions=4)
        embedder.embed(["one"])
        embedder.embed(["two"])
        embedder.embed(["three"])

        assert constructions == 1

    def test_an_empty_batch_does_not_load_the_model(self) -> None:
        # Nothing to embed is not a reason to pull in a model.
        embedder = SentenceTransformerEmbedder(dimensions=4)
        batch = embedder.embed([])
        assert batch.vectors == []
        assert embedder._model is None

    def test_an_uninstallable_library_is_reported_as_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def refuse(name: str, *args: object, **kwargs: object) -> object:
            if name == "sentence_transformers":
                raise ImportError("no module named sentence_transformers")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", refuse)

        embedder = SentenceTransformerEmbedder()
        assert not embedder.is_available()
        with pytest.raises(EmbedderUnavailableError) as failure:
            embedder.embed(["anything"])
        assert failure.value.code is IndexFailureCode.EMBEDDING_FAILURE


class TestEncoding:
    def test_vectors_are_returned_in_input_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Order is the contract with the service: vectors are paired back with
        # their chunks by position, so a reordering would attach every passage's
        # vector to a different passage.
        embedder = SentenceTransformerEmbedder(dimensions=4)
        monkeypatch.setattr(embedder, "_load", lambda: StubModel(width=4))

        batch = embedder.embed(["a", "b", "c"])
        assert [vector[0] for vector in batch.vectors] == [0.0, 1.0, 2.0]

    def test_encoding_asks_for_normalised_vectors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unit length, so Qdrant's cosine distance is a dot product and vectors
        # written by different runs stay comparable.
        model = StubModel(width=4)
        embedder = SentenceTransformerEmbedder(dimensions=4)
        monkeypatch.setattr(embedder, "_load", lambda: model)

        embedder.embed(["one"])
        assert model.calls[0]["normalize_embeddings"] is True

    def test_encoding_never_shows_a_progress_bar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A background worker writing to stdout would corrupt structured logs.
        model = StubModel(width=4)
        embedder = SentenceTransformerEmbedder(dimensions=4)
        monkeypatch.setattr(embedder, "_load", lambda: model)

        embedder.embed(["one"])
        assert model.calls[0]["show_progress_bar"] is False

    def test_the_configured_batch_size_is_passed_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = StubModel(width=4)
        embedder = SentenceTransformerEmbedder(dimensions=4, batch_size=7)
        monkeypatch.setattr(embedder, "_load", lambda: model)

        embedder.embed(["one", "two"])
        assert model.calls[0]["batch_size"] == 7

    def test_the_model_identity_travels_with_the_vectors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Configuration is current; an index is historical. Changing models
        # requires re-indexing, and that comparison needs the model recorded.
        embedder = SentenceTransformerEmbedder(model="acme/tiny", dimensions=4)
        monkeypatch.setattr(embedder, "_load", lambda: StubModel(width=4))

        batch = embedder.embed(["one"])
        assert batch.model == "acme/tiny"
        assert batch.dimensions == 4
        assert len(batch) == 1


class TestShapeChecks:
    def test_a_width_mismatch_fails_once_rather_than_per_point(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Far better than letting Qdrant reject every point individually with a
        # message about vector shapes.
        embedder = SentenceTransformerEmbedder(dimensions=1024)
        monkeypatch.setattr(embedder, "_load", lambda: StubModel(width=4))

        with pytest.raises(EmbeddingError) as failure:
            embedder.embed(["one"])
        assert "1024" in str(failure.value)
        assert failure.value.code is IndexFailureCode.EMBEDDING_FAILURE

    def test_a_wrong_count_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Miscounting:
            def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
                return [[0.0] * 4]

        embedder = SentenceTransformerEmbedder(dimensions=4)
        monkeypatch.setattr(embedder, "_load", lambda: Miscounting())

        with pytest.raises(EmbeddingError) as failure:
            embedder.embed(["one", "two"])
        assert "2 passages" in str(failure.value)


class TestFailureTranslation:
    def test_a_failure_message_does_not_quote_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The library's message can echo the text it was encoding, so it must not
        # travel out of this module.
        embedder = SentenceTransformerEmbedder(dimensions=4)
        monkeypatch.setattr(
            embedder,
            "_load",
            lambda: StubModel(raises=RuntimeError("failed on 'Contrat de bail'")),
        )

        with pytest.raises(EmbeddingError) as failure:
            embedder.embed(["Contrat de bail"])
        assert "Contrat de bail" not in str(failure.value)

    def test_an_embedding_error_carries_a_machine_readable_code(self) -> None:
        assert EmbeddingError("boom").code is IndexFailureCode.EMBEDDING_FAILURE


class TestResolution:
    def test_the_default_backend_is_sentence_transformers(self) -> None:
        assert isinstance(get_embedder(), SentenceTransformerEmbedder)

    def test_an_unknown_identifier_falls_back_rather_than_failing(self) -> None:
        assert isinstance(get_embedder("not-a-backend"), SentenceTransformerEmbedder)

    def test_the_embedder_is_shared_across_the_process(self) -> None:
        # Unlike the OCR engine, which is a thin wrapper around a subprocess: an
        # embedder owns a large in-memory model, and a second one would double
        # the platform's memory for nothing.
        assert get_embedder() is get_embedder()

    def test_the_cache_can_be_cleared(self) -> None:
        first = get_embedder()
        reset_embedder_cache()
        assert get_embedder() is not first

    def test_the_registry_is_the_extension_point(self) -> None:
        # The spec's "multiple embedding models" enhancement: one class plus one
        # entry here.
        assert available_embedders() == sorted(EMBEDDER_FACTORIES)
        assert SentenceTransformerEmbedder.name in EMBEDDER_FACTORIES


class TestBatchValue:
    def test_a_batch_reports_its_size(self) -> None:
        batch = EmbeddingBatch(vectors=[[0.0], [1.0]], model="m", dimensions=1)
        assert len(batch) == 2

    def test_a_batch_is_immutable(self) -> None:
        batch = EmbeddingBatch(vectors=[], model="m", dimensions=1)
        with pytest.raises(AttributeError):
            batch.model = "other"  # type: ignore[misc]
