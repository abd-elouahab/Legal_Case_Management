"""Timeline-domain utilities.

Small, pure helpers shared by the timeline schemas, repository, and service: the
event registry's presentation (category and default title), and the
normalisation applied to every recorded event's text and metadata.

They live here rather than inside the service for the same reason
:mod:`core.cases`, :mod:`core.users`, and :mod:`core.documents` exist — the rules
must apply however an event arrives, and they can be unit-tested without a
database or a request.

**This module is the whole of the timeline's "knowledge".** It maps an event type
onto a category and a headline and nothing more; it knows nothing about cases,
documents, or what any of those events *mean*. That is what keeps the timeline
generic: a future module publishes by adding one registry member and one line in
each mapping below, and no other timeline code changes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any

from models.timeline import TimelineEventCategory, TimelineEventType

#: Longest event title retained, matching the column. Titles are headlines
#: ("Document Uploaded"), not sentences.
MAX_TITLE_LENGTH = 200

#: Longest description accepted. The column is unbounded ``Text``; the ceiling
#: exists so publishing an event cannot be used to store an arbitrary payload.
MAX_DESCRIPTION_LENGTH = 1_000

#: Longest actor name retained, matching the column and ``users.first_name`` +
#: ``last_name`` at their combined maximum.
MAX_ACTOR_NAME_LENGTH = 255

#: Longest serialized ``metadata`` object accepted, in bytes.
#:
#: The column is schemaless by design, which means nothing else bounds what a
#: publisher can put in it. A cap keeps one event from growing without limit while
#: leaving room for far more than any event the platform records needs.
MAX_METADATA_BYTES = 8_192

#: Deepest nesting normalised inside ``metadata``. Beyond this the value is
#: rendered as text, which bounds the work a single event can cost and makes a
#: cyclic structure impossible to construct.
MAX_METADATA_DEPTH = 4


class InvalidTimelineMetadataError(ValueError):
    """Raised when an event's metadata cannot be stored as it stands."""


# --------------------------------------------------------------------------- #
# The event registry's presentation
# --------------------------------------------------------------------------- #

#: Event type → the family it belongs to, which decides its icon.
#:
#: ``08-timeline.md`` groups the icons as case (folder), document (file),
#: assignment (user), status (refresh), and priority (flag). Deriving the category
#: rather than storing it means an event can never be filed under a family that
#: disagrees with its type.
EVENT_CATEGORIES: Mapping[TimelineEventType, TimelineEventCategory] = MappingProxyType(
    {
        TimelineEventType.CASE_CREATED: TimelineEventCategory.CASE,
        TimelineEventType.CASE_UPDATED: TimelineEventCategory.CASE,
        TimelineEventType.CASE_ARCHIVED: TimelineEventCategory.CASE,
        TimelineEventType.CASE_RESTORED: TimelineEventCategory.CASE,
        TimelineEventType.STATUS_CHANGED: TimelineEventCategory.STATUS,
        TimelineEventType.PRIORITY_CHANGED: TimelineEventCategory.PRIORITY,
        TimelineEventType.LAWYER_ASSIGNED: TimelineEventCategory.ASSIGNMENT,
        TimelineEventType.LAWYER_REMOVED: TimelineEventCategory.ASSIGNMENT,
        TimelineEventType.REPRESENTATIVE_ASSIGNED: TimelineEventCategory.ASSIGNMENT,
        TimelineEventType.REPRESENTATIVE_REMOVED: TimelineEventCategory.ASSIGNMENT,
        TimelineEventType.DOCUMENT_UPLOADED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.DOCUMENT_UPDATED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.DOCUMENT_REPLACED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.DOCUMENT_DELETED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.DOCUMENT_DOWNLOADED: TimelineEventCategory.DOCUMENT,
        # OCR happens *to* a document, so it belongs to the document family and
        # reuses its icon. A sixth category would have meant a new icon, a new
        # filter option, and a change to the timeline's presentation — the exact
        # "modifying the Timeline implementation" the spec asks publishers to
        # avoid, in exchange for nothing a reader needs.
        TimelineEventType.OCR_STARTED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.OCR_COMPLETED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.OCR_FAILED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.OCR_RETRIED: TimelineEventCategory.DOCUMENT,
        # Indexing, for the same reason: making a document searchable is
        # something that happened to that document.
        TimelineEventType.INDEXING_STARTED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.INDEXING_COMPLETED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.INDEXING_FAILED: TimelineEventCategory.DOCUMENT,
        TimelineEventType.INDEXING_RETRIED: TimelineEventCategory.DOCUMENT,
    }
)

#: Event type → the headline shown above the description.
#:
#: A default rather than a requirement: a publisher may pass its own title when
#: the type is too coarse to say what happened. Having one here means the common
#: case does not restate the obvious at every call site, and that two modules
#: recording the same kind of event cannot phrase its headline differently.
DEFAULT_TITLES: Mapping[TimelineEventType, str] = MappingProxyType(
    {
        TimelineEventType.CASE_CREATED: "Case Created",
        TimelineEventType.CASE_UPDATED: "Case Updated",
        TimelineEventType.CASE_ARCHIVED: "Case Archived",
        TimelineEventType.CASE_RESTORED: "Case Restored",
        TimelineEventType.STATUS_CHANGED: "Status Changed",
        TimelineEventType.PRIORITY_CHANGED: "Priority Changed",
        TimelineEventType.LAWYER_ASSIGNED: "Lawyer Assigned",
        TimelineEventType.LAWYER_REMOVED: "Lawyer Removed",
        TimelineEventType.REPRESENTATIVE_ASSIGNED: "Representative Assigned",
        TimelineEventType.REPRESENTATIVE_REMOVED: "Representative Removed",
        TimelineEventType.DOCUMENT_UPLOADED: "Document Uploaded",
        TimelineEventType.DOCUMENT_UPDATED: "Document Updated",
        TimelineEventType.DOCUMENT_REPLACED: "Document Replaced",
        TimelineEventType.DOCUMENT_DELETED: "Document Deleted",
        TimelineEventType.DOCUMENT_DOWNLOADED: "Document Downloaded",
        # "Text Extraction" rather than "OCR": the timeline is read by lawyers
        # and court staff, and a headline in a case history should not be an
        # acronym from the platform's implementation.
        TimelineEventType.OCR_STARTED: "Text Extraction Started",
        TimelineEventType.OCR_COMPLETED: "Text Extraction Completed",
        TimelineEventType.OCR_FAILED: "Text Extraction Failed",
        TimelineEventType.OCR_RETRIED: "Text Extraction Retried",
        # "Search Indexing" rather than "Embedding" or "Vectorization", for the
        # same reason: a case history is read by lawyers, and the headline should
        # name the capability it gives them rather than the machinery behind it.
        TimelineEventType.INDEXING_STARTED: "Search Indexing Started",
        TimelineEventType.INDEXING_COMPLETED: "Search Indexing Completed",
        TimelineEventType.INDEXING_FAILED: "Search Indexing Failed",
        TimelineEventType.INDEXING_RETRIED: "Search Indexing Requested",
    }
)


def category_for(event_type: str) -> TimelineEventCategory:
    """The family an event type belongs to.

    Tolerant of an unknown identifier rather than raising: this is read on the way
    *out* of the database, and a stored row written by a future version of the
    platform must still render. An unrecognised type is presented as a plain case
    event, which is the neutral choice — it gets a generic icon rather than one
    that misrepresents it.
    """
    try:
        return EVENT_CATEGORIES[TimelineEventType(event_type)]
    except (KeyError, ValueError):
        return TimelineEventCategory.CASE


def humanize(value: str) -> str:
    """Render a snake_case identifier as a readable phrase.

    ``in_progress`` → "In progress", ``court_decision`` → "Court decision". Used
    for the enum values an event description quotes — a status, a priority, a
    category — so a timeline entry never shows a user a database value.

    Only the first word is capitalised, because these appear *inside* sentences
    ("changed the status from Open to In progress"); title-casing every word
    would make a phrase read like a heading.
    """
    words = value.replace("_", " ").split()
    if not words:
        return ""
    return " ".join(words).capitalize()


def default_title(event_type: str) -> str:
    """The headline for an event type.

    Falls back to a title-cased form of the identifier (``court_hearing_added`` →
    "Court Hearing Added"), so an event type added to the registry without a
    title entry still reads as English rather than as a database value. Titles
    *are* headings, so unlike :func:`humanize` every word is capitalised.
    """
    try:
        return DEFAULT_TITLES[TimelineEventType(event_type)]
    except (KeyError, ValueError):
        return event_type.replace("_", " ").strip().title() or "Event"


def known_event_types() -> list[str]:
    """Every event type in the registry, in declaration order.

    Declaration order groups case, assignment, and document events the way
    ``08-timeline.md`` lists them, which is what a filter dropdown should show.
    """
    return [event_type.value for event_type in TimelineEventType]


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def normalize_title(value: str) -> str:
    """Collapse a title's whitespace and truncate it to the column width.

    Truncated rather than rejected: a title is a label, and refusing to record an
    event because its headline was verbose would lose the event itself — which is
    the one thing an audit trail must not do.
    """
    collapsed = " ".join(value.split())
    return collapsed[:MAX_TITLE_LENGTH]


def normalize_description(value: str | None) -> str | None:
    """Trim a description, treating a blank string as absent.

    Line breaks are preserved — a description may quote a multi-line change — but
    the text is truncated to the ceiling for the same reason titles are.
    """
    if value is None:
        return None

    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:MAX_DESCRIPTION_LENGTH]


def normalize_actor_name(value: str | None) -> str | None:
    """Collapse an actor's display name, treating a blank string as absent."""
    if value is None:
        return None

    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    return collapsed[:MAX_ACTOR_NAME_LENGTH]


def _json_safe(value: Any, *, depth: int) -> Any:
    """Reduce one metadata value to something the JSON column can hold.

    UUIDs, dates, and enums are the types the platform's own events carry, and all
    three are unserializable as they stand — a publisher passing ``document.id``
    should not have to remember ``str()``. Anything else unrecognised is rendered
    as text rather than dropped, so the specifics survive even when the shape does
    not.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        # Before the str/int branches would catch a StrEnum or IntEnum member and
        # store the member rather than its value.
        return _json_safe(value.value, depth=depth)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()

    if depth >= MAX_METADATA_DEPTH:
        return str(value)

    if isinstance(value, Mapping):
        return {str(key): _json_safe(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(item, depth=depth + 1) for item in value]

    return str(value)


def normalize_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Reduce an event's metadata to a storable JSON object.

    Three rules, in order:

    * ``None`` and an absent value both become ``{}``, so no reader has to handle
      a null column *and* an empty object.
    * keys whose value is ``None`` are dropped, because "the field was not
      recorded" and "the field was recorded as null" are the same statement here,
      and carrying both would make every consumer test for two things.
    * every value is coerced to something JSON can hold (see :func:`_json_safe`).

    Raises:
        InvalidTimelineMetadataError: the result exceeds
            :data:`MAX_METADATA_BYTES`. Raised rather than truncated, because
            half a JSON object is not a smaller JSON object — it is invalid.
    """
    if not value:
        return {}

    normalized = {
        str(key): _json_safe(item, depth=1) for key, item in value.items() if item is not None
    }

    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidTimelineMetadataError(
            f"Event metadata must serialize to at most {MAX_METADATA_BYTES} bytes."
        )

    return normalized
