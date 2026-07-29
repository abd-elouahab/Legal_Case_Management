"""Case-domain utilities.

Small, pure helpers shared by the case schemas, the repository, and the service:
the lifecycle rules, the priority ordering, and case-number formatting.

They live here rather than inside a service method for the same reason
:mod:`core.users` exists — the same rules must apply however a case is created or
changed, and they can be unit-tested without a database or a request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from models.case import CasePriority, CaseStatus

#: Longest ``cases.case_number`` accepted, matching the column.
MAX_CASE_NUMBER_LENGTH = 50

#: Longest ``cases.title``, matching the column.
MAX_TITLE_LENGTH = 255

#: Longest ``cases.category``, matching the column.
MAX_CATEGORY_LENGTH = 100

#: Longest ``cases.court_name``, matching the column.
MAX_COURT_NAME_LENGTH = 255

#: Longest description accepted. The column is unbounded ``Text``; the ceiling
#: exists so a single request cannot be used to store an arbitrary payload.
MAX_DESCRIPTION_LENGTH = 10_000

#: Prefix of a generated case number.
CASE_NUMBER_PREFIX = "CASE"

#: Digits in the sequence part of a generated case number. Four gives 9 999
#: cases per year before the width grows, which it does gracefully — the format
#: is a minimum width, not a fixed one, so overflow lengthens rather than wraps.
CASE_NUMBER_SEQUENCE_DIGITS = 4

#: Matches a generated case number and captures its sequence, so the next one can
#: be derived from the highest already issued. Deliberately anchored: a manually
#: entered number in some other format must not be mistaken for a sequence.
_GENERATED_CASE_NUMBER = re.compile(
    rf"^{CASE_NUMBER_PREFIX}-(?P<year>\d{{4}})-(?P<sequence>\d+)$"
)

#: Characters a client-supplied case number may contain: letters, digits, and the
#: separators registries actually use. Permissive about *format* (every court
#: numbers its cases differently) while still rejecting free text and anything
#: that would make the identifier ambiguous in a URL or a search term.
_CASE_NUMBER_ALLOWED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._-]*$")

_WHITESPACE = re.compile(r"\s+")


class InvalidCaseNumberError(ValueError):
    """Raised when a case number is not in an acceptable format."""


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

#: Which status may follow which.
#:
#: The spec asks to "support status transitions" and "prevent invalid transitions
#: where appropriate", so the rules encode the shape of a real matter rather than
#: forbidding everything unfamiliar:
#:
#: * A draft has not been filed, so it can only be opened or abandoned
#:   (archived). Nothing returns *to* draft — a case that has been opened was
#:   filed, and pretending otherwise would falsify the record.
#: * The three working states move freely between one another, because a matter
#:   genuinely does go back and forth between active work and waiting for a
#:   hearing.
#: * Closed is reversible (a matter can return), archived is reachable from
#:   everywhere (anything can be shelved), and an archived case can be restored
#:   to open — the alternative is an irreversible operation triggered by a single
#:   ``DELETE``.
#:
#: Staying in the current status is always permitted and is not looked up here;
#: see :func:`can_transition`.
STATUS_TRANSITIONS: Mapping[CaseStatus, frozenset[CaseStatus]] = MappingProxyType(
    {
        CaseStatus.DRAFT: frozenset({CaseStatus.OPEN, CaseStatus.ARCHIVED}),
        CaseStatus.OPEN: frozenset(
            {
                CaseStatus.IN_PROGRESS,
                CaseStatus.WAITING_FOR_HEARING,
                CaseStatus.CLOSED,
                CaseStatus.ARCHIVED,
            }
        ),
        CaseStatus.IN_PROGRESS: frozenset(
            {
                CaseStatus.OPEN,
                CaseStatus.WAITING_FOR_HEARING,
                CaseStatus.CLOSED,
                CaseStatus.ARCHIVED,
            }
        ),
        CaseStatus.WAITING_FOR_HEARING: frozenset(
            {
                CaseStatus.OPEN,
                CaseStatus.IN_PROGRESS,
                CaseStatus.CLOSED,
                CaseStatus.ARCHIVED,
            }
        ),
        CaseStatus.CLOSED: frozenset({CaseStatus.OPEN, CaseStatus.ARCHIVED}),
        # Restoring an archived case returns it to the active workload; which of
        # the working states it belongs in is then an ordinary status change.
        CaseStatus.ARCHIVED: frozenset({CaseStatus.OPEN}),
    }
)


def can_transition(current: CaseStatus, target: CaseStatus) -> bool:
    """Whether a case may move from ``current`` to ``target``.

    Re-submitting the current status is always allowed: a form that round-trips
    every field is not requesting a transition, and rejecting it would make an
    unrelated edit fail for no reason.
    """
    if current is target:
        return True
    return target in STATUS_TRANSITIONS[current]


def allowed_transitions(current: CaseStatus) -> frozenset[CaseStatus]:
    """The statuses a case in ``current`` may move to, excluding itself."""
    return STATUS_TRANSITIONS[current]


# --------------------------------------------------------------------------- #
# Priority
# --------------------------------------------------------------------------- #

#: Sort weight per priority, lowest to highest.
#:
#: Sorting on the stored value would order alphabetically — high, low, medium,
#: urgent — which is meaningless. The rank is defined once, here, and the
#: repository builds its ORDER BY from it, so the API and any future report agree.
PRIORITY_RANK: Mapping[CasePriority, int] = MappingProxyType(
    {
        CasePriority.LOW: 1,
        CasePriority.MEDIUM: 2,
        CasePriority.HIGH: 3,
        CasePriority.URGENT: 4,
    }
)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def normalize_text(value: str) -> str:
    """Trim a single-line field and collapse internal runs of whitespace.

    Keeps ``"Benali  v.  State "`` and ``"Benali v. State"`` from being stored as
    two different titles, which would also make search results depend on how the
    administrator happened to type them.
    """
    return _WHITESPACE.sub(" ", value).strip()


def normalize_description(value: str) -> str:
    """Trim a multi-line field without collapsing its line breaks.

    Unlike :func:`normalize_text`: a description is prose, and flattening its
    paragraphs would destroy the author's structure.
    """
    return value.strip()


def normalize_case_number(value: str) -> str:
    """Validate and normalize a case number.

    Uppercased so that ``case-2026-0001`` and ``CASE-2026-0001`` cannot exist as
    two separate cases — the identifier is quoted in correspondence, where case is
    not preserved reliably.

    Raises:
        InvalidCaseNumberError: the value is blank, too long, or contains
            characters that cannot be part of an identifier.
    """
    normalized = _WHITESPACE.sub("", value).strip().upper()

    if not normalized:
        raise InvalidCaseNumberError("Case number must not be blank.")

    if len(normalized) > MAX_CASE_NUMBER_LENGTH:
        raise InvalidCaseNumberError(
            f"Case number must be at most {MAX_CASE_NUMBER_LENGTH} characters."
        )

    if not _CASE_NUMBER_ALLOWED.match(normalized):
        raise InvalidCaseNumberError(
            "Case number may contain only letters, digits, and the characters - _ . /"
        )

    return normalized


# --------------------------------------------------------------------------- #
# Case-number generation
# --------------------------------------------------------------------------- #


def build_case_number(year: int, sequence: int) -> str:
    """Format a generated case number, e.g. ``CASE-2026-0007``.

    Zero-padded so that numbers sort lexicographically in the same order they
    were issued — which is what makes "sort by case number" in the API match what
    a reader expects, without a second numeric column.
    """
    return f"{CASE_NUMBER_PREFIX}-{year:04d}-{sequence:0{CASE_NUMBER_SEQUENCE_DIGITS}d}"


def case_number_sequence(case_number: str, *, year: int) -> int | None:
    """Extract the sequence from a generated case number issued in ``year``.

    Returns ``None`` for anything else — a manually entered registry number must
    not influence the generated series, or one court's numbering scheme would
    silently reset the platform's.
    """
    match = _GENERATED_CASE_NUMBER.match(case_number)
    if match is None or int(match.group("year")) != year:
        return None
    return int(match.group("sequence"))
