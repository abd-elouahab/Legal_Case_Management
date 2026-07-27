"""Shared helpers for the backend test suite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jose import jwt

from core.config import settings
from core.security import TokenType


def forge_token(
    subject: str,
    *,
    token_type: TokenType = TokenType.ACCESS,
    expires_in: timedelta = timedelta(minutes=5),
    jti: str = "forged-jti",
    secret: str | None = None,
    issuer: str | None = None,
    audience: str | None = None,
) -> str:
    """Sign a token with arbitrary claims, for testing rejection paths.

    Defaults produce a valid token; override a single argument to test one
    specific failure (expired, wrong audience, wrong secret, …).
    """
    issued_at = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "type": token_type.value,
            "jti": jti,
            "iat": issued_at,
            "nbf": issued_at,
            "exp": issued_at + expires_in,
            "iss": settings.JWT_ISSUER if issuer is None else issuer,
            "aud": settings.JWT_AUDIENCE if audience is None else audience,
        },
        settings.JWT_SECRET_KEY if secret is None else secret,
        algorithm=settings.JWT_ALGORITHM,
    )


def expired_access_token(subject: str) -> str:
    """An access token that expired five minutes ago."""
    return forge_token(subject, token_type=TokenType.ACCESS, expires_in=timedelta(minutes=-5))


def expired_refresh_token(subject: str) -> str:
    """A refresh token that expired five minutes ago."""
    return forge_token(subject, token_type=TokenType.REFRESH, expires_in=timedelta(minutes=-5))
