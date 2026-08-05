"""The OCR engine boundary.

``09-ocr-processing.md``: *"Use mature OCR libraries instead of implementing OCR
algorithms from scratch"* and *"the OCR engine should be abstracted so it can be
replaced in the future without changing the rest of the application"*. This
module is that abstraction, and it is the **only** place in the platform that
imports Tesseract, pytesseract, pdf2image, or Pillow.

The shape:

* :class:`OcrEngine` is the protocol everything above depends on — a name, a
  version, an availability probe, and one ``extract`` call. Nothing in
  :mod:`services.ocr` knows which engine is behind it.
* :class:`TesseractOcrEngine` is the implementation the spec's technology stack
  names.
* :func:`get_ocr_engine` resolves the configured engine by identifier, so adding
  a second one (handwriting, a cloud service, a layout-aware model) is one class
  plus one registry entry — the extensibility the spec's last section asks for.

Failures are translated at this boundary. Every way an extraction can go wrong
becomes an :class:`OcrEngineError` carrying an
:class:`~core.ocr.OcrFailureCode`, so the service above records a cause without
knowing what a ``PDFPageCountError`` or a ``TesseractNotFoundError`` is — and so
the engine's raw message, which can quote the document's contents, never leaves
this module.

**Temporary resources are always released.** Page rendering happens inside a
:class:`tempfile.TemporaryDirectory` and every :class:`PIL.Image.Image` is
closed, including on the failure paths — the spec requires it explicitly, and a
partially-read 200-page scan is exactly where a leak would go unnoticed.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from core.config import settings
from core.ocr import (
    OcrFailureCode,
    is_paged,
    is_supported,
    mean_confidence,
    normalize_confidence,
    normalize_extracted_text,
    normalize_language,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class OcrEngineError(Exception):
    """An extraction could not be completed.

    Deliberately **not** an :class:`~core.exceptions.AppException`: an engine
    failure is not an HTTP response. Extraction runs in a background worker with
    no request behind it, and the failure's destination is the run's
    ``error_code`` column, not a status line. The service translates it.
    """

    #: The cause, recorded on the result and used to group failures in the
    #: monitoring view.
    code: OcrFailureCode = OcrFailureCode.ENGINE_FAILURE

    def __init__(self, message: str, *, code: OcrFailureCode | None = None) -> None:
        self.code = code or self.code
        super().__init__(message)


class OcrEngineUnavailableError(OcrEngineError):
    """The engine's binary or runtime is not installed or not reachable.

    An operational fault rather than a fault of the document: the same file will
    process correctly once Tesseract is on the host. It is still recorded as a
    failed run, because the spec requires a failure to update the status — and
    the run stays retryable, which is exactly the right recovery.
    """

    code = OcrFailureCode.ENGINE_FAILURE


class OcrUnsupportedFormatError(OcrEngineError):
    """The file's type is not one this engine can read."""

    code = OcrFailureCode.UNSUPPORTED_FORMAT


class OcrCorruptedDocumentError(OcrEngineError):
    """The bytes could not be decoded as the format their extension claims."""

    code = OcrFailureCode.CORRUPTED_DOCUMENT


class OcrTimeoutError(OcrEngineError):
    """Extraction exceeded the configured deadline and was stopped."""

    code = OcrFailureCode.TIMEOUT


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """One page as the engine read it."""

    page_number: int
    text: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class Extraction:
    """Everything one engine run produced.

    Frozen, because it is a report: the service records it, and nothing should be
    able to edit a machine's reading of a document on the way to storage.
    """

    pages: list[ExtractedPage] = field(default_factory=list)
    engine: str = ""
    engine_version: str | None = None
    detected_language: str | None = None
    confidence: float | None = None

    @property
    def page_count(self) -> int:
        """How many pages were read."""
        return len(self.pages)

    @property
    def has_text(self) -> bool:
        """Whether any page yielded non-whitespace text."""
        return any(page.text.strip() for page in self.pages)


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class OcrEngine(Protocol):
    """What the OCR service requires of an engine.

    Four members, and none of them mentions a file, a document, a database row,
    or a request — an engine is handed bytes and an extension and returns text.
    That narrowness is what makes the seam real: a replacement has nothing to
    reimplement beyond recognition itself.
    """

    @property
    def name(self) -> str:
        """Stable identifier recorded on every result ("tesseract")."""
        ...

    def version(self) -> str | None:
        """The engine's own version, or ``None`` when it cannot be determined."""
        ...

    def is_available(self) -> bool:
        """Whether the engine can run here, right now.

        Probed rather than assumed, so a missing binary is reported as an
        actionable failed run instead of surfacing as a stack trace on the first
        upload.
        """
        ...

    def supports(self, extension: str) -> bool:
        """Whether this engine reads files of this type."""
        ...

    def extract(self, content: bytes, *, extension: str) -> Extraction:
        """Read ``content`` and return its text.

        Raises:
            OcrEngineError: extraction could not be completed. The subclass, or
                the instance's :attr:`~OcrEngineError.code`, says why.
        """
        ...


# --------------------------------------------------------------------------- #
# Tesseract
# --------------------------------------------------------------------------- #


class TesseractOcrEngine:
    """Tesseract OCR, via pytesseract, with pdf2image for page rendering.

    The pipeline, per file type:

    * an **image** (PNG/JPEG) is opened with Pillow and read directly;
    * a **PDF** — scanned or not — is rendered page by page to images at
      ``OCR_DPI`` and each page is read. Rendering every PDF rather than first
      testing for an embedded text layer is deliberate for this feature: the spec
      lists "PDF" and "Scanned PDF" as two supported formats and asks for one
      pipeline, and a PDF that mixes a text layer with scanned inserts would
      otherwise yield the text of some pages and not others.

    Both paths converge on :meth:`_read_image`, so the language configuration,
    the confidence calculation, and the normalisation are applied once.
    """

    #: The identifier recorded on every result this engine produces.
    name = "tesseract"

    def __init__(
        self,
        *,
        languages: str | None = None,
        dpi: int | None = None,
        timeout_seconds: int | None = None,
        max_pages: int | None = None,
        poppler_path: str | None = None,
        tesseract_cmd: str | None = None,
    ) -> None:
        self._languages = languages or settings.OCR_LANGUAGES
        self._dpi = dpi or settings.OCR_DPI
        self._timeout = timeout_seconds or settings.OCR_TIMEOUT_SECONDS
        self._max_pages = max_pages or settings.OCR_MAX_PAGES
        self._poppler_path = poppler_path if poppler_path is not None else settings.POPPLER_PATH
        self._tesseract_cmd = (
            tesseract_cmd if tesseract_cmd is not None else settings.TESSERACT_CMD
        )
        self._configured = False

    # ------------------------------------------------------------ identity #

    def version(self) -> str | None:
        """Tesseract's reported version, or ``None`` if it cannot be asked."""
        try:
            self._configure()
            import pytesseract

            return str(pytesseract.get_tesseract_version())
        except Exception:
            return None

    def is_available(self) -> bool:
        """Whether the Tesseract binary can be invoked."""
        return self.version() is not None

    def supports(self, extension: str) -> bool:
        """Whether this engine reads files of this type."""
        return is_supported(extension)

    # ---------------------------------------------------------- extraction #

    def extract(self, content: bytes, *, extension: str) -> Extraction:
        """Read the document's bytes and return its text, page by page.

        Raises:
            OcrUnsupportedFormatError: the type is not one OCR applies to.
            OcrCorruptedDocumentError: the bytes are not a readable file of that
                type.
            OcrTimeoutError: the deadline was exceeded.
            OcrEngineUnavailableError: Tesseract or Poppler is not installed.
            OcrEngineError: anything else the engine reported.
        """
        normalized = extension.strip().lower()
        if not self.supports(normalized):
            raise OcrUnsupportedFormatError(f"OCR does not support {normalized!r} files.")
        if not content:
            raise OcrCorruptedDocumentError("The stored file is empty.")

        self._configure()
        started = time.monotonic()

        if is_paged(normalized):
            pages = self._extract_pdf(content, started=started)
        else:
            pages = self._extract_image(content, started=started)

        return Extraction(
            pages=pages,
            engine=self.name,
            engine_version=self.version(),
            # Tesseract is *told* which languages to try rather than detecting
            # them, so what is recorded is the configured set. A future engine
            # that genuinely detects a language reports it here instead, and no
            # caller changes — which is why the column is "detected_language"
            # and this is the honest value to put in it today.
            detected_language=normalize_language(self._languages),
            confidence=mean_confidence(page.confidence for page in pages),
        )

    # ------------------------------------------------------------- helpers #

    def _configure(self) -> None:
        """Point pytesseract at the configured binary, once per instance.

        ``TESSERACT_CMD`` exists because the binary is not on ``PATH`` on a
        default Windows install, which is where most of this platform's
        development happens. Left unset, pytesseract's own default applies.
        """
        if self._configured:
            return

        if self._tesseract_cmd:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd
        self._configured = True

    def _extract_image(self, content: bytes, *, started: float) -> list[ExtractedPage]:
        """Read a single-image document as one page."""
        import io

        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(content)) as image:
                # `load()` forces the decode inside the context manager, so a
                # truncated file fails here — where it is a corrupted document —
                # rather than later inside Tesseract, where it would be an
                # opaque engine failure.
                image.load()
                return [self._read_image(image, page_number=1, started=started)]
        except UnidentifiedImageError as exc:
            raise OcrCorruptedDocumentError("The image could not be decoded.") from exc
        except OSError as exc:
            # Pillow raises plain OSError for a truncated or malformed image.
            raise OcrCorruptedDocumentError("The image could not be decoded.") from exc

    def _extract_pdf(self, content: bytes, *, started: float) -> list[ExtractedPage]:
        """Render each PDF page to an image and read it.

        Pages are rendered **into a temporary directory** rather than held in
        memory: a 100-page scan at 300 DPI is several gigabytes of decoded
        bitmap, and ``pdf2image``'s default in-memory mode would hold all of it
        at once. With ``output_folder`` the images are lazily backed by files,
        and the directory — with every rendered page in it — is removed when the
        block exits, on the failure path as well as the success one.
        """
        from pdf2image import convert_from_bytes
        from pdf2image.exceptions import (
            PDFInfoNotInstalledError,
            PDFPageCountError,
            PDFSyntaxError,
        )

        pages: list[ExtractedPage] = []

        # `poppler_path` is passed only when set. pdf2image's own default is
        # `None` ("find the binaries on PATH") but its annotation does not admit
        # one, so omitting the argument is both the correct call and the one that
        # type-checks — a conditional keyword rather than a cast that would claim
        # `None` is a path.
        options: dict[str, Any] = (
            {"poppler_path": self._poppler_path} if self._poppler_path else {}
        )

        with self._temporary_directory() as workdir:
            try:
                images = convert_from_bytes(
                    content,
                    dpi=self._dpi,
                    output_folder=workdir,
                    # Rendering is bounded as well as reading: the deadline has
                    # to cover the whole run, not only the recognition half.
                    # Rounded up, because pdf2image passes this to
                    # `subprocess.communicate` as whole seconds.
                    timeout=max(1, int(self._remaining(started))),
                    # `last_page` is 1-based and inclusive, so this reads as
                    # "the first N pages" — the cap the spec's timeout handling
                    # needs, since a 900-page bundle would otherwise be a
                    # guaranteed timeout rather than a partial result.
                    last_page=self._max_pages,
                    **options,
                )
            except PDFInfoNotInstalledError as exc:
                raise OcrEngineUnavailableError(
                    "Poppler is not installed, so PDF pages cannot be rendered."
                ) from exc
            except (PDFPageCountError, PDFSyntaxError) as exc:
                raise OcrCorruptedDocumentError("The PDF could not be read.") from exc
            except Exception as exc:
                raise self._translate(exc, context="pdf_render") from exc

            try:
                for index, image in enumerate(images, start=1):
                    pages.append(self._read_image(image, page_number=index, started=started))
            finally:
                # Pillow images backed by temporary files hold open handles;
                # on Windows the directory cannot be removed until they close.
                for image in images:
                    image.close()

        return pages

    def _read_image(self, image: Image, *, page_number: int, started: float) -> ExtractedPage:
        """Run Tesseract over one rendered page.

        The text and the confidence come from **two** calls rather than one:
        ``image_to_data`` alone would give both, but its word-level
        reconstruction loses the line and block layout that
        ``image_to_string`` preserves — and that layout is what makes the stored
        text readable, and what a future chunker will split on. The second call
        is comparatively cheap because Tesseract has already been warmed on the
        same page.
        """
        import pytesseract

        remaining = self._remaining(started)

        try:
            text = pytesseract.image_to_string(
                image, lang=self._languages, timeout=remaining
            )
            confidence = self._page_confidence(image, timeout=self._remaining(started))
        except RuntimeError as exc:
            # pytesseract raises RuntimeError("Tesseract process timeout") when
            # its own `timeout` elapses.
            if "timeout" in str(exc).lower():
                raise OcrTimeoutError(
                    f"Extraction exceeded {self._timeout} seconds."
                ) from exc
            raise self._translate(exc, context="image_to_string", page=page_number) from exc
        except Exception as exc:
            raise self._translate(exc, context="image_to_string", page=page_number) from exc

        return ExtractedPage(
            page_number=page_number,
            text=normalize_extracted_text(text),
            confidence=confidence,
        )

    def _page_confidence(self, image: Image, *, timeout: float) -> float | None:
        """Mean word confidence for one page, or ``None`` if unavailable.

        Never raises: a confidence is a nice-to-have the spec qualifies with "if
        available", and losing a completed extraction because the *score* could
        not be computed would be a poor trade.
        """
        try:
            import pytesseract
            from pytesseract import Output

            data = pytesseract.image_to_data(
                image, lang=self._languages, output_type=Output.DICT, timeout=timeout
            )
        except Exception:
            return None

        scores: list[float] = []
        for raw in data.get("conf", []):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            # Tesseract writes -1 for a region it did not score.
            if value >= 0:
                scores.append(value)

        return mean_confidence(scores) if scores else None

    def _remaining(self, started: float) -> float:
        """Seconds left of the run's deadline, never zero or negative.

        The deadline covers the *whole* extraction, so each page gets what is
        left rather than a fresh allowance — otherwise a 100-page document with
        a 120-second timeout would run for over three hours. A floor of one
        second keeps a nearly-exhausted budget from being read by pytesseract as
        "no timeout at all", which ``0`` means.

        Raises:
            OcrTimeoutError: the deadline has already passed.
        """
        remaining = self._timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise OcrTimeoutError(f"Extraction exceeded {self._timeout} seconds.")
        return max(remaining, 1.0)

    @contextmanager
    def _temporary_directory(self) -> Iterator[str]:
        """A scratch directory that is removed however the block exits."""
        with tempfile.TemporaryDirectory(prefix="ocr-") as workdir:
            yield workdir

    @staticmethod
    def _translate(exc: Exception, *, context: str, page: int | None = None) -> OcrEngineError:
        """Turn a library exception into the platform's own vocabulary.

        The engine's message is **logged, not carried**: pytesseract surfaces
        Tesseract's stderr verbatim, which can echo fragments of the page it was
        reading, and that text would otherwise travel into a stored
        ``error_message`` and out to a client.
        """
        import pytesseract

        if isinstance(exc, pytesseract.TesseractNotFoundError):
            return OcrEngineUnavailableError(
                "Tesseract is not installed or not on the configured path."
            )

        logger.error(
            "ocr_engine_error",
            context=context,
            page=page,
            error=type(exc).__name__,
        )
        if isinstance(exc, pytesseract.TesseractError):
            return OcrEngineError("The OCR engine reported an error.")
        return OcrEngineError("The OCR engine could not process this document.")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

#: Engine identifier → factory.
#:
#: The extension point the spec's "multiple OCR engines" enhancement needs: a
#: second engine is one class plus one entry here, selected per deployment
#: through ``OCR_ENGINE``. Read-only so a bug elsewhere cannot swap an engine at
#: runtime.
ENGINE_FACTORIES: Mapping[str, type[TesseractOcrEngine]] = MappingProxyType(
    {TesseractOcrEngine.name: TesseractOcrEngine}
)


def available_engines() -> list[str]:
    """Every engine identifier this build can be configured to use."""
    return sorted(ENGINE_FACTORIES)


def get_ocr_engine(identifier: str | None = None) -> OcrEngine:
    """Build the configured OCR engine.

    Falls back to Tesseract for an unrecognised identifier rather than failing
    startup: an engine name is deployment configuration, and an unreadable one
    should degrade to the documented default with a warning, not take the API
    down. The fallback is logged, so the misconfiguration is visible.
    """
    wanted = (identifier or settings.OCR_ENGINE).strip().lower()

    factory = ENGINE_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "ocr_engine_unknown", requested=wanted, fallback=TesseractOcrEngine.name
        )
        factory = TesseractOcrEngine

    return factory()


__all__ = [
    "ENGINE_FACTORIES",
    "ExtractedPage",
    "Extraction",
    "OcrCorruptedDocumentError",
    "OcrEngine",
    "OcrEngineError",
    "OcrEngineUnavailableError",
    "OcrTimeoutError",
    "OcrUnsupportedFormatError",
    "TesseractOcrEngine",
    "available_engines",
    "get_ocr_engine",
    "normalize_confidence",
]
