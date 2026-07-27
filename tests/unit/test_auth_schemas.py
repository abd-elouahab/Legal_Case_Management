"""Unit tests for authentication request validation (``schemas.auth``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.auth import MIN_PASSWORD_LENGTH, ChangePasswordRequest, LoginRequest, RefreshRequest


class TestLoginRequest:
    def test_accepts_valid_credentials(self) -> None:
        request = LoginRequest(email="amina@example.com", password="a-password")

        assert request.email == "amina@example.com"
        assert request.password == "a-password"

    @pytest.mark.parametrize(
        ("submitted", "expected"),
        [
            ("Amina@Example.COM", "amina@example.com"),
            ("  amina@example.com  ", "amina@example.com"),
            ("AMINA@EXAMPLE.COM", "amina@example.com"),
        ],
    )
    def test_normalizes_email_so_login_is_case_insensitive(self, submitted: str, expected: str) -> None:
        assert LoginRequest(email=submitted, password="a-password").email == expected

    @pytest.mark.parametrize(
        "invalid_email",
        ["not-an-email", "missing@tld", "@example.com", "spaces in@example.com", ""],
    )
    def test_rejects_malformed_email(self, invalid_email: str) -> None:
        with pytest.raises(ValidationError) as error:
            LoginRequest(email=invalid_email, password="a-password")

        assert "email" in str(error.value)

    def test_rejects_missing_email(self) -> None:
        with pytest.raises(ValidationError):
            LoginRequest(password="a-password")  # type: ignore[call-arg]

    def test_rejects_missing_password(self) -> None:
        with pytest.raises(ValidationError):
            LoginRequest(email="amina@example.com")  # type: ignore[call-arg]

    def test_rejects_empty_password(self) -> None:
        with pytest.raises(ValidationError):
            LoginRequest(email="amina@example.com", password="")

    def test_does_not_enforce_a_minimum_length_at_login(self) -> None:
        # Enforcing the policy here would leak it and block legacy passwords;
        # only presence is required.
        assert LoginRequest(email="amina@example.com", password="x").password == "x"

    def test_rejects_unexpected_fields(self) -> None:
        with pytest.raises(ValidationError):
            LoginRequest(email="amina@example.com", password="a-password", role="administrator")  # type: ignore[call-arg]


class TestChangePasswordRequest:
    def test_accepts_a_valid_change(self) -> None:
        request = ChangePasswordRequest(current_password="old-password", new_password="brand-new-password")

        assert request.new_password == "brand-new-password"

    def test_rejects_a_new_password_below_the_minimum_length(self) -> None:
        with pytest.raises(ValidationError) as error:
            ChangePasswordRequest(current_password="old-password", new_password="a" * (MIN_PASSWORD_LENGTH - 1))

        assert "new_password" in str(error.value)

    def test_accepts_a_new_password_at_exactly_the_minimum_length(self) -> None:
        request = ChangePasswordRequest(
            current_password="old-password",
            new_password="a" * MIN_PASSWORD_LENGTH,
        )

        assert len(request.new_password) == MIN_PASSWORD_LENGTH

    def test_rejects_reusing_the_current_password(self) -> None:
        with pytest.raises(ValidationError) as error:
            ChangePasswordRequest(current_password="same-password", new_password="same-password")

        assert "different" in str(error.value)

    def test_rejects_a_new_password_bcrypt_cannot_hash(self) -> None:
        # 25 three-byte characters exceeds bcrypt's 72-byte input limit while
        # staying under any character-count check.
        with pytest.raises(ValidationError) as error:
            ChangePasswordRequest(current_password="old-password", new_password="€" * 25)

        assert "72 bytes" in str(error.value)

    def test_rejects_missing_current_password(self) -> None:
        with pytest.raises(ValidationError):
            ChangePasswordRequest(new_password="brand-new-password")  # type: ignore[call-arg]

    def test_rejects_empty_current_password(self) -> None:
        with pytest.raises(ValidationError):
            ChangePasswordRequest(current_password="", new_password="brand-new-password")


class TestRefreshRequest:
    def test_token_is_optional_so_cookie_clients_can_send_an_empty_body(self) -> None:
        assert RefreshRequest().refresh_token is None

    def test_accepts_an_explicit_token(self) -> None:
        assert RefreshRequest(refresh_token="a.b.c").refresh_token == "a.b.c"

    def test_rejects_unexpected_fields(self) -> None:
        with pytest.raises(ValidationError):
            RefreshRequest(access_token="a.b.c")  # type: ignore[call-arg]
