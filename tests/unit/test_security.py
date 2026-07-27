"""Unit tests for password hashing and JWT handling (``core.security``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from core.config import settings
from core.security import (
    BCRYPT_MAX_PASSWORD_BYTES,
    InvalidTokenError,
    PasswordTooLongError,
    TokenExpiredError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

PASSWORD = "correct-horse-battery"


class TestPasswordHashing:
    def test_hash_is_bcrypt_and_not_the_plaintext(self) -> None:
        hashed = hash_password(PASSWORD)

        assert hashed != PASSWORD
        assert PASSWORD not in hashed
        assert hashed.startswith("$2b$")

    def test_verify_accepts_the_correct_password(self) -> None:
        assert verify_password(PASSWORD, hash_password(PASSWORD)) is True

    def test_verify_rejects_a_wrong_password(self) -> None:
        assert verify_password("not-the-password", hash_password(PASSWORD)) is False

    def test_each_hash_uses_a_fresh_salt(self) -> None:
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)

        assert first != second
        assert verify_password(PASSWORD, first)
        assert verify_password(PASSWORD, second)

    def test_verification_is_case_sensitive(self) -> None:
        assert verify_password(PASSWORD.upper(), hash_password(PASSWORD)) is False

    def test_hashing_rejects_passwords_bcrypt_would_truncate(self) -> None:
        # bcrypt ignores input past 72 bytes; truncating would make distinct
        # passwords interchangeable, so this must be an error, not a silent cut.
        with pytest.raises(PasswordTooLongError):
            hash_password("a" * (BCRYPT_MAX_PASSWORD_BYTES + 1))

    def test_byte_length_not_character_length_is_enforced(self) -> None:
        # 25 three-byte characters = 75 bytes, but only 25 characters.
        multibyte = "€" * 25
        assert len(multibyte) < BCRYPT_MAX_PASSWORD_BYTES
        assert len(multibyte.encode()) > BCRYPT_MAX_PASSWORD_BYTES

        with pytest.raises(PasswordTooLongError):
            hash_password(multibyte)

    def test_verify_returns_false_for_over_long_password(self) -> None:
        assert verify_password("a" * 200, hash_password(PASSWORD)) is False

    def test_verify_returns_false_for_a_malformed_hash(self) -> None:
        assert verify_password(PASSWORD, "not-a-bcrypt-hash") is False


class TestAccessAndRefreshTokens:
    def test_access_token_round_trips(self) -> None:
        issued = create_access_token("user-123")
        payload = decode_token(issued.token, expected_type=TokenType.ACCESS)

        assert payload.subject == "user-123"
        assert payload.token_type is TokenType.ACCESS
        assert payload.jti == issued.jti

    def test_refresh_token_round_trips(self) -> None:
        issued = create_refresh_token("user-123")
        payload = decode_token(issued.token, expected_type=TokenType.REFRESH)

        assert payload.subject == "user-123"
        assert payload.token_type is TokenType.REFRESH

    def test_each_token_gets_a_unique_identifier(self) -> None:
        assert create_access_token("user-123").jti != create_access_token("user-123").jti

    def test_access_token_expires_after_the_configured_window(self) -> None:
        issued = create_access_token("user-123")
        lifetime = issued.expires_at - datetime.now(UTC)

        assert lifetime <= settings.access_token_ttl
        assert lifetime > settings.access_token_ttl - timedelta(seconds=30)

    def test_refresh_token_expires_after_the_configured_window(self) -> None:
        issued = create_refresh_token("user-123")
        lifetime = issued.expires_at - datetime.now(UTC)

        assert lifetime <= settings.refresh_token_ttl
        assert lifetime > settings.refresh_token_ttl - timedelta(seconds=30)

    def test_a_refresh_token_cannot_be_used_as_an_access_token(self) -> None:
        refresh = create_refresh_token("user-123")

        with pytest.raises(InvalidTokenError):
            decode_token(refresh.token, expected_type=TokenType.ACCESS)

    def test_an_access_token_cannot_be_used_as_a_refresh_token(self) -> None:
        access = create_access_token("user-123")

        with pytest.raises(InvalidTokenError):
            decode_token(access.token, expected_type=TokenType.REFRESH)


class TestTokenRejection:
    def test_expired_token_raises_token_expired(self) -> None:
        past = datetime.now(UTC) - timedelta(minutes=5)
        expired = jwt.encode(
            {
                "sub": "user-123",
                "type": "access",
                "jti": "expired-jti",
                "iat": past - timedelta(minutes=1),
                "exp": past,
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        with pytest.raises(TokenExpiredError):
            decode_token(expired, expected_type=TokenType.ACCESS)

    def test_token_signed_with_another_secret_is_rejected(self) -> None:
        forged = jwt.encode(
            {
                "sub": "user-123",
                "type": "access",
                "jti": "forged",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
            },
            "an-entirely-different-signing-secret",
            algorithm=settings.JWT_ALGORITHM,
        )

        with pytest.raises(InvalidTokenError):
            decode_token(forged, expected_type=TokenType.ACCESS)

    def test_tampered_token_is_rejected(self) -> None:
        issued = create_access_token("user-123")
        header, payload, signature = issued.token.split(".")
        tampered = f"{header}.{payload}x.{signature}"

        with pytest.raises(InvalidTokenError):
            decode_token(tampered, expected_type=TokenType.ACCESS)

    @pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "Bearer something"])
    def test_malformed_tokens_are_rejected(self, garbage: str) -> None:
        with pytest.raises(InvalidTokenError):
            decode_token(garbage, expected_type=TokenType.ACCESS)

    def test_token_for_another_audience_is_rejected(self) -> None:
        wrong_audience = jwt.encode(
            {
                "sub": "user-123",
                "type": "access",
                "jti": "wrong-aud",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "iss": settings.JWT_ISSUER,
                "aud": "some-other-service",
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        with pytest.raises(InvalidTokenError):
            decode_token(wrong_audience, expected_type=TokenType.ACCESS)

    def test_token_from_another_issuer_is_rejected(self) -> None:
        wrong_issuer = jwt.encode(
            {
                "sub": "user-123",
                "type": "access",
                "jti": "wrong-iss",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "iss": "some-other-issuer",
                "aud": settings.JWT_AUDIENCE,
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        with pytest.raises(InvalidTokenError):
            decode_token(wrong_issuer, expected_type=TokenType.ACCESS)

    def test_token_without_a_subject_is_rejected(self) -> None:
        no_subject = jwt.encode(
            {
                "type": "access",
                "jti": "no-sub",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        with pytest.raises(InvalidTokenError):
            decode_token(no_subject, expected_type=TokenType.ACCESS)

    def test_token_without_a_type_claim_is_rejected(self) -> None:
        no_type = jwt.encode(
            {
                "sub": "user-123",
                "jti": "no-type",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        with pytest.raises(InvalidTokenError):
            decode_token(no_type, expected_type=TokenType.ACCESS)
