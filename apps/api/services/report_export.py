"""The report-rendering boundary.

``14-ai-report-agent.md`` requires **PDF** and **Markdown** *"at minimum"*, and
adds that the implementation *"should be extensible to support DOCX and HTML
later without redesign"*. This module is that boundary, and it has the shape
every other seam in this codebase has (:mod:`services.ocr_engine`,
:mod:`services.chunking`, :mod:`services.embedding`,
:mod:`services.vector_store`, :mod:`services.vector_search`,
:mod:`services.prompts`, :mod:`services.llm`):

* :class:`ReportRenderer` is the protocol :mod:`services.report` depends on;
* :class:`MarkdownReportRenderer` and :class:`PdfReportRenderer` are the two that
  ship — and two rather than one is the point, because a seam with a single
  implementation is a claim and a seam with two is a fact;
* :func:`get_report_renderer` resolves one by format, so DOCX or HTML is a class
  plus an entry in :data:`RENDERER_FACTORIES` plus a member on
  :class:`~core.reports.ReportFormat`, and no change to the service, the router,
  or the client.

**Nothing is stored.** ``architecture.md`` lists generated reports and exported
documents under MinIO, and this feature deliberately does not put them there: an
export is a *deterministic projection* of the report row, so storing the rendered
bytes would create a second copy that can disagree with the report the moment it
is regenerated, and would need a lifecycle, a cleanup job, and an authorization
story of its own. Rendering per request instead makes the spec's *"exported
reports inherit the same permissions as their source case"* structural — there is
no object anyone can be handed a URL to, and every byte is produced inside a
request that has already been authorized. The trade is a few milliseconds of CPU
per download, which is nothing beside the minutes the report took to generate.

**The renderers see a finished report and nothing else.** They are handed a
:class:`RenderableReport` — a title, a language, some sections, and some
citations — with no ORM instance, no user, no case, no repository, and no way to
reach a document. A renderer that *cannot be handed* the case file cannot leak
it, which is the same structural argument :class:`~services.rag_metrics.RagMetricsRecorder`
makes about never being handed a question.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Protocol

import structlog

from core.config import settings
from core.reports import ReportFailureCode, ReportFormat, references_title

logger = structlog.get_logger(__name__)

#: Characters stripped from a generated download filename.
#:
#: A report's title carries a case number and can carry whatever a user typed, and
#: the result reaches a ``Content-Disposition`` header — the same header
#: :mod:`core.documents` sanitises an uploaded filename for, and for the same
#: reason: a quotation mark or a newline in it is a header-injection primitive.
_UNSAFE_FILENAME = re.compile(r"[^\w\-. ]+", re.UNICODE)

#: Longest generated filename stem, before the extension.
MAX_FILENAME_LENGTH = 80

#: A representative Arabic letter (ARABIC LETTER BEH), used to ask a font whether
#: it can render Arabic at all.
#:
#: One codepoint rather than a range, deliberately: a font that maps beh maps the
#: Arabic block, and a font that does not is not a font this platform can use. It
#: is a *letter* rather than a diacritic or a presentation form, because those are
#: exactly the things a partial font omits while still claiming the block.
ARABIC_PROBE_CODEPOINT = 0x0628

#: Where to look for a font with Arabic coverage when none is configured.
#:
#: Ordered by preference rather than by platform: a Naskh face designed for body
#: text first, then general-purpose faces that happen to include Arabic. The list
#: is deliberately **not** a glob of the font directory — a search that took the
#: first ``.ttf`` it found would pick up ``DejaVuSans.ttf``, which is on almost
#: every Linux box and contains no Arabic at all.
#:
#: Every candidate is still verified against its own character map before use, so
#: an entry here is a *hint* rather than a promise, and adding one cannot break a
#: deployment that does not have it.
ARABIC_FONT_CANDIDATES: tuple[str, ...] = (
    # Debian/Ubuntu — `fonts-noto-core`, which is what an API image should
    # install. Noto Naskh is the face Google designed for Arabic body text.
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    # Debian/Ubuntu — `fonts-hosny-amiri`, a Naskh face widely used for legal and
    # literary Arabic typesetting.
    "/usr/share/fonts/truetype/amiri/Amiri-Regular.ttf",
    # Alpine and Fedora layouts for the same Noto packages.
    "/usr/share/fonts/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoNaskhArabic-Regular.ttf",
    # Debian/Ubuntu — `fonts-dejavu` is *not* here on purpose. See the note above.
    # `ttf-freefont` is, because FreeSerif does carry Arabic.
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    # macOS.
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    # Windows. Arial and Tahoma both carry full Arabic; Segoe UI is the system
    # face and carries it too.
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\tahoma.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
)


class ReportExportError(Exception):
    """A report could not be turned into a file.

    Deliberately **not** an :class:`~core.exceptions.AppException`, exactly as
    :class:`~services.llm.LLMError` and :class:`~services.prompts.PromptError`
    are not: this module is a library boundary, and translating a failure into a
    status line is the service's job.
    """

    #: The cause, reported to the caller and used to group failures.
    code: ReportFailureCode = ReportFailureCode.EXPORT_FAILURE

    def __init__(self, message: str, *, code: ReportFailureCode | None = None) -> None:
        self.code = code or self.code
        super().__init__(message)


class ReportRendererUnavailableError(ReportExportError):
    """The library this format needs is not installed here.

    Distinct from a rendering failure because the remedy is different: an
    operator installs a package, and the caller meanwhile has a format that works
    — which the service's message names, since a dead end and a workaround are
    not the same answer.
    """


# --------------------------------------------------------------------------- #
# What a renderer is handed
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RenderableSection:
    """One section of a report, as a renderer sees it."""

    title: str
    content: str
    #: Whether the pipeline could ground this section. Rendered as a marginal
    #: note rather than omitted: a section reading "the documents do not cover
    #: this" is a *finding*, and hiding it would leave a reader to conclude the
    #: report simply forgot to mention the parties.
    grounded: bool = True


@dataclass(frozen=True, slots=True)
class RenderableCitation:
    """One entry of a report's reference list, as a renderer sees it.

    Exactly the four references ``14-ai-report-agent.md`` names — document name,
    page number, document version, case — and deliberately nothing else. The
    excerpt is **not** here: a reference list is a list of where to look, and
    reproducing thirty passages of a client's file into a PDF that will be
    emailed is the opposite of what the spec's Security section asks for. The
    excerpts are on the API's detail response, behind the same authorization, for
    a reader who wants to check one.
    """

    marker: int
    document_name: str
    document_version: int
    page_number: int


@dataclass(frozen=True, slots=True)
class RenderableReport:
    """A finished report, in the only form a renderer ever sees it.

    A plain value rather than a :class:`~models.report.Report`: an ORM instance
    carries a case, a requester, a session, and lazy relationships to all three,
    and handing one to a rendering library would make "what can an exporter
    reach" a question about SQLAlchemy's loader configuration.
    """

    title: str
    language: str
    report_type: str
    case_number: str
    generated_at: datetime | None
    disclaimer: str
    sections: Sequence[RenderableSection] = field(default_factory=tuple)
    citations: Sequence[RenderableCitation] = field(default_factory=tuple)

    @property
    def is_rtl(self) -> bool:
        """Whether this report reads right-to-left.

        Arabic is one of the two languages ``project-overview.md`` names, so this
        is not a future concern: it decides paragraph alignment in the PDF and is
        the reason :class:`PdfReportRenderer` needs a font at all.
        """
        return self.language == "ar"


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class ReportRenderer(Protocol):
    """What the report service requires of an export backend.

    Five members, and none of them mentions a case, a user, a document, or a
    database. A renderer is handed a finished report and returns bytes — that
    narrowness is the seam, and it is what makes "add DOCX without redesign" a
    property of the type system rather than a promise in a document.
    """

    @property
    def format(self) -> ReportFormat:
        """The format this backend produces."""
        ...

    @property
    def media_type(self) -> str:
        """MIME type of the produced bytes."""
        ...

    @property
    def file_extension(self) -> str:
        """Extension of the produced file, without the dot."""
        ...

    def is_available(self) -> bool:
        """Whether this format can actually be produced here, right now."""
        ...

    def render(self, report: RenderableReport) -> bytes:
        """Produce the file.

        Raises:
            ReportExportError: the file could not be produced.
        """
        ...


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


class MarkdownReportRenderer:
    """The report as Markdown.

    **Always available**, and that is its most important property rather than an
    incidental one: it is produced by string concatenation with no library
    behind it, so it is the format a deployment can always fall back to when the
    PDF renderer's dependency is missing. Every "try Markdown instead" message on
    this feature rests on that being true without qualification.

    Markdown rather than plain text because the report is *structured* — the spec
    requires it — and headings are how that structure survives being pasted into
    an email, a wiki, or a word processor. It is also the input a future DOCX or
    HTML renderer would most naturally take, which is why this one is written
    first.
    """

    format = ReportFormat.MARKDOWN
    #: ``charset=utf-8`` is not decoration: a French or Arabic report served
    #: without it is decoded as Latin-1 by a browser that guesses, and every
    #: accented character becomes mojibake.
    media_type = "text/markdown; charset=utf-8"
    file_extension = "md"

    def is_available(self) -> bool:
        """Always ``True``. See the class docstring for why that matters."""
        return True

    def render(self, report: RenderableReport) -> bytes:
        """Produce the Markdown document.

        Section order is the report's, untouched: ``14-ai-report-agent.md``
        requires section ordering to be template-driven, and a renderer that
        sorted or grouped would be a second, contradictory opinion about the
        structure.
        """
        lines: list[str] = [f"# {report.title}", ""]

        meta = self._front_matter(report)
        if meta:
            lines.extend(meta)
            lines.append("")

        for section in report.sections:
            lines.append(f"## {section.title}")
            lines.append("")
            lines.append(section.content)
            lines.append("")

        if report.citations:
            lines.append(f"## {references_title(report.language)}")
            lines.append("")
            lines.extend(self._reference(citation) for citation in report.citations)
            lines.append("")

        lines.append("---")
        lines.append("")
        # Italicised so it reads as a note about the document rather than as one
        # more finding in it.
        lines.append(f"*{report.disclaimer}*")

        return "\n".join(lines).encode("utf-8")

    @staticmethod
    def _front_matter(report: RenderableReport) -> list[str]:
        """The two facts a reader needs before the first section.

        The case *number* and the generation timestamp, and nothing else. Not the
        case title, not the requester's name, and not the model that wrote it: an
        exported report leaves the platform, and everything on its first page is
        something that will be read by whoever it is forwarded to.
        """
        parts = [f"**{report.case_number}**"]
        if report.generated_at is not None:
            parts.append(report.generated_at.strftime("%Y-%m-%d %H:%M UTC"))
        return [" · ".join(parts)]

    @staticmethod
    def _reference(citation: RenderableCitation) -> str:
        """One line of the reference list.

        Formatted as ``[3] Contrat de bail.pdf — p. 7 (v2)``: the marker a reader
        followed, the document they should open, and where in it — which is
        exactly what a legal citation is for and is why the page number survived
        every stage of this pipeline from OCR onward.
        """
        return (
            f"[{citation.marker}] {citation.document_name} — p. {citation.page_number} "
            f"(v{citation.document_version})"
        )


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


class PdfReportRenderer:
    """The report as a paginated PDF, through ReportLab.

    ReportLab rather than a browser-based renderer (WeasyPrint, wkhtmltopdf)
    because those need system libraries or a headless browser in the image, and
    this platform already carries two system binaries it cannot pip-install
    (Tesseract and Poppler). A third would be a third thing to explain in a
    deployment guide; ReportLab is pure Python plus a C extension pip provides.

    **Arabic works out of the box, and getting there took three things rather
    than one.** ``project-overview.md`` names Arabic and French as *the*
    platform's languages, so an Arabic report that cannot be exported is not an
    edge case — it is half the intended users. ReportLab's built-in Type 1 fonts
    cover Latin script only, and there are two further traps behind that one:

    * **a font has to be found.** :data:`ARABIC_FONT_CANDIDATES` is searched when
      ``REPORT_PDF_FONT_PATH`` is not set, so a normal Debian image (with
      ``fonts-noto-core``) and a Windows host both work with no configuration.
      The setting remains, and still wins, as the override for a deployment that
      wants a particular typeface;
    * **the font has to actually contain Arabic.** Existing on disk is not
      coverage: ``DejaVuSans.ttf`` is present on almost every Linux box, is the
      obvious thing for a search like this to find, and has **no Arabic glyphs at
      all**. So every candidate is verified against the font's own character map
      (see :func:`has_arabic_coverage`) before it is accepted — otherwise
      discovery would reintroduce the silent page of boxes it exists to prevent;
    * **the text has to be shaped.** ReportLab draws glyphs in the order it is
      given them and performs no Arabic joining or bidirectional reordering, so
      ``arabic-reshaper`` and ``python-bidi`` do that first. Both are **required
      dependencies** rather than optional ones — unlike ``litellm``, which is an
      alternative to something that already works, these are the difference
      between correct Arabic and mangled Arabic.

    An Arabic report is still **refused rather than rendered** if no font with
    Arabic coverage can be found anywhere, with a message naming Markdown.
    Refusing stays the only honest option for that case: a legal report that
    exports as blank boxes is worse than one that does not export.
    """

    format = ReportFormat.PDF
    media_type = "application/pdf"
    file_extension = "pdf"

    #: Name the embedded font is registered under. Constant, because registration
    #: is process-wide in ReportLab and registering the same file twice under two
    #: names would keep two copies of it in memory.
    _FONT_NAME = "ReportBody"

    def __init__(self, font_path: str | None = None) -> None:
        self._font_path = font_path if font_path is not None else settings.REPORT_PDF_FONT_PATH
        #: Whether font resolution has run, and what it produced. Both cached —
        #: including the *negative* answer, so a deployment with no Arabic font
        #: does not rescan the candidate list on every download.
        self._resolved = False
        self._font_name: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ identity #

    def is_available(self) -> bool:
        """Whether ReportLab is installed here.

        A *library* probe, not a font probe: whether this deployment can render
        an **Arabic** report additionally depends on the configured font, and
        that is decided per report in :meth:`render` rather than here, because a
        French-only deployment with no font configured can export perfectly well
        and must not be told the format is unavailable.
        """
        try:
            self._modules()
        except ReportExportError:
            return False
        return True

    # -------------------------------------------------------------- render #

    def render(self, report: RenderableReport) -> bytes:
        """Produce the PDF.

        Raises:
            ReportRendererUnavailableError: ReportLab is not installed, or the
                report is Arabic and no font with Arabic coverage is configured.
            ReportExportError: the document could not be built.
        """
        modules = self._modules()
        font = self._font(modules)

        if report.is_rtl and font is None:
            logger.warning(
                "report_export_font_missing",
                language=report.language,
                configured=bool(self._font_path),
                searched=len(ARABIC_FONT_CANDIDATES),
            )
            raise ReportRendererUnavailableError(
                "PDF export of an Arabic report needs a font with Arabic coverage, and none "
                "could be found on this deployment. Export as Markdown instead, or install one "
                "(for example the fonts-noto-core package). Reports in French and English are "
                "unaffected."
            )

        try:
            return self._build(report, modules=modules, font=font)
        except ReportExportError:
            raise
        except Exception as exc:
            # The library's own message can quote the text it was laying out,
            # which here is a generated interpretation of a client's file. It is
            # never carried into the raised error and never logged — the same
            # rule :meth:`~services.llm.GeminiProvider._translate` follows.
            logger.error(
                "report_export_failed",
                export_format=self.format.value,
                error_type=type(exc).__name__,
            )
            raise ReportExportError("The report could not be rendered as a PDF.") from exc

    def _build(self, report: RenderableReport, *, modules: Any, font: str | None) -> bytes:
        """Lay the report out, in the same order every other renderer uses."""
        styles = self._styles(modules, report=report, font=font)
        buffer = BytesIO()

        document = modules.SimpleDocTemplate(
            buffer,
            pagesize=modules.A4,
            leftMargin=settings.REPORT_PDF_MARGIN,
            rightMargin=settings.REPORT_PDF_MARGIN,
            topMargin=settings.REPORT_PDF_MARGIN,
            bottomMargin=settings.REPORT_PDF_MARGIN,
            title=report.title,
            # Deliberately not the requester's name: PDF metadata travels with
            # the file and is read by every viewer, and the spec's Security
            # section is about what leaves the platform.
            author="Legal Case Management Platform",
        )

        flow: list[Any] = [
            modules.Paragraph(self._text(report.title, report), styles["ReportTitle"]),
            modules.Paragraph(self._text(self._meta(report), report), styles["ReportMeta"]),
            modules.Spacer(1, 12),
        ]

        for section in report.sections:
            flow.append(modules.Paragraph(self._text(section.title, report), styles["ReportHeading"]))
            for paragraph in self._paragraphs(section.content):
                flow.append(modules.Paragraph(self._text(paragraph, report), styles["ReportBody"]))
            flow.append(modules.Spacer(1, 8))

        if report.citations:
            flow.append(
                modules.Paragraph(
                    self._text(references_title(report.language), report), styles["ReportHeading"]
                )
            )
            for citation in report.citations:
                line = MarkdownReportRenderer._reference(citation)
                flow.append(modules.Paragraph(self._text(line, report), styles["ReportReference"]))

        flow.append(modules.Spacer(1, 16))
        flow.append(modules.Paragraph(self._text(report.disclaimer, report), styles["ReportNote"]))

        document.build(flow)
        return buffer.getvalue()

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _meta(report: RenderableReport) -> str:
        """The one-line front matter, identical to the Markdown renderer's."""
        if report.generated_at is None:
            return report.case_number
        return f"{report.case_number} · {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}"

    @staticmethod
    def _paragraphs(content: str) -> list[str]:
        """Split a section's prose into paragraphs.

        ReportLab's ``Paragraph`` collapses newlines, so a section whose model
        wrote a bulleted list would arrive as one run-on block. Splitting on line
        breaks keeps the shape the model produced without this module having to
        interpret Markdown — which it deliberately does not, for the reason
        ``ui-context.md`` gives about rendering generated text as markup.
        """
        return [line.strip() for line in content.splitlines() if line.strip()]

    @staticmethod
    def _escape(value: str) -> str:
        """Escape the mini-markup ReportLab's ``Paragraph`` interprets.

        Generated legal prose can contain ``<`` and ``&`` — a comparison, an
        ampersand in a firm's name — and ReportLab would read the first as the
        start of a tag and fail the whole render. This is the one place in the
        platform where generated text is escaped rather than delimited, because
        here it genuinely *is* being placed into a markup language.
        """
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _text(self, value: str, report: RenderableReport) -> str:
        """Prepare one run of text for the layout engine."""
        if report.is_rtl:
            value = _shape_rtl(value)
        return self._escape(value)

    def _styles(self, modules: Any, *, report: RenderableReport, font: str | None) -> Mapping[str, Any]:
        """Build the paragraph styles, honouring the report's direction."""
        base = modules.getSampleStyleSheet()
        body_font = font or "Helvetica"
        bold_font = font or "Helvetica-Bold"
        # ReportLab's alignment constants: 0 left, 2 right, 4 justify.
        align = 2 if report.is_rtl else 4
        heading_align = 2 if report.is_rtl else 0

        return {
            "ReportTitle": modules.ParagraphStyle(
                "ReportTitle",
                parent=base["Title"],
                fontName=bold_font,
                fontSize=settings.REPORT_PDF_FONT_SIZE + 8,
                leading=settings.REPORT_PDF_FONT_SIZE + 12,
                alignment=heading_align,
                spaceAfter=4,
            ),
            "ReportMeta": modules.ParagraphStyle(
                "ReportMeta",
                parent=base["Normal"],
                fontName=body_font,
                fontSize=settings.REPORT_PDF_FONT_SIZE - 1,
                textColor=modules.grey,
                alignment=heading_align,
                spaceAfter=8,
            ),
            "ReportHeading": modules.ParagraphStyle(
                "ReportHeading",
                parent=base["Heading2"],
                fontName=bold_font,
                fontSize=settings.REPORT_PDF_FONT_SIZE + 3,
                leading=settings.REPORT_PDF_FONT_SIZE + 6,
                alignment=heading_align,
                spaceBefore=10,
                spaceAfter=4,
            ),
            "ReportBody": modules.ParagraphStyle(
                "ReportBody",
                parent=base["BodyText"],
                fontName=body_font,
                fontSize=settings.REPORT_PDF_FONT_SIZE,
                leading=settings.REPORT_PDF_FONT_SIZE + 4,
                alignment=align,
                spaceAfter=4,
            ),
            "ReportReference": modules.ParagraphStyle(
                "ReportReference",
                parent=base["BodyText"],
                fontName=body_font,
                fontSize=settings.REPORT_PDF_FONT_SIZE - 1,
                leading=settings.REPORT_PDF_FONT_SIZE + 2,
                alignment=heading_align,
                spaceAfter=2,
            ),
            "ReportNote": modules.ParagraphStyle(
                "ReportNote",
                parent=base["BodyText"],
                fontName=body_font,
                fontSize=settings.REPORT_PDF_FONT_SIZE - 2,
                leading=settings.REPORT_PDF_FONT_SIZE,
                textColor=modules.grey,
                alignment=heading_align,
            ),
        }

    def _font(self, modules: Any) -> str | None:
        """Register (once) and return the embedded font, or ``None``.

        Resolved in two steps, and the order is the whole of the configuration
        story: the **configured path wins** when there is one, and otherwise the
        platform **discovers** a font with Arabic coverage from
        :data:`ARABIC_FONT_CANDIDATES`. So an operator who wants a particular
        typeface sets one, and everybody else gets a working Arabic export
        without knowing this setting exists.

        Registered behind a lock and cached, for the same reason the embedding
        model and the Jinja environment are: ReportLab's font registry is
        process-wide, and two concurrent first exports must not both parse a
        multi-megabyte font file. The *resolution* is cached too — including the
        negative answer — because scanning a candidate list and parsing each
        font's character map is not work to repeat per download.

        A configured path that cannot be loaded is a **warning, not a failure**,
        and it **falls through to discovery** rather than giving up: a typo in
        one setting should not cost a deployment an Arabic export it could
        otherwise have produced. A French report renders perfectly without any
        of this.
        """
        if self._resolved:
            return self._font_name

        with self._lock:
            if self._resolved:
                return self._font_name

            path = self._font_path or find_arabic_font()
            if self._font_path and not self._loadable(modules, self._font_path):
                logger.warning(
                    "report_export_font_unusable",
                    reason="configured_font_rejected",
                    fallback=True,
                )
                path = find_arabic_font()

            if not path:
                logger.info("report_export_font_missing", searched=len(ARABIC_FONT_CANDIDATES))
                self._resolved = True
                return None

            try:
                modules.pdfmetrics.registerFont(modules.TTFont(self._FONT_NAME, path))
            except Exception as exc:
                logger.warning("report_export_font_unusable", error_type=type(exc).__name__)
                self._resolved = True
                return None

            self._font_name = self._FONT_NAME
            self._resolved = True

        # The *file name* rather than the full path: a path can carry a machine's
        # directory layout, and this line is operational rather than diagnostic.
        logger.info(
            "report_export_font_registered",
            font=self._FONT_NAME,
            source=Path(path).name,
            configured=bool(self._font_path),
        )
        return self._font_name

    @staticmethod
    def _loadable(modules: Any, path: str) -> bool:
        """Whether a configured path is a font this renderer can actually use.

        Checked before it is trusted, so a misconfigured setting falls through to
        discovery instead of being registered and then producing boxes. Coverage
        is part of the question, not just parseability — a path pointing at
        ``DejaVuSans.ttf`` is a real font and still the wrong answer.
        """
        return Path(path).is_file() and has_arabic_coverage(path)

    @staticmethod
    def _modules() -> Any:
        """Import ReportLab lazily and expose exactly what this renderer uses.

        Lazy for the reason every other optional backend here is lazy: a
        deployment that never exports a PDF should not pay for the import, and a
        deployment that has not installed the package must **report** the fact
        rather than fail to start — the same posture a missing Tesseract, a
        missing embedding model, and a missing ``litellm`` take.

        Raises:
            ReportRendererUnavailableError: ReportLab is not installed.
        """
        try:
            from reportlab.lib.colors import grey
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        except ImportError as exc:
            logger.warning("report_renderer_unavailable", export_format="pdf", reason="library_missing")
            raise ReportRendererUnavailableError(
                "PDF export is not available on this deployment. Export as Markdown instead."
            ) from exc

        # One namespace rather than ten module-level names, so the import list
        # above is the single place that says what this renderer touches — and so
        # a reader can tell at a glance that nothing else from ReportLab is
        # reachable from the layout code below.
        return SimpleNamespace(
            A4=A4,
            grey=grey,
            getSampleStyleSheet=getSampleStyleSheet,
            ParagraphStyle=ParagraphStyle,
            Paragraph=Paragraph,
            SimpleDocTemplate=SimpleDocTemplate,
            Spacer=Spacer,
            pdfmetrics=pdfmetrics,
            TTFont=TTFont,
        )


def has_arabic_coverage(path: str) -> bool:
    """Whether a font file actually contains Arabic glyphs.

    Asked of the font's **own character map** rather than of its filename, and
    that is the point: a font is not Arabic-capable because it is called
    ``NotoSans`` or because it sits in a directory alongside one that is.
    ``DejaVuSans.ttf`` is the case that makes this necessary — it is present on
    almost every Linux host, is the obvious thing a font search finds first, and
    maps no Arabic codepoint at all.

    Returns ``False`` for anything that cannot be parsed, which is the right
    answer to "can this render Arabic" for a missing file, a corrupt file, an
    OpenType/CFF font ReportLab cannot embed, and a directory alike. It is a
    probe, so it never raises: the caller's next move is to try the next
    candidate.
    """
    try:
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:  # pragma: no cover - declared dependency
        return False

    try:
        # A throwaway registration name: this parses the file without touching
        # ReportLab's process-wide registry, which the caller does separately and
        # only for the font it accepts.
        font = TTFont(f"probe-{Path(path).name}", path)
        return ARABIC_PROBE_CODEPOINT in font.face.charToGlyph
    except Exception:
        return False


def find_arabic_font() -> str | None:
    """The first font on this host that can actually render Arabic, or ``None``.

    Searched in :data:`ARABIC_FONT_CANDIDATES` order, which is preference order
    rather than platform order, and **verified** rather than assumed — see
    :func:`has_arabic_coverage`.

    Returning ``None`` is a real answer rather than a failure: a French- or
    English-only deployment needs no Arabic font, exports PDFs correctly without
    one, and must not be told anything is wrong. It is only when an *Arabic*
    report is exported that the absence becomes a refusal.
    """
    for candidate in ARABIC_FONT_CANDIDATES:
        if Path(candidate).is_file() and has_arabic_coverage(candidate):
            return candidate
    return None


def _shape_rtl(value: str) -> str:
    """Shape and reorder Arabic text for a layout engine that does neither.

    Two transformations, in this order and only this order:

    * **reshaping** picks the correct contextual form of every letter. Arabic
      letters join, and their initial, medial, final, and isolated forms are
      different glyphs — drawing the isolated form of each is the difference
      between Arabic and a row of disconnected shapes;
    * **the bidirectional algorithm** puts the glyphs in visual order. ReportLab
      draws a string left to right and applies no reordering, so the text has to
      arrive already reversed — and only the algorithm can do that correctly for
      a line mixing Arabic with a Latin filename or a number, which every
      citation line in this platform does.

    Both libraries are **required** rather than optional (see the class
    docstring). The import is still guarded, because the honest fallback for a
    broken install is unshaped text plus a log line rather than a failed export —
    the reader gets something legible, and the operator gets told.
    """
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError:  # pragma: no cover - declared dependency
        logger.warning("report_export_rtl_unshaped", reason="library_missing")
        return value

    try:
        return str(get_display(arabic_reshaper.reshape(value)))
    except Exception:  # pragma: no cover - defensive
        logger.warning("report_export_rtl_unshaped", reason="shaping_failed")
        return value


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every renderer this build can produce.
#:
#: Adding one is a class implementing :class:`ReportRenderer`, a member on
#: :class:`~core.reports.ReportFormat`, and an entry here — the spec's
#: *"extensible to support DOCX and HTML later without redesign"*, in the same
#: shape as :data:`~services.llm.PROVIDER_FACTORIES`.
RENDERER_FACTORIES: Mapping[ReportFormat, type[MarkdownReportRenderer] | type[PdfReportRenderer]] = (
    MappingProxyType(
        {
            ReportFormat.MARKDOWN: MarkdownReportRenderer,
            ReportFormat.PDF: PdfReportRenderer,
        }
    )
)

#: The renderers the process shares, built on first use.
#:
#: Shared rather than per request because :class:`PdfReportRenderer` owns a font
#: registration that is process-wide anyway — a renderer per download would
#: re-register the font on every one.
_shared: dict[ReportFormat, ReportRenderer] = {}
_shared_lock = threading.Lock()


def get_report_renderer(export_format: ReportFormat) -> ReportRenderer:
    """Return the renderer for a format, shared across the process.

    Raises:
        ReportRendererUnavailableError: the format has no renderer in this build.
            Unreachable through the API — the query parameter accepts only
            :class:`~core.reports.ReportFormat` members — and deliberately loud
            rather than defaulted, because silently exporting a *different*
            format than the one asked for would be worse than failing.
    """
    cached = _shared.get(export_format)
    if cached is not None:
        return cached

    factory = RENDERER_FACTORIES.get(export_format)
    if factory is None:  # pragma: no cover - unreachable through the schema
        raise ReportRendererUnavailableError(
            "That export format is not available on this deployment."
        )

    with _shared_lock:
        existing = _shared.get(export_format)
        if existing is not None:
            return existing
        built: ReportRenderer = factory()
        _shared[export_format] = built
        return built


def available_formats() -> list[ReportFormat]:
    """Every format this deployment can actually produce.

    Probed rather than listed, so a client is never offered an export that will
    answer 503 — the same posture the monitoring endpoints take when they report
    ``embedding_available`` and ``llm_available`` instead of inferring them from
    a counter of zero.
    """
    return [
        export_format
        for export_format in ReportFormat
        if get_report_renderer(export_format).is_available()
    ]


def reset_report_renderer_cache() -> None:
    """Discard the shared renderers.

    For tests, and for a deployment that installs a font at runtime. Not called
    by the application.
    """
    with _shared_lock:
        _shared.clear()


def export_filename(title: str, *, extension: str) -> str:
    """Build a safe download filename from a report's title.

    Sanitised for the reason :mod:`core.documents` sanitises an uploaded one: it
    reaches a ``Content-Disposition`` header, where a quotation mark or a
    newline is a header-injection primitive rather than a typo. Accents are
    folded rather than stripped, so *"Synthèse"* stays readable as *"Synthese"*
    instead of becoming *"Synthse"*.
    """
    folded = unicodedata.normalize("NFKD", title)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    cleaned = _UNSAFE_FILENAME.sub(" ", ascii_only)
    stem = "-".join(cleaned.split())[:MAX_FILENAME_LENGTH].strip("-")
    return f"{stem or 'report'}.{extension}"


__all__ = [
    "ARABIC_FONT_CANDIDATES",
    "ARABIC_PROBE_CODEPOINT",
    "MAX_FILENAME_LENGTH",
    "RENDERER_FACTORIES",
    "MarkdownReportRenderer",
    "PdfReportRenderer",
    "RenderableCitation",
    "RenderableReport",
    "RenderableSection",
    "ReportExportError",
    "ReportRenderer",
    "ReportRendererUnavailableError",
    "available_formats",
    "export_filename",
    "find_arabic_font",
    "get_report_renderer",
    "has_arabic_coverage",
    "reset_report_renderer_cache",
]
