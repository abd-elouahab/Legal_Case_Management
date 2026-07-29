"""Shared password policy.

Extracted so the two schema modules that set a password — ``schemas.auth`` (a
user changing their own) and ``schemas.user`` (an administrator provisioning or
resetting one) — enforce the *same* rules from one definition. It lives in its
own module because ``schemas.auth`` imports ``UserRead`` from ``schemas.user``,
so either importing the other would be a cycle.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from core.security import BCRYPT_MAX_PASSWORD_BYTES

#: Minimum password length. Applies to passwords being *set*; login only checks
#: presence, so an existing (possibly shorter, legacy) password can still sign in.
MIN_PASSWORD_LENGTH = 8


def validate_password_bytes(value: str) -> str:
    """Reject passwords bcrypt cannot hash without silently truncating them.

    ``max_length`` counts characters, but bcrypt's limit is 72 *bytes*, so a
    short string of multi-byte characters can still exceed it. Truncation would
    make two different passwords equivalent, so it is refused rather than hidden.
    """
    if len(value.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must not exceed {BCRYPT_MAX_PASSWORD_BYTES} bytes when UTF-8 encoded.")
    return value


#: A password being *set* — length-checked at both ends.
NewPassword = Annotated[
    str,
    Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description=f"At least {MIN_PASSWORD_LENGTH} characters.",
    ),
]
