"""OCR-domain utilities.

Small, pure helpers shared by the OCR schemas, repository, service, and engine:
the lifecycle policy, the format policy, the failure vocabulary, and the
normalisation applied to every piece of extracted text.

They live here rather than inside a service method for the same reason
:mod:`core.cases`, :mod:`core.users`, :mod:`core.documents`, and
:mod:`core.timeline` exist — the same rules must apply however a run arrives
(scheduled by an upload, scheduled by a replacement, or requested as a retry),
and they can be unit-tested without a database, a request, a running MinIO, or
an installed Tesseract.

**Nothing here calls an OCR engine.** The engine lives behind
:mod:`services.ocr_engine`, which is the seam the spec's "the OCR engine should
be abstracted so it can be replaced" requires.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from models.ocr import OcrStatus

#: Longest text retained for a single page, in characters.
#:
#: The column is unbounded ``Text``; the ceiling exists because a page's text is
#: produced by a machine reading arbitrary user-supplied bytes, and a
#: pathological input (a poster-sized scan of dense type, or an engine looping on
#: noise) must not be able to write an unbounded row. Far above anything a real
#: page of legal prose produces — a dense A4 page is roughly 4 000 characters.
MAX_PAGE_TEXT_CHARS = 200_000

#: Longest failure message stored on a result. Messages are ours, not the
#: engine's, so this is a guard against a future call site rather than against
#: user input.
MAX_ERROR_MESSAGE_LENGTH = 500

#: Separator written between pages when the run's text is rendered as one
#: string. U+000C FORM FEED is the conventional page break in plain text, which
#: is what makes the joined form losslessly splittable back into pages.
PAGE_SEPARATOR = "\f"


class OcrFailureCode(StrEnum):
    """Why a run ended without usable text.

    Machine-readable, so a client can branch on the cause and the monitoring
    view can group failures, without either parsing a sentence. The members cover
    exactly the failures ``09-ocr-processing.md`` lists under "Failure Handling",
    plus the storage fault that is the one way the *platform* rather than the
    document can be at fault.
    """

    #: The file's bytes could not be decoded as the format its extension claims.
    CORRUPTED_DOCUMENT = "corrupted_document"
    #: The file decoded, but the engine found nothing readable in it.
    UNREADABLE_DOCUMENT = "unreadable_document"
    #: Extraction exceeded ``OCR_TIMEOUT_SECONDS``.
    TIMEOUT = "timeout"
    #: The document's type is not one OCR applies to.
    UNSUPPORTED_FORMAT = "unsupported_format"
    #: The engine itself failed — missing binary, crash, or an error it reported.
    ENGINE_FAILURE = "engine_failure"
    #: The original file could not be read back from object storage.
    STORAGE_FAILURE = "storage_failure"
    #: Anything the branches above do not describe.
    UNKNOWN = "unknown"


#: Human-readable explanation per failure code.
#:
#: Kept here rather than raised with the exception so that the message a user
#: reads is written once and cannot vary between the engine's call sites. Every
#: one is deliberately free of the document's *contents* — it says what went
#: wrong with the file, never what was in it.
FAILURE_MESSAGES: Mapping[OcrFailureCode, str] = MappingProxyType(
    {
        OcrFailureCode.CORRUPTED_DOCUMENT: (
            "The file could not be read. It may be corrupted or incomplete."
        ),
        OcrFailureCode.UNREADABLE_DOCUMENT: (
            "No readable text could be found in this document."
        ),
        OcrFailureCode.TIMEOUT: (
            "Text extraction took too long and was stopped. Try again, or split the document."
        ),
        OcrFailureCode.UNSUPPORTED_FORMAT: (
            "Text extraction is not available for this file type."
        ),
        OcrFailureCode.ENGINE_FAILURE: (
            "The text extraction service could not process this document."
        ),
        OcrFailureCode.STORAGE_FAILURE: (
            "The stored file could not be retrieved. The document itself is unaffected."
        ),
        OcrFailureCode.UNKNOWN: "Text extraction did not complete.",
    }
)


def failure_message(code: OcrFailureCode) -> str:
    """The message shown for a failure code.

    Falls back to the generic explanation for a code with no entry, so a future
    member added without a message still reads as a sentence rather than as an
    identifier.
    """
    return FAILURE_MESSAGES.get(code, FAILURE_MESSAGES[OcrFailureCode.UNKNOWN])


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

#: The legal moves between OCR states, declared once.
#:
#: Read-only (``MappingProxyType`` over ``frozenset``s) so the policy cannot be
#: widened by mutation at runtime, exactly as ``STATUS_TRANSITIONS`` in
#: :mod:`core.cases` is.
#:
#: The shape is a cycle rather than a chain: ``COMPLETED`` and ``FAILED`` both
#: return to ``PENDING``, which is what a retry *is* — the same run, queued
#: again. Neither returns directly to ``PROCESSING``, because a run must be
#: claimed by a worker to enter it, and that claim is the platform's concurrency
#: guarantee (see :meth:`~repositories.ocr.OcrRepository.claim`).
STATUS_TRANSITIONS: Mapping[OcrStatus, frozenset[OcrStatus]] = MappingProxyType(
    {
        OcrStatus.PENDING: frozenset({OcrStatus.PROCESSING, OcrStatus.FAILED}),
        OcrStatus.PROCESSING: frozenset({OcrStatus.COMPLETED, OcrStatus.FAILED}),
        OcrStatus.COMPLETED: frozenset({OcrStatus.PENDING}),
        OcrStatus.FAILED: frozenset({OcrStatus.PENDING}),
    }
)

#: States a run may be retried from: the terminal ones.
#:
#: Derived from :data:`STATUS_TRANSITIONS` rather than listed again, so the two
#: cannot disagree — a state that can move to ``PENDING`` *is* a state a retry
#: may start from, and that is the definition rather than a coincidence.
RETRYABLE_STATUSES: frozenset[OcrStatus] = frozenset(
    status for status, targets in STATUS_TRANSITIONS.items() if OcrStatus.PENDING in targets
)


def can_transition(current: OcrStatus, target: OcrStatus) -> bool:
    """Whether a run may move from ``current`` to ``target``.

    A move to the state a run is already in is **not** a transition and is
    refused: "start processing" arriving twice is a concurrency bug, not a no-op,
    and treating it as one is what would let two workers believe they own the
    same run.
    """
    return target in STATUS_TRANSITIONS.get(current, frozenset())


def can_retry(current: OcrStatus) -> bool:
    """Whether a run in this state may be retried."""
    return current in RETRYABLE_STATUSES


# --------------------------------------------------------------------------- #
# Format policy
# --------------------------------------------------------------------------- #

#: File types OCR applies to — the spec's minimum set.
#:
#: A "scanned PDF" needs no separate entry: it is a PDF whose pages happen to be
#: images, and the engine renders every PDF page to an image before reading it,
#: so the two are the same path. Word documents and plain text are deliberately
#: absent: they already carry machine-readable text, and running an image
#: recogniser over a rendering of them would produce a worse copy of something
#: the platform already has. Extracting text from those is a *different*
#: capability, and inventing it is not this feature's call.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({"pdf", "png", "jpg", "jpeg"})

#: The subset that is a page-based document rather than a single image.
PAGED_EXTENSIONS: frozenset[str] = frozenset({"pdf"})


def is_supported(extension: str) -> bool:
    """Whether OCR can be run over a file of this type."""
    return extension.strip().lower() in SUPPORTED_EXTENSIONS


def sorted_supported_extensions() -> list[str]:
    """The OCR-able file types in a stable order, for messages and OpenAPI."""
    return sorted(SUPPORTED_EXTENSIONS)


def is_paged(extension: str) -> bool:
    """Whether a file of this type can hold more than one page."""
    return extension.strip().lower() in PAGED_EXTENSIONS


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

#: Characters no OCR engine should be emitting: C0 controls other than tab and
#: newline, plus DEL. They arrive from a misread of image noise, and they would
#: travel straight into a JSON response and a future search index.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")

#: Three or more consecutive blank lines. OCR of a sparse scan produces long
#: runs of them, which cost storage and tell a reader nothing.
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

#: Trailing whitespace at the end of a line.
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)


def normalize_extracted_text(value: str) -> str:
    """Reduce one page's raw engine output to the text that is stored.

    Four rules, in order, and every one of them exists to protect a property the
    spec names:

    * **Unicode is normalised to NFC**, so Arabic and French text recognised on
      one platform compares equal to the same text recognised on another. Without
      it "é" as a single code point and "é" as e + combining acute are two
      different strings, and a future search index would treat them as such.
    * **Line endings are unified to ``\\n``**, because the engine's output
      depends on the host it runs on and the stored text must not.
    * **Control characters are stripped** — see :data:`_CONTROL_CHARS`. Tab and
      newline survive, because they carry the page's layout.
    * **Runs of blank lines are collapsed and trailing spaces removed**, which
      preserves the paragraph structure while discarding the noise around it.

    The page separator is *not* inserted here: a page's text is stored per page,
    and joining is a rendering decision (see :func:`join_pages`).
    """
    if not value:
        return ""

    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # The separator is a control character itself, and a stray one inside a
    # page's text would make the joined form split back into the wrong pages.
    text = text.replace(PAGE_SEPARATOR, "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = _TRAILING_SPACE.sub("", text)
    text = _EXCESS_BLANK_LINES.sub("\n\n", text)
    text = text.strip()

    return text[:MAX_PAGE_TEXT_CHARS]


def join_pages(texts: Iterable[str]) -> str:
    """Render a run's pages as one string, page boundaries preserved.

    Joined with :data:`PAGE_SEPARATOR` rather than with a blank line, so the
    result splits back into exactly the pages it was built from. Empty pages are
    kept, not skipped: dropping page 3 because it was blank would renumber every
    page after it.
    """
    return PAGE_SEPARATOR.join(texts)


def split_pages(text: str) -> list[str]:
    """The inverse of :func:`join_pages`."""
    return text.split(PAGE_SEPARATOR) if text else []


def normalize_language(value: str | None) -> str | None:
    """Reduce an engine's language identifier to a stored value.

    Accepts what the engines actually emit — ``"eng"``, ``"fra+ara"``,
    ``"  ENG "`` — and stores it lowercased and trimmed. ``None`` and a blank
    string both become ``None``, because "the engine did not report a language"
    and "the engine reported an empty one" are the same statement, and carrying
    both would make every reader test for two things.
    """
    if value is None:
        return None

    normalized = " ".join(value.split()).lower()
    if not normalized:
        return None
    return normalized[:50]


def normalize_confidence(value: float | None) -> float | None:
    """Clamp an engine's confidence into the 0-100 range this platform reports.

    Tesseract emits ``-1`` for a region it could not score, and a mean over a
    page with no recognised words comes back negative; neither is a confidence,
    so both become ``None`` rather than a misleading zero. Values above 100 are
    clamped rather than rejected, because a slightly out-of-range float from a
    future engine is not worth failing a completed extraction over.
    """
    if value is None:
        return None
    if value < 0:
        return None
    return round(min(float(value), 100.0), 2)


def mean_confidence(values: Iterable[float | None]) -> float | None:
    """The average of the confidences that exist, or ``None`` if none do.

    Pages the engine could not score are *excluded* rather than counted as zero:
    a two-page document whose second page is a photograph should not report half
    the confidence of its first page's text.
    """
    scored = [value for value in values if value is not None]
    if not scored:
        return None
    return normalize_confidence(sum(scored) / len(scored))


def normalize_error_message(value: str | None) -> str | None:
    """Collapse a failure message and truncate it to the stored ceiling."""
    if value is None:
        return None

    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    return collapsed[:MAX_ERROR_MESSAGE_LENGTH]


def success_rate(completed: int, failed: int) -> float:
    """Share of finished runs that produced text, as a percentage.

    Only *terminal* runs are counted, which is what makes the number meaningful:
    including queued and in-flight runs would make the success rate fall every
    time a document is uploaded and recover as it is processed, which measures
    traffic rather than quality. No terminal runs at all reports ``0.0`` — there
    is nothing to be successful at yet.
    """
    total = completed + failed
    if total <= 0:
        return 0.0
    return round(completed / total * 100, 2)
