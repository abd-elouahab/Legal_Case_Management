"""User-domain utilities.

Small, pure helpers shared by the user schemas, the service layer, and the
provisioning script: normalizing the values that go into a user record and
composing the display name that comes out of one.

They live here rather than inside a Pydantic validator so the same rules apply
however a user is created — through the API, through ``scripts/create_user.py``,
or through a future import — and so they can be unit-tested without a request.
"""

from __future__ import annotations

import re

#: Longest phone number accepted, matching ``users.phone``.
MAX_PHONE_LENGTH = 32

#: Longest name part accepted, matching ``users.first_name`` / ``last_name``.
MAX_NAME_LENGTH = 100

#: Characters a phone number may contain: an optional leading ``+`` followed by
#: digits and the usual human separators. Deliberately permissive about *format*
#: (the platform is multi-country) while still rejecting free text — the digit
#: count is checked separately.
_PHONE_ALLOWED = re.compile(r"^\+?[0-9 ()./-]+$")

#: Practical bounds on the number of digits: shorter than 7 cannot be a real
#: subscriber number, longer than 15 exceeds E.164.
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15

_WHITESPACE = re.compile(r"\s+")


class InvalidPhoneNumberError(ValueError):
    """Raised when a phone number is not in an acceptable format."""


def normalize_name(value: str) -> str:
    """Trim a name and collapse internal runs of whitespace.

    Keeps ``"  Amina   Nour "`` and ``"Amina Nour"`` from being stored as two
    different people, which would also make search results depend on how the
    administrator happened to type the name.
    """
    return _WHITESPACE.sub(" ", value).strip()


def normalize_email(value: str) -> str:
    """Lowercase and trim an email so lookups are case-insensitive.

    The stored form: ``repositories.user`` normalizes the same way when querying,
    so a login and a record always agree.
    """
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    """Validate and normalize an optional phone number.

    Whitespace is collapsed and trimmed; the rest of the user's formatting is
    preserved, because a locally meaningful grouping is more useful to whoever
    dials the number than an aggressively canonicalized one.

    Raises:
        InvalidPhoneNumberError: the value contains characters that cannot be
            part of a phone number, or an implausible number of digits.
    """
    collapsed = _WHITESPACE.sub(" ", value).strip()

    if not _PHONE_ALLOWED.match(collapsed):
        raise InvalidPhoneNumberError(
            "Phone number may contain only digits, spaces, and the characters + ( ) - . /"
        )

    digits = sum(character.isdigit() for character in collapsed)
    if not MIN_PHONE_DIGITS <= digits <= MAX_PHONE_DIGITS:
        raise InvalidPhoneNumberError(
            f"Phone number must contain between {MIN_PHONE_DIGITS} and {MAX_PHONE_DIGITS} digits."
        )

    return collapsed


def compose_full_name(first_name: str, last_name: str) -> str:
    """Combine name parts into a display name.

    Mirrors :attr:`models.user.User.full_name`; used where no ORM instance is at
    hand (logs, the provisioning script).
    """
    return f"{first_name} {last_name}".strip()


def split_full_name(value: str) -> tuple[str, str]:
    """Split a single display name into first and last parts.

    For inputs that only carry one name field — the ``--name`` argument of
    ``scripts/create_user.py``. Splits on the first space, so ``"Amina Ben
    Salah"`` becomes ``("Amina", "Ben Salah")``; a single word yields an empty
    last name rather than duplicating the first.
    """
    normalized = normalize_name(value)
    first, separator, rest = normalized.partition(" ")
    return (first, rest if separator else "")
