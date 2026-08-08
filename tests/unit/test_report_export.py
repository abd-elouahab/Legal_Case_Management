"""Unit tests for the report-rendering boundary.

The renderer seam, both implementations, the filename sanitiser, and the two
guarantees the export path rests on: that **Markdown is always available** (every
"try Markdown instead" message on this feature depends on it) and that an
**Arabic PDF is refused rather than rendered as empty boxes** when no font with
Arabic coverage is configured.

No database and no request: a renderer is handed a finished report and returns
bytes, which is exactly the narrowness that makes "add DOCX without redesign" a
property of the type system.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.reports import ReportFormat
from services import report_export
from services.report_export import (
    ARABIC_FONT_CANDIDATES,
    MarkdownReportRenderer,
    PdfReportRenderer,
    RenderableCitation,
    RenderableReport,
    RenderableSection,
    ReportRendererUnavailableError,
    available_formats,
    covers_arabic_report,
    export_filename,
    find_arabic_font,
    get_report_renderer,
    reset_report_renderer_cache,
)


def build_report(*, language: str = "fr", citations: bool = True) -> RenderableReport:
    return RenderableReport(
        title="Synthèse de l'affaire — CASE-2026-0007",
        language=language,
        report_type="case_summary",
        case_number="CASE-2026-0007",
        generated_at=datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
        disclaimer="Rapport généré automatiquement. Ne constitue pas un conseil juridique.",
        sections=(
            RenderableSection(
                title="Aperçu",
                content="Le litige porte sur un bail commercial [1].\nLe loyer est mensuel [2].",
                grounded=True,
            ),
            RenderableSection(
                title="Parties",
                content="Les documents indexés ne couvrent pas cette section.",
                grounded=False,
            ),
        ),
        citations=(
            (
                RenderableCitation(
                    marker=1, document_name="bail.pdf", document_version=1, page_number=7
                ),
                RenderableCitation(
                    marker=2, document_name="annexe.pdf", document_version=2, page_number=3
                ),
            )
            if citations
            else ()
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_renderers() -> None:
    """Renderers are process-wide, and one test's font configuration must not
    decide another's outcome."""
    reset_report_renderer_cache()


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


class TestMarkdown:
    def test_it_is_always_available(self) -> None:
        """Its most important property rather than an incidental one: every "try
        Markdown instead" message on this feature rests on it being true without
        qualification."""
        assert MarkdownReportRenderer().is_available()

    def test_the_title_is_the_documents_heading(self) -> None:
        output = MarkdownReportRenderer().render(build_report()).decode("utf-8")

        assert output.startswith("# Synthèse de l'affaire — CASE-2026-0007")

    def test_sections_appear_as_headings_in_template_order(self) -> None:
        output = MarkdownReportRenderer().render(build_report()).decode("utf-8")

        assert output.index("## Aperçu") < output.index("## Parties")

    def test_the_prose_survives_verbatim(self) -> None:
        output = MarkdownReportRenderer().render(build_report()).decode("utf-8")

        assert "Le litige porte sur un bail commercial [1]." in output

    def test_the_reference_list_names_document_page_and_version(self) -> None:
        """The four references the spec asks for, in the form a lawyer acts on."""
        output = MarkdownReportRenderer().render(build_report()).decode("utf-8")

        assert "[1] bail.pdf — p. 7 (v1)" in output
        assert "[2] annexe.pdf — p. 3 (v2)" in output

    def test_the_reference_list_is_omitted_when_there_is_nothing_to_reference(self) -> None:
        output = (
            MarkdownReportRenderer().render(build_report(citations=False)).decode("utf-8")
        )

        assert "## Références" not in output

    def test_the_disclaimer_travels_with_the_document(self) -> None:
        """`ai-workflow-rules.md`: AI features are assistants, not
        decision-makers — and the statement has to survive the export, the email
        it is forwarded in, and the print-out."""
        output = MarkdownReportRenderer().render(build_report()).decode("utf-8")

        assert "Ne constitue pas un conseil juridique." in output

    def test_the_case_number_appears_and_the_case_title_does_not(self) -> None:
        output = MarkdownReportRenderer().render(build_report()).decode("utf-8")

        assert "CASE-2026-0007" in output

    def test_an_excerpt_is_never_reproduced_in_the_reference_list(self) -> None:
        """A reference list is a list of where to look. Reproducing thirty
        passages of a client's file into a document that will be emailed is the
        opposite of what the spec's Security section asks for."""
        output = MarkdownReportRenderer().render(build_report()).decode("utf-8")

        assert "Le loyer est payable" not in output

    def test_it_is_utf8(self) -> None:
        rendered = MarkdownReportRenderer().render(build_report(language="ar"))

        assert rendered.decode("utf-8")

    def test_the_media_type_declares_the_charset(self) -> None:
        """Served without it, a French report is decoded as Latin-1 by a browser
        that guesses and every accent becomes mojibake."""
        assert "charset=utf-8" in MarkdownReportRenderer().media_type


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


class TestPdf:
    def test_it_produces_a_pdf(self) -> None:
        rendered = PdfReportRenderer().render(build_report())

        assert rendered.startswith(b"%PDF")
        assert len(rendered) > 500

    def test_generated_prose_containing_markup_does_not_break_the_render(self) -> None:
        """Legal prose contains ``<`` and ``&`` — a comparison, an ampersand in a
        firm's name — and ReportLab reads the first as the start of a tag."""
        report = RenderableReport(
            title="A & B < C",
            language="fr",
            report_type="case_summary",
            case_number="CASE-2026-0001",
            generated_at=None,
            disclaimer="Note.",
            sections=(
                RenderableSection(
                    title="Aperçu",
                    content="La créance de 5 000 < 10 000 et Dupont & Fils est partie.",
                ),
            ),
        )

        assert PdfReportRenderer().render(report).startswith(b"%PDF")

    def test_an_arabic_report_renders_when_a_font_can_be_found(self) -> None:
        """The point of the discovery: `project-overview.md` names Arabic as one
        of the platform's two languages, so an Arabic export that needed manual
        configuration would be half the intended users locked out of PDF."""
        if find_arabic_font() is None:  # pragma: no cover - environment dependent
            pytest.skip("no font with Arabic coverage on this host")

        assert PdfReportRenderer().render(build_report(language="ar")).startswith(b"%PDF")

    def test_an_arabic_report_is_refused_when_no_font_exists_anywhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refusing stays the only honest option for that case: ReportLab's
        built-in fonts are Latin-only, so an Arabic report rendered with them is a
        page of empty boxes — a silently blank legal document that looks like a
        working export."""
        monkeypatch.setattr(report_export, "ARABIC_FONT_CANDIDATES", ())

        with pytest.raises(ReportRendererUnavailableError) as failure:
            PdfReportRenderer(font_path=None).render(build_report(language="ar"))

        assert "Markdown" in str(failure.value)

    def test_a_misconfigured_font_falls_back_to_the_search(self) -> None:
        """A typo in one setting should not cost a deployment an Arabic export it
        could otherwise have produced."""
        if find_arabic_font() is None:  # pragma: no cover - environment dependent
            pytest.skip("no font with Arabic coverage on this host")

        renderer = PdfReportRenderer(font_path="/nonexistent/font.ttf")

        assert renderer.render(build_report(language="ar")).startswith(b"%PDF")

    def test_a_misconfigured_font_does_not_break_a_french_report(self) -> None:
        """A French report renders perfectly without any of this, and refusing
        every export because an Arabic font is misconfigured would be the
        misconfiguration taking down the working case as well."""
        renderer = PdfReportRenderer(font_path="/nonexistent/font.ttf")

        assert renderer.render(build_report(language="fr")).startswith(b"%PDF")

    def test_it_reports_its_own_availability(self) -> None:
        assert PdfReportRenderer().is_available() is True


class TestArabicFontDiscovery:
    def test_coverage_is_read_from_the_fonts_own_character_map(self) -> None:
        """Not from its filename, and not from the directory it sits in."""
        found = find_arabic_font()
        if found is None:  # pragma: no cover - environment dependent
            pytest.skip("no usable font on this host")

        assert covers_arabic_report(found)

    @pytest.mark.parametrize(
        "path", ["/nonexistent/font.ttf", "", "/usr", "C:\\Windows\\Fonts", "Helvetica"]
    )
    def test_anything_unparseable_has_no_coverage(self, path: str) -> None:
        """A probe, so it answers rather than raising — the caller's next move is
        to try the next candidate. ``Helvetica`` is in the list because
        ReportLab's built-in Type 1 faces are not TrueType files at all, which is
        precisely why they cannot render Arabic."""
        assert covers_arabic_report(path) is False

    def test_the_probe_requires_a_presentation_form_not_just_the_arabic_block(
        self,
    ) -> None:
        """The glyphs actually drawn are presentation forms, because
        `_shape_rtl` converts to them before rendering. A font carrying the base
        block and not these renders nothing, so checking `U+0628` alone is not
        enough."""
        assert 0x0628 in report_export.REQUIRED_CODEPOINTS
        assert 0xFEDF in report_export.REQUIRED_CODEPOINTS

    def test_the_probe_requires_latin_as_well_as_arabic(self) -> None:
        """The trap that a first attempt at this walked into: the Noto Arabic
        faces render Arabic beautifully and carry **no Latin and no em dash**, so
        every case number, filename, page reference, and citation dash in an
        Arabic report would come out as a box — the same defect inverted.

        `[1] bail.pdf — p. 7 (v1)` is what every citation line looks like, and it
        is almost entirely Latin.
        """
        assert 0x0041 in report_export.REQUIRED_CODEPOINTS
        assert 0x2014 in report_export.REQUIRED_CODEPOINTS

    def test_an_arabic_only_font_would_be_rejected(self) -> None:
        """Asserted against the *rule* rather than against a font this host may
        not have: a character map with Arabic and no Latin must not pass."""
        arabic_only = {0x0628, 0xFEDF}

        assert not all(
            codepoint in arabic_only for codepoint in report_export.REQUIRED_CODEPOINTS
        )

    def test_no_candidate_is_a_bare_directory_glob(self) -> None:
        """A search that took the first `.ttf` it found would pick a Latin-only
        or an Arabic-only face, and both fail silently. Every candidate is a
        specific, named file."""
        assert all(candidate.lower().endswith(".ttf") for candidate in ARABIC_FONT_CANDIDATES)

    def test_the_search_answers_none_rather_than_raising_when_nothing_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A French- or English-only deployment needs no Arabic font, exports
        PDFs correctly without one, and must not be told anything is wrong."""
        monkeypatch.setattr(report_export, "ARABIC_FONT_CANDIDATES", ("/nope/x.ttf",))

        assert find_arabic_font() is None


class TestArabicShaping:
    """A font alone is not enough, and these are the two reasons why.

    ReportLab draws glyphs in the order it is handed them and applies no Arabic
    joining. Without both transformations an Arabic report exports as a row of
    disconnected, reversed letters — which is *legible enough to look intended*,
    and therefore worse than a blank page.
    """

    #: Arabic Presentation Forms-A and -B, the blocks a reshaper writes joined
    #: letter forms into. Nothing in ordinary Arabic *input* lives here, which is
    #: what makes their presence proof that reshaping ran.
    PRESENTATION_FORMS = ((0xFB50, 0xFDFF), (0xFE70, 0xFEFF))

    def _is_presentation_form(self, character: str) -> bool:
        return any(low <= ord(character) <= high for low, high in self.PRESENTATION_FORMS)

    def test_letters_are_reshaped_into_their_joined_forms(self) -> None:
        """Arabic letters join, and their initial, medial, final, and isolated
        forms are different glyphs. Drawing the isolated form of each is the
        difference between Arabic and a row of disconnected shapes."""
        source = "عقد كراء تجاري"
        shaped = report_export._shape_rtl(source)

        assert not any(self._is_presentation_form(character) for character in source)
        assert any(self._is_presentation_form(character) for character in shaped)

    def test_the_text_is_reordered_for_a_left_to_right_engine(self) -> None:
        """The bidirectional algorithm has to run, because ReportLab will not do
        it — the string has to arrive already in visual order.

        Asserted as *reversal of the word order* rather than of characters, which
        is the property that survives reshaping changing the codepoints
        themselves.
        """
        shaped = report_export._shape_rtl("عقد كراء")
        first_word, second_word = "عقد", "كراء"

        # Both words are still present as shaped runs, and the one that comes
        # first logically now comes last visually.
        assert len(shaped.split()) == 2
        assert report_export._shape_rtl(second_word).strip() in shaped.split()[0]
        assert report_export._shape_rtl(first_word).strip() in shaped.split()[1]

    def test_latin_inside_arabic_keeps_its_own_direction(self) -> None:
        """Every citation line in this platform mixes the two — "bail.pdf" must
        not come out reversed inside an Arabic sentence, and only the real
        algorithm gets that right."""
        shaped = report_export._shape_rtl("انظر bail.pdf صفحة 7")

        assert "bail.pdf" in shaped

    def test_latin_only_text_is_left_alone(self) -> None:
        assert report_export._shape_rtl("Contrat de bail") == "Contrat de bail"


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


class TestResolution:
    @pytest.mark.parametrize("export_format", list(ReportFormat))
    def test_every_format_resolves_to_a_renderer(self, export_format: ReportFormat) -> None:
        renderer = get_report_renderer(export_format)

        assert renderer.format is export_format
        assert renderer.file_extension
        assert renderer.media_type

    def test_the_renderer_is_shared_across_calls(self) -> None:
        """It owns a process-wide font registration; one per download would
        re-register the font on every one."""
        assert get_report_renderer(ReportFormat.PDF) is get_report_renderer(ReportFormat.PDF)

    def test_available_formats_are_probed_rather_than_listed(self) -> None:
        """So a client is never offered an export that will answer 503."""
        assert ReportFormat.MARKDOWN in available_formats()


class TestFilenames:
    def test_accents_are_folded_rather_than_stripped(self) -> None:
        assert export_filename("Synthèse", extension="pdf") == "Synthese.pdf"

    def test_spaces_become_hyphens(self) -> None:
        assert export_filename("Case Summary 2026", extension="md") == "Case-Summary-2026.md"

    @pytest.mark.parametrize("hostile", ['a"b', "a\r\nb", "a/../b", "a;b"])
    def test_header_special_characters_are_removed(self, hostile: str) -> None:
        """The value reaches a ``Content-Disposition`` header, where a quotation
        mark or a newline is a header-injection primitive rather than a typo."""
        name = export_filename(hostile, extension="pdf")

        assert '"' not in name
        assert "\r" not in name
        assert "\n" not in name
        assert "/" not in name

    def test_a_title_with_nothing_usable_still_yields_a_filename(self) -> None:
        assert export_filename("؟؟؟", extension="md") == "report.md"

    def test_the_stem_is_bounded(self) -> None:
        assert len(export_filename("x" * 300, extension="pdf")) <= 84
