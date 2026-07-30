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


# --------------------------------------------------------------------------- #
# Document fixtures
#
# Real bytes rather than `b"x"`: the upload validator checks each format's
# leading signature, so a placeholder payload would be rejected as a corrupted
# upload — which is precisely the rule these give the tests something to prove.
# They live here rather than in `conftest.py` because pytest loads that module as
# top-level `conftest`, so importing from it under its package path creates a
# second copy of everything it defines.
# --------------------------------------------------------------------------- #

#: A minimal, structurally valid PDF.
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"

#: A PNG signature followed by filler.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

#: A JPEG signature followed by filler.
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32

#: An OOXML package, i.e. a ZIP archive — what a .docx actually is.
DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 32

#: Plain text, which has no signature and is validated negatively.
TXT_BYTES = "Procès-verbal d'audience.\n".encode()
