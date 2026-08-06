"""Unit tests for :mod:`core.indexing`.

Pure functions only — the lifecycle policy, the failure vocabulary, the chunk
identity, the language heuristic, and the normalisers. No database, no request,
no running Qdrant, and no downloaded embedding model, which is the whole reason
they live in ``core`` rather than inside a service method.
"""

from __future__ import annotations

import uuid

import pytest

from core.indexing import (
    CHUNK_ID_NAMESPACE,
    FAILURE_MESSAGES,
    LANGUAGE_ARABIC,
    LANGUAGE_ENGLISH,
    LANGUAGE_FRENCH,
    LANGUAGE_UNKNOWN,
    MIN_CHUNK_CHARS,
    REINDEXABLE_STATUSES,
    STATUS_TRANSITIONS,
    ChunkLocation,
    IndexFailureCode,
    can_reindex,
    can_transition,
    chunk_point_id,
    detect_language,
    dominant_language,
    failure_message,
    is_indexable_chunk,
    normalize_chunk_text,
    normalize_error_message,
    success_rate,
)
from models.indexing import IndexStatus


class TestLifecycle:
    def test_every_status_has_an_entry(self) -> None:
        assert set(STATUS_TRANSITIONS) == set(IndexStatus)

    def test_the_transition_table_is_read_only(self) -> None:
        # The policy must not be widenable by mutation at runtime — the same
        # guarantee `core.cases` and `core.ocr` make.
        with pytest.raises(TypeError):
            STATUS_TRANSITIONS[IndexStatus.INDEXED] = frozenset(IndexStatus)  # type: ignore[index]

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (IndexStatus.PENDING, IndexStatus.INDEXING),
            (IndexStatus.PENDING, IndexStatus.FAILED),
            (IndexStatus.INDEXING, IndexStatus.INDEXED),
            (IndexStatus.INDEXING, IndexStatus.FAILED),
            (IndexStatus.INDEXED, IndexStatus.PENDING),
            (IndexStatus.FAILED, IndexStatus.PENDING),
        ],
    )
    def test_the_legal_moves_are_allowed(
        self, current: IndexStatus, target: IndexStatus
    ) -> None:
        assert can_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            # A run must be *claimed* to start indexing; jumping straight back
            # into it would bypass the platform's concurrency control.
            (IndexStatus.INDEXED, IndexStatus.INDEXING),
            (IndexStatus.FAILED, IndexStatus.INDEXING),
            # Nothing reaches a terminal state without passing through INDEXING,
            # or every duration and start time would be a lie.
            (IndexStatus.PENDING, IndexStatus.INDEXED),
            (IndexStatus.INDEXED, IndexStatus.FAILED),
            (IndexStatus.FAILED, IndexStatus.INDEXED),
        ],
    )
    def test_the_illegal_moves_are_refused(
        self, current: IndexStatus, target: IndexStatus
    ) -> None:
        assert not can_transition(current, target)

    @pytest.mark.parametrize("status", list(IndexStatus))
    def test_a_move_to_the_same_state_is_not_a_transition(self, status: IndexStatus) -> None:
        # "Start indexing" arriving twice is a concurrency bug, not a no-op.
        assert not can_transition(status, status)

    def test_reindexable_statuses_are_exactly_the_terminal_ones(self) -> None:
        assert {IndexStatus.INDEXED, IndexStatus.FAILED} == REINDEXABLE_STATUSES

    def test_reindexable_is_derived_from_the_transition_table(self) -> None:
        # Derived rather than restated, so the two cannot disagree: a state that
        # can move to PENDING *is* a state a re-index may start from.
        assert {
            status
            for status, targets in STATUS_TRANSITIONS.items()
            if IndexStatus.PENDING in targets
        } == REINDEXABLE_STATUSES

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (IndexStatus.PENDING, False),
            (IndexStatus.INDEXING, False),
            (IndexStatus.INDEXED, True),
            (IndexStatus.FAILED, True),
        ],
    )
    def test_can_reindex(self, status: IndexStatus, expected: bool) -> None:
        assert can_reindex(status) is expected


class TestFailureCodes:
    def test_every_code_has_a_message(self) -> None:
        assert set(FAILURE_MESSAGES) == set(IndexFailureCode)

    @pytest.mark.parametrize("code", list(IndexFailureCode))
    def test_no_message_quotes_the_document(self, code: IndexFailureCode) -> None:
        message = failure_message(code)
        assert message
        assert message[0].isupper()
        assert message.endswith(".")

    def test_an_unknown_code_falls_back_rather_than_raising(self) -> None:
        # A future member added without a message must still read as a sentence.
        assert failure_message("not-a-code") == FAILURE_MESSAGES[IndexFailureCode.UNKNOWN]  # type: ignore[arg-type]


class TestChunkIdentity:
    def test_the_same_chunk_always_gets_the_same_id(self) -> None:
        document_id = uuid.uuid4()
        first = chunk_point_id(document_id, 1, 3, 7)
        second = chunk_point_id(document_id, 1, 3, 7)
        assert first == second

    def test_each_coordinate_changes_the_id(self) -> None:
        document_id = uuid.uuid4()
        base = chunk_point_id(document_id, 1, 3, 7)

        assert chunk_point_id(uuid.uuid4(), 1, 3, 7) != base
        assert chunk_point_id(document_id, 2, 3, 7) != base
        assert chunk_point_id(document_id, 1, 4, 7) != base
        assert chunk_point_id(document_id, 1, 3, 8) != base

    def test_the_id_is_a_uuid5_over_the_fixed_namespace(self) -> None:
        # Stated as the construction it is, so a future change that made ids
        # depend on the host or the Python version fails here rather than in
        # production as silently duplicated vectors.
        document_id = uuid.uuid4()
        assert chunk_point_id(document_id, 2, 5, 9) == uuid.uuid5(
            CHUNK_ID_NAMESPACE, f"{document_id}:2:5:9"
        )

    def test_a_location_derives_its_own_point_id(self) -> None:
        document_id = uuid.uuid4()
        location = ChunkLocation(
            document_id=document_id,
            document_version=2,
            case_id=uuid.uuid4(),
            page_number=5,
            chunk_number=9,
        )
        assert location.point_id == chunk_point_id(document_id, 2, 5, 9)

    def test_the_case_does_not_affect_the_id(self) -> None:
        # A document never changes case, so including it would add nothing — and
        # if one somehow did, the vectors must stay addressable.
        document_id = uuid.uuid4()
        first = ChunkLocation(document_id, 1, uuid.uuid4(), 1, 0)
        second = ChunkLocation(document_id, 1, uuid.uuid4(), 1, 0)
        assert first.point_id == second.point_id

    def test_a_location_is_immutable(self) -> None:
        location = ChunkLocation(uuid.uuid4(), 1, uuid.uuid4(), 1, 0)
        with pytest.raises(AttributeError):
            location.page_number = 2  # type: ignore[misc]


class TestLanguageDetection:
    def test_arabic_prose_is_arabic(self) -> None:
        assert detect_language("المادة الأولى يلتزم المكري بتسليم المحل") == LANGUAGE_ARABIC

    def test_arabic_quoting_french_party_names_is_still_arabic(self) -> None:
        # A real filing quotes French names and case references in Latin script
        # throughout, which is why the threshold is well below half.
        text = "المادة الأولى يلتزم المكري بتسليم المحل للشركة Societe Generale Maroc SA"
        assert detect_language(text) == LANGUAGE_ARABIC

    def test_french_is_told_from_english_by_its_diacritics(self) -> None:
        assert detect_language("Le bailleur loue au preneur les locaux désignés.") == (
            LANGUAGE_FRENCH
        )
        assert detect_language("The lessor leases the premises to the lessee.") == (
            LANGUAGE_ENGLISH
        )

    @pytest.mark.parametrize("text", ["", "   ", "1234 5678", "— 14 —", "..."])
    def test_text_with_no_letters_is_undetermined(self, text: str) -> None:
        # A wrong label would silently exclude a passage from a filtered search,
        # which is worse than no label.
        assert detect_language(text) == LANGUAGE_UNKNOWN

    def test_detection_is_deterministic(self) -> None:
        text = "Article 4 : Loyer et charges — le preneur s'acquitte du loyer."
        assert detect_language(text) == detect_language(text)

    def test_normalisation_form_does_not_change_the_answer(self) -> None:
        # "é" as one code point and as e + combining acute must agree, or the
        # same page would be labelled differently depending on the OCR host.
        assert detect_language("désignés") == detect_language(
            "désignés"
        )


class TestDominantLanguage:
    def test_the_most_common_language_wins(self) -> None:
        assert dominant_language(["fr", "fr", "ar"]) == "fr"

    def test_undetermined_chunks_are_excluded(self) -> None:
        # A document whose pages are mostly numeric tables should not be labelled
        # "undetermined" while its actual prose is French.
        assert dominant_language(["und", "und", "und", "fr"]) == "fr"

    def test_all_undetermined_is_none(self) -> None:
        assert dominant_language(["und", "und"]) is None

    def test_nothing_at_all_is_none(self) -> None:
        assert dominant_language([]) is None

    def test_ties_are_broken_reproducibly(self) -> None:
        assert dominant_language(["fr", "ar"]) == dominant_language(["ar", "fr"])


class TestChunkNormalisation:
    def test_leading_and_trailing_whitespace_is_removed(self) -> None:
        assert normalize_chunk_text("  Article 4 : Loyer.  \n\n") == "Article 4 : Loyer."

    def test_trailing_spaces_per_line_are_removed(self) -> None:
        assert normalize_chunk_text("Article 4 :   \nLoyer.  ") == "Article 4 :\nLoyer."

    def test_empty_input_is_empty_output(self) -> None:
        assert normalize_chunk_text("") == ""
        assert normalize_chunk_text("   \n  ") == ""

    def test_internal_structure_is_preserved(self) -> None:
        # The stored payload has to equal what was embedded, or a future search
        # result is not quotable.
        assert normalize_chunk_text("Un.\n\nDeux.") == "Un.\n\nDeux."


class TestIndexableChunks:
    def test_a_real_passage_is_indexable(self) -> None:
        assert is_indexable_chunk("Le bailleur loue au preneur les locaux désignés.")

    def test_a_fragment_below_the_floor_is_not(self) -> None:
        assert not is_indexable_chunk("a" * (MIN_CHUNK_CHARS - 1))

    def test_a_page_number_is_not_indexable_however_long(self) -> None:
        # A vector for "— 14 — 15 — 16 — 17 — 18 —" can only ever be noise.
        assert not is_indexable_chunk("— 14 — 15 — 16 — 17 — 18 — 19 — 20 — 21 —")

    def test_arabic_letters_count_as_letters(self) -> None:
        assert is_indexable_chunk("المادة الأولى يلتزم المكري بتسليم")


class TestErrorMessages:
    def test_whitespace_is_collapsed(self) -> None:
        assert normalize_error_message("  too    slow \n again ") == "too slow again"

    def test_blank_becomes_none(self) -> None:
        assert normalize_error_message("   ") is None
        assert normalize_error_message(None) is None

    def test_a_long_message_is_truncated(self) -> None:
        from core.indexing import MAX_ERROR_MESSAGE_LENGTH

        message = normalize_error_message("x" * 1000)
        assert message is not None
        assert len(message) == MAX_ERROR_MESSAGE_LENGTH


class TestSuccessRate:
    def test_it_counts_only_finished_runs(self) -> None:
        assert success_rate(indexed=3, failed=1) == 75.0

    def test_no_finished_runs_is_zero(self) -> None:
        assert success_rate(indexed=0, failed=0) == 0.0

    def test_it_is_rounded_to_two_decimals(self) -> None:
        assert success_rate(indexed=1, failed=2) == 33.33
