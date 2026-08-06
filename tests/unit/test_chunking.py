"""Unit tests for :mod:`services.chunking`.

The real splitter, deliberately: it is a pure function of text with no network,
no model, and no binary behind it, so substituting it would mean every chunking
guarantee ``10-document-indexing.md`` asks for — page order, page numbering,
overlap, semantic boundaries — was asserted against a fake.
"""

from __future__ import annotations

import pytest

from core.indexing import (
    LANGUAGE_ARABIC,
    LANGUAGE_FRENCH,
    MIN_CHUNK_CHARS,
    IndexFailureCode,
)
from services.chunking import (
    CHUNKER_FACTORIES,
    ChunkerUnavailableError,
    ChunkingError,
    RecursiveCharacterChunker,
    TextChunk,
    available_chunkers,
    get_chunker,
)

FRENCH_PAGE = (
    "CONTRAT DE BAIL COMMERCIAL\n\n"
    "Article 1 : Objet. Le bailleur loue au preneur les locaux désignés ci-après, "
    "situés à Casablanca, pour l'exercice d'une activité commerciale.\n\n"
    "Article 2 : Durée. Le présent bail est consenti pour une durée de trois années "
    "entières et consécutives à compter de la date de signature.\n\n"
    "Article 3 : Loyer. Le loyer annuel est fixé à soixante mille dirhams, payable "
    "par douzièmes d'avance le premier de chaque mois."
)

# RUF001 flags U+06D4 as "ambiguous" because it resembles a hyphen. That is the
# point: it is the Arabic full stop, this platform's documents are written with
# it, and a fixture that used a Latin period would not exercise the separator the
# chunker deliberately adds. Suppressed per line, so the check keeps working
# everywhere else.
ARABIC_PAGE = (
    "عقد كراء تجاري\n\n"
    "المادة الأولى يلتزم المكري بتسليم المحل التجاري إلى المكتري في حالة جيدة۔ "  # noqa: RUF001
    "ويقع المحل بمدينة الدار البيضاء۔\n\n"  # noqa: RUF001
    "المادة الثانية حدد مبلغ الكراء في ستين ألف درهم سنويا تؤدى شهريا۔ "  # noqa: RUF001
    "ويلتزم المكتري بأداء الوجيبة الكرائية في بداية كل شهر۔"  # noqa: RUF001
)


@pytest.fixture
def chunker() -> RecursiveCharacterChunker:
    return RecursiveCharacterChunker(chunk_size=200, chunk_overlap=40)


class TestAvailability:
    def test_the_library_is_installed(self, chunker: RecursiveCharacterChunker) -> None:
        assert chunker.is_available()

    def test_the_configured_settings_are_reported(self) -> None:
        # Recorded on every run, because a collection built at one setting is not
        # comparable with one built at another.
        built = RecursiveCharacterChunker(chunk_size=512, chunk_overlap=64)
        assert built.chunk_size == 512
        assert built.chunk_overlap == 64


class TestSeparators:
    def test_arabic_sentence_punctuation_is_a_separator(self) -> None:
        # The one deliberate platform decision in the list, and the reason it is
        # class data rather than inline: without it an Arabic page has no
        # sentence separator at all and degrades straight to word splitting.
        assert "۔" in RecursiveCharacterChunker.SEPARATORS  # noqa: RUF001 - Arabic full stop
        assert "؟" in RecursiveCharacterChunker.SEPARATORS
        assert "،" in RecursiveCharacterChunker.SEPARATORS

    def test_the_most_structural_separators_come_first(self) -> None:
        separators = RecursiveCharacterChunker.SEPARATORS
        assert separators[0] == "\n\n"
        assert separators[1] == "\n"
        # The character-level fallback is the last resort, never tried earlier.
        assert separators[-1] == ""


class TestSplitting:
    def test_a_page_is_divided_into_passages(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        chunks = chunker.split([FRENCH_PAGE])
        assert len(chunks) > 1
        assert all(isinstance(chunk, TextChunk) for chunk in chunks)

    def test_every_chunk_records_its_page(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        chunks = chunker.split([FRENCH_PAGE, ARABIC_PAGE])
        assert {chunk.page_number for chunk in chunks} == {1, 2}

    def test_page_order_is_preserved(self, chunker: RecursiveCharacterChunker) -> None:
        # The spec's "preserve page ordering". Pages are split one at a time and
        # numbering runs across the document, so the two orders agree.
        chunks = chunker.split([FRENCH_PAGE, ARABIC_PAGE])
        pages = [chunk.page_number for chunk in chunks]
        assert pages == sorted(pages)

    def test_chunk_numbers_run_across_the_document_without_gaps(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        chunks = chunker.split([FRENCH_PAGE, ARABIC_PAGE])
        assert [chunk.chunk_number for chunk in chunks] == list(range(len(chunks)))

    def test_a_blank_page_contributes_nothing_and_does_not_renumber(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        # A separator sheet has nothing to say. The page number travels *on* the
        # chunk rather than being inferred from a position, so page 3 is still
        # page 3 after page 2 produced nothing.
        chunks = chunker.split([FRENCH_PAGE, "   \n\n  ", ARABIC_PAGE])
        assert {chunk.page_number for chunk in chunks} == {1, 3}

    def test_no_pages_produces_no_chunks(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        assert chunker.split([]) == []

    def test_a_page_of_page_numbers_produces_no_chunks(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        assert chunker.split(["— 14 —\n\n— 15 —\n\n— 16 —"]) == []

    def test_every_chunk_clears_the_minimum_length(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        for chunk in chunker.split([FRENCH_PAGE, ARABIC_PAGE]):
            assert len(chunk.text.strip()) >= MIN_CHUNK_CHARS

    def test_chunks_are_normalised(self, chunker: RecursiveCharacterChunker) -> None:
        for chunk in chunker.split([FRENCH_PAGE]):
            assert chunk.text == chunk.text.strip()

    def test_splitting_is_deterministic(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        first = chunker.split([FRENCH_PAGE, ARABIC_PAGE])
        second = chunker.split([FRENCH_PAGE, ARABIC_PAGE])
        assert [chunk.text for chunk in first] == [chunk.text for chunk in second]

    def test_a_chunk_reports_its_own_length(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        chunk = chunker.split([FRENCH_PAGE])[0]
        assert chunk.character_count == len(chunk.text)

    def test_a_chunk_is_immutable(self, chunker: RecursiveCharacterChunker) -> None:
        # Everything downstream is derived from exactly these values; letting
        # them be edited in flight is how a vector ends up describing a passage
        # that is not the one it was built from.
        chunk = chunker.split([FRENCH_PAGE])[0]
        with pytest.raises(AttributeError):
            chunk.text = "something else"  # type: ignore[misc]


class TestSemanticBoundaries:
    def test_passages_break_at_paragraphs_before_words(self) -> None:
        # The spec's "preserve semantic meaning": with a chunk size that fits a
        # paragraph, the splitter must use the paragraph boundary rather than
        # cutting mid-sentence. Each article therefore survives whole inside one
        # chunk, and no chunk holds text from two of them.
        chunker = RecursiveCharacterChunker(chunk_size=180, chunk_overlap=0)
        chunks = [chunk.text for chunk in chunker.split([FRENCH_PAGE])]

        for article in ("Article 1 : Objet.", "Article 2 : Durée.", "Article 3 : Loyer."):
            assert sum(article in chunk for chunk in chunks) == 1

        assert all(
            sum(chunk.count(f"Article {number} :") for number in (1, 2, 3)) <= 1
            for chunk in chunks
        )

    def test_a_sentence_keeps_the_punctuation_that_closes_it(self) -> None:
        # `keep_separator="end"`, not `True`: a chunk ending "Article 2" and the
        # next beginning ". Durée" reads as a fragment in a search result.
        chunker = RecursiveCharacterChunker(chunk_size=90, chunk_overlap=0)
        chunks = chunker.split([FRENCH_PAGE])
        assert not any(chunk.text.startswith(". ") for chunk in chunks)

    def test_overlap_repeats_text_between_neighbours(self) -> None:
        # With no overlap, a definition split across two chunks is retrievable
        # from neither. The chunk size here is deliberately *below* a paragraph:
        # overlap is applied when the splitter merges smaller pieces into a
        # chunk, so a size that fits every paragraph whole never exercises it —
        # which is itself worth knowing when tuning `INDEX_CHUNK_OVERLAP`.
        with_overlap = RecursiveCharacterChunker(chunk_size=80, chunk_overlap=40)
        without = RecursiveCharacterChunker(chunk_size=80, chunk_overlap=0)

        overlapped = sum(len(chunk.text) for chunk in with_overlap.split([FRENCH_PAGE]))
        plain = sum(len(chunk.text) for chunk in without.split([FRENCH_PAGE]))
        assert overlapped > plain

    def test_a_smaller_chunk_size_produces_more_passages(self) -> None:
        small = RecursiveCharacterChunker(chunk_size=120, chunk_overlap=0)
        large = RecursiveCharacterChunker(chunk_size=600, chunk_overlap=0)
        assert len(small.split([FRENCH_PAGE])) > len(large.split([FRENCH_PAGE]))


class TestLanguage:
    def test_each_chunk_carries_its_own_language(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        # Per chunk rather than per document, because a Moroccan filing routinely
        # carries an Arabic body and a French annex.
        chunks = chunker.split([FRENCH_PAGE, ARABIC_PAGE])
        by_page = {chunk.page_number: chunk.language for chunk in chunks}
        assert by_page[1] == LANGUAGE_FRENCH
        assert by_page[2] == LANGUAGE_ARABIC


class TestArabicSplitting:
    def test_an_arabic_page_is_divided(self, chunker: RecursiveCharacterChunker) -> None:
        chunks = chunker.split([ARABIC_PAGE])
        assert len(chunks) > 1

    def test_arabic_text_survives_unmangled(
        self, chunker: RecursiveCharacterChunker
    ) -> None:
        chunks = chunker.split([ARABIC_PAGE])
        joined = "".join(chunk.text for chunk in chunks)
        assert "المادة الأولى" in joined
        assert "الدار البيضاء" in joined


class TestFailureTranslation:
    def test_a_missing_library_is_reported_as_an_indexing_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An operational fault rather than a fault of the document: the run is
        # recorded as failed and stays retryable, exactly as a missing Tesseract
        # is for OCR.
        import builtins

        real_import = builtins.__import__

        def refuse(name: str, *args: object, **kwargs: object) -> object:
            if name == "langchain_text_splitters":
                raise ImportError("no module named langchain_text_splitters")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", refuse)

        chunker = RecursiveCharacterChunker()
        assert not chunker.is_available()
        with pytest.raises(ChunkerUnavailableError) as failure:
            chunker.split([FRENCH_PAGE])
        assert failure.value.code is IndexFailureCode.CHUNKING_FAILURE

    def test_a_chunking_error_carries_a_machine_readable_code(self) -> None:
        assert ChunkingError("boom").code is IndexFailureCode.CHUNKING_FAILURE

    def test_a_failure_message_does_not_quote_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The library's message can echo what it was splitting, so it must not
        # travel out of this module.
        chunker = RecursiveCharacterChunker()

        class Exploding:
            def split_text(self, text: str) -> list[str]:
                raise RuntimeError(f"failed on {text!r}")

        monkeypatch.setattr(chunker, "_build", lambda: Exploding())

        with pytest.raises(ChunkingError) as failure:
            chunker.split([FRENCH_PAGE])
        assert "bailleur" not in str(failure.value)


class TestResolution:
    def test_the_default_chunker_is_the_recursive_one(self) -> None:
        assert isinstance(get_chunker(), RecursiveCharacterChunker)

    def test_an_unknown_identifier_falls_back_rather_than_failing(self) -> None:
        # A strategy name is deployment configuration; an unreadable one should
        # degrade to the documented default rather than take the API down.
        assert isinstance(get_chunker("not-a-chunker"), RecursiveCharacterChunker)

    def test_the_registry_is_the_extension_point(self) -> None:
        assert available_chunkers() == sorted(CHUNKER_FACTORIES)
        assert RecursiveCharacterChunker.name in CHUNKER_FACTORIES
