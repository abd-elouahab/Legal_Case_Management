"""Unit tests for the user-domain utilities.

Pure functions, so these need neither a database nor a request.
"""

from __future__ import annotations

import pytest

from core.users import (
    InvalidPhoneNumberError,
    compose_full_name,
    normalize_email,
    normalize_name,
    normalize_phone,
    split_full_name,
)


class TestNormalizeName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Amina  ", "Amina"),
            ("Amina   Nour", "Amina Nour"),
            ("\tBen\nSalah ", "Ben Salah"),
        ],
    )
    def test_trims_and_collapses_whitespace(self, raw: str, expected: str) -> None:
        # Otherwise "Amina  Nour" and "Amina Nour" become two different people,
        # and search results depend on how the administrator happened to type.
        assert normalize_name(raw) == expected

    def test_a_blank_name_normalizes_to_empty(self) -> None:
        assert normalize_name("   ") == ""


class TestNormalizeEmail:
    def test_lowercases_and_trims(self) -> None:
        assert normalize_email("  Amina.Benali@Example.COM ") == "amina.benali@example.com"


class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw",
        [
            "+212 6 12 34 56 78",
            "0612345678",
            "+33 (0)1 23 45 67",
            "212-612-345678",
            "+1 555.123.4567",
        ],
    )
    def test_accepts_common_international_formats(self, raw: str) -> None:
        assert normalize_phone(raw)

    def test_collapses_whitespace_but_keeps_the_grouping(self) -> None:
        # A locally meaningful grouping is more useful to whoever dials the
        # number than an aggressively canonicalized string.
        assert normalize_phone("  +212   6 12 34 56 78 ") == "+212 6 12 34 56 78"

    @pytest.mark.parametrize(
        "raw",
        [
            "not a phone",
            "+212 612 345 678 ext 9",
            "<script>alert(1)</script>",
            "061234",  # too few digits
            "+1234567890123456",  # more digits than E.164 allows
        ],
    )
    def test_rejects_values_that_are_not_phone_numbers(self, raw: str) -> None:
        with pytest.raises(InvalidPhoneNumberError):
            normalize_phone(raw)


class TestFullName:
    def test_composes_the_display_name(self) -> None:
        assert compose_full_name("Amina", "Benali") == "Amina Benali"

    def test_composition_tolerates_a_missing_last_name(self) -> None:
        assert compose_full_name("Amina", "") == "Amina"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Amina Benali", ("Amina", "Benali")),
            ("Amina Ben Salah", ("Amina", "Ben Salah")),
            ("Amina", ("Amina", "")),
            ("  Amina   Benali  ", ("Amina", "Benali")),
        ],
    )
    def test_splits_on_the_first_space(self, raw: str, expected: tuple[str, str]) -> None:
        assert split_full_name(raw) == expected

    def test_split_and_compose_round_trip(self) -> None:
        assert compose_full_name(*split_full_name("Amina Ben Salah")) == "Amina Ben Salah"
