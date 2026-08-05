"""Unit tests for :mod:`services.ocr_engine`.

Two halves, and they are tested differently on purpose:

* the **seam** — the protocol, the registry, the error vocabulary, the
  resource-management contract — is tested directly, because it is the thing the
  rest of the platform depends on and it must hold whatever engine is behind it;
* the **Tesseract adapter** is tested through its translation and deadline logic
  rather than by running a real recognition. Tesseract is a system binary with a
  language pack per language, so a test that required one would only run on
  machines that had it — and the interesting behaviour (a timeout, a missing
  binary, a corrupted PDF) is not producible on demand from a real install
  anyway.
"""

from __future__ import annotations

import time

import pytest

from core.ocr import OcrFailureCode
from services.ocr_engine import (
    ENGINE_FACTORIES,
    ExtractedPage,
    Extraction,
    OcrCorruptedDocumentError,
    OcrEngine,
    OcrEngineError,
    OcrEngineUnavailableError,
    OcrTimeoutError,
    OcrUnsupportedFormatError,
    TesseractOcrEngine,
    available_engines,
    get_ocr_engine,
)


class TestErrorVocabulary:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (OcrEngineError("x"), OcrFailureCode.ENGINE_FAILURE),
            (OcrEngineUnavailableError("x"), OcrFailureCode.ENGINE_FAILURE),
            (OcrUnsupportedFormatError("x"), OcrFailureCode.UNSUPPORTED_FORMAT),
            (OcrCorruptedDocumentError("x"), OcrFailureCode.CORRUPTED_DOCUMENT),
            (OcrTimeoutError("x"), OcrFailureCode.TIMEOUT),
        ],
    )
    def test_every_error_carries_a_failure_code(
        self, error: OcrEngineError, expected: OcrFailureCode
    ) -> None:
        assert error.code is expected

    def test_the_code_can_be_overridden_per_instance(self) -> None:
        error = OcrEngineError("x", code=OcrFailureCode.UNREADABLE_DOCUMENT)

        assert error.code is OcrFailureCode.UNREADABLE_DOCUMENT

    def test_an_engine_error_is_not_an_http_exception(self) -> None:
        from core.exceptions import AppException

        # Extraction runs in a background worker with no request behind it: the
        # failure's destination is a column, not a status line.
        assert not issubclass(OcrEngineError, AppException)


class TestExtraction:
    def test_it_counts_its_pages(self) -> None:
        extraction = Extraction(
            pages=[
                ExtractedPage(page_number=1, text="a"),
                ExtractedPage(page_number=2, text="b"),
            ]
        )

        assert extraction.page_count == 2

    def test_has_text_ignores_whitespace_only_pages(self) -> None:
        assert not Extraction(pages=[ExtractedPage(page_number=1, text="  \n ")]).has_text
        assert Extraction(pages=[ExtractedPage(page_number=1, text="a")]).has_text

    def test_an_empty_extraction_has_no_pages(self) -> None:
        assert Extraction().page_count == 0
        assert not Extraction().has_text

    def test_it_is_frozen(self) -> None:
        # A report, not a workspace: nothing should be able to edit a machine's
        # reading of a document on the way to storage.
        with pytest.raises(AttributeError):
            Extraction().engine = "other"  # type: ignore[misc]


class TestRegistry:
    def test_tesseract_is_registered(self) -> None:
        assert "tesseract" in available_engines()
        assert ENGINE_FACTORIES["tesseract"] is TesseractOcrEngine

    def test_it_builds_the_configured_engine(self) -> None:
        assert isinstance(get_ocr_engine("tesseract"), TesseractOcrEngine)

    def test_an_unknown_identifier_falls_back_rather_than_failing(self) -> None:
        # An engine name is deployment configuration; an unreadable one should
        # degrade to the documented default with a warning, not take the API down.
        assert isinstance(get_ocr_engine("nonexistent-engine"), TesseractOcrEngine)

    def test_the_registry_cannot_be_mutated(self) -> None:
        with pytest.raises(TypeError):
            ENGINE_FACTORIES["rogue"] = TesseractOcrEngine  # type: ignore[index]

    def test_the_default_comes_from_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "OCR_ENGINE", "TESSERACT")

        assert isinstance(get_ocr_engine(), TesseractOcrEngine)


class TestProtocolConformance:
    def test_tesseract_satisfies_the_protocol(self) -> None:
        engine: OcrEngine = TesseractOcrEngine()

        assert engine.name == "tesseract"

    def test_the_fake_engine_satisfies_the_protocol(self, ocr_engine) -> None:  # type: ignore[no-untyped-def]
        # If the double could drift from the protocol, every test using it would
        # be proving something about a shape production does not have.
        engine: OcrEngine = ocr_engine

        assert engine.name == "fake"
        assert engine.is_available() is True


class TestFormatPolicy:
    @pytest.mark.parametrize("extension", ["pdf", "png", "jpg", "jpeg"])
    def test_it_supports_the_specs_formats(self, extension: str) -> None:
        assert TesseractOcrEngine().supports(extension)

    @pytest.mark.parametrize("extension", ["docx", "txt", "xlsx"])
    def test_it_refuses_other_formats(self, extension: str) -> None:
        engine = TesseractOcrEngine()

        assert not engine.supports(extension)
        with pytest.raises(OcrUnsupportedFormatError):
            engine.extract(b"anything", extension=extension)

    def test_empty_content_is_a_corrupted_document(self) -> None:
        # Refused before the engine is even consulted: zero bytes is not a file.
        with pytest.raises(OcrCorruptedDocumentError):
            TesseractOcrEngine().extract(b"", extension="pdf")


class TestDeadline:
    def test_the_remaining_budget_shrinks_as_the_run_proceeds(self) -> None:
        engine = TesseractOcrEngine(timeout_seconds=60)
        started = time.monotonic()

        first = engine._remaining(started)
        time.sleep(0.05)
        second = engine._remaining(started)

        # The deadline covers the *whole* extraction: a fresh allowance per page
        # would let a 100-page document run for hours under a 120-second limit.
        assert second < first

    def test_it_never_returns_zero(self) -> None:
        engine = TesseractOcrEngine(timeout_seconds=60)

        # `0` means "no timeout at all" to pytesseract, which is the opposite of
        # what a nearly-exhausted budget should mean.
        assert engine._remaining(time.monotonic() - 59.999) >= 1.0

    def test_an_exhausted_budget_raises(self) -> None:
        engine = TesseractOcrEngine(timeout_seconds=1)

        with pytest.raises(OcrTimeoutError):
            engine._remaining(time.monotonic() - 5)


class TestTemporaryResources:
    def test_the_scratch_directory_is_removed(self) -> None:
        import os

        engine = TesseractOcrEngine()
        with engine._temporary_directory() as workdir:
            assert os.path.isdir(workdir)
            captured = workdir

        # The spec requires temporary resources to be cleaned up, and a
        # partially-read 200-page scan is exactly where a leak would hide.
        assert not os.path.exists(captured)

    def test_it_is_removed_even_when_the_block_raises(self) -> None:
        import os

        engine = TesseractOcrEngine()
        captured = ""
        with pytest.raises(RuntimeError), engine._temporary_directory() as workdir:
            captured = workdir
            raise RuntimeError("boom")

        assert captured and not os.path.exists(captured)


class TestAvailability:
    def test_it_reports_unavailable_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = TesseractOcrEngine()
        monkeypatch.setattr(
            engine, "_configure", lambda: (_ for _ in ()).throw(RuntimeError("no binary"))
        )

        # A missing binary must surface as an actionable failed run, never as a
        # stack trace on the first upload.
        assert engine.version() is None
        assert engine.is_available() is False
