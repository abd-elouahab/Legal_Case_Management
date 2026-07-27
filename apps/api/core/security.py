"""Security primitives: password hashing and JWT signing/verification.

This module is intentionally free of business logic and of any database or
framework dependency — it only knows how to turn a password into a hash and a
subject into a signed token (and back). ``services/auth.py`` composes these
primitives into the authentication workflow.

Password hashing uses **bcrypt** directly. ``passlib`` is deliberately not used:
version 1.7.4 (its last release, 2020) is incompatible with modern ``bcrypt``
releases — its backend probe hashes a >72-byte password, which ``bcrypt`` 5.x
rejects with ``ValueError``. Calling bcrypt directly keeps the dependency
current and the failure modes explicit.
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import bcrypt
from jose import JWTError, jwt

from core.config import settings

# bcrypt hashes at most 72 bytes of input and silently ignores anything beyond
# that, so longer passwords must be rejected rather than truncated (truncation
# would make two different passwords equivalent).
BCRYPT_MAX_PASSWORD_BYTES: Final[int] = 72


class TokenType(StrEnum):
    """Discriminates the two token kinds so one can never be used as the other."""

    ACCESS = "access"
    REFRESH = "refresh"


class PasswordTooLongError(ValueError):
    """Raised when a password exceeds what bcrypt can hash without truncation."""


class TokenError(Exception):
    """Base class for token decoding failures."""


class TokenExpiredError(TokenError):
    """The token is well-formed and correctly signed but past its expiry."""


class InvalidTokenError(TokenError):
    """The token is malformed, has a bad signature, or carries wrong claims."""


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Validated claims extracted from a JWT."""

    subject: str
    token_type: TokenType
    jti: str
    issued_at: datetime
    expires_at: datetime
    #: Session generation the token was minted under. A token is rejected once the
    #: user's generation moves past it, which is how sessions are revoked in bulk.
    session_generation: int


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly minted JWT together with the metadata needed to track it."""

    token: str
    jti: str
    expires_at: datetime


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def _password_bytes(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"Password must not exceed {BCRYPT_MAX_PASSWORD_BYTES} bytes when UTF-8 encoded."
        )
    return encoded


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt (per-hash random salt).

    Raises:
        PasswordTooLongError: if the password exceeds bcrypt's 72-byte input limit.
    """
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(_password_bytes(password), salt).decode("ascii")


def verify_password(password: str, hashed_password: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash.

    Returns ``False`` (never raises) for over-long passwords or malformed
    hashes, so callers can treat any failure as "credentials do not match".
    """
    try:
        candidate = _password_bytes(password)
    except PasswordTooLongError:
        return False

    try:
        # bcrypt.checkpw performs its own constant-time comparison.
        return bcrypt.checkpw(candidate, hashed_password.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed/unknown hash format — treat as a failed verification.
        return False


def hashes_equal(left: str, right: str) -> bool:
    """Constant-time comparison of two hash strings."""
    return hmac.compare_digest(left, right)


# --------------------------------------------------------------------------- #
# JSON Web Tokens
# --------------------------------------------------------------------------- #


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_in: timedelta,
    session_generation: int,
) -> IssuedToken:
    """Sign a JWT for ``subject`` with a unique ``jti`` and bounded lifetime."""
    issued_at = datetime.now(UTC)
    expires_at = issued_at + expires_in
    jti = str(uuid.uuid4())

    claims: dict[str, Any] = {
        "sub": subject,
        "type": token_type.value,
        "jti": jti,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": expires_at,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        # Private claim: the session generation this token belongs to.
        "sgen": session_generation,
    }
    token = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


def create_access_token(subject: str, session_generation: int = 0) -> IssuedToken:
    """Mint a short-lived access token (used as a Bearer credential)."""
    return _create_token(subject, TokenType.ACCESS, settings.access_token_ttl, session_generation)


def create_refresh_token(subject: str, session_generation: int = 0) -> IssuedToken:
    """Mint a long-lived refresh token (used only to obtain new access tokens)."""
    return _create_token(subject, TokenType.REFRESH, settings.refresh_token_ttl, session_generation)


def decode_token(token: str, *, expected_type: TokenType) -> TokenPayload:
    """Verify a JWT's signature, claims, and type, and return its payload.

    Signature, expiry, issuer, and audience are all verified. The ``type`` claim
    is checked so a refresh token cannot be presented as an access token (or the
    reverse).

    Raises:
        TokenExpiredError: the token has expired.
        InvalidTokenError: bad signature, malformed token, or unexpected claims.
    """
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
    except JWTError as exc:
        # python-jose raises ExpiredSignatureError (a JWTError subclass) for
        # expiry; match on the message-independent class name to keep the
        # distinction without importing a private symbol.
        if type(exc).__name__ == "ExpiredSignatureError":
            raise TokenExpiredError("Token has expired.") from exc
        raise InvalidTokenError("Token is invalid.") from exc

    subject = claims.get("sub")
    jti = claims.get("jti")
    raw_type = claims.get("type")
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")

    if not isinstance(subject, str) or not subject:
        raise InvalidTokenError("Token is missing a subject.")
    if not isinstance(jti, str) or not jti:
        raise InvalidTokenError("Token is missing an identifier.")
    if raw_type != expected_type.value:
        raise InvalidTokenError("Token is not valid for this operation.")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise InvalidTokenError("Token has invalid timestamps.")

    # A missing `sgen` is read as generation 0, so tokens minted before this claim
    # existed keep working against users still on the default generation.
    session_generation = claims.get("sgen", 0)
    if not isinstance(session_generation, int) or isinstance(session_generation, bool):
        raise InvalidTokenError("Token has an invalid session generation.")

    return TokenPayload(
        subject=subject,
        token_type=expected_type,
        jti=jti,
        issued_at=datetime.fromtimestamp(issued_at, tz=UTC),
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
        session_generation=session_generation,
    )
