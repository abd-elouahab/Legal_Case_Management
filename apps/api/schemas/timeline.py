"""Timeline request and response schemas.

Two responsibilities, the same two the case and document schemas carry:

* **Validation** — the list query's page bounds, filters, search term, and date
  coherence are enforced here, so routes stay thin and every rejection comes back
  in the standard envelope with a per-field message.
* **Serialization** — an event is returned with its category computed and its
  actor already denormalised onto the row, so a client renders an entry without a
  second lookup.

There is deliberately **no request schema for creating an event**. ``08-timeline.md``
specifies two read endpoints and nothing else: events are published by the
business services that cause them (see :class:`~services.timeline.TimelineService`),
never by a client. An audit trail a client can write directly is not an audit
trail.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from core.timeline import category_for, known_event_types
from models.timeline import TimelineEventCategory
from schemas.case import SortOrder

#: Default and maximum page sizes for the timeline. The ceiling exists so a
#: single request cannot be used to dump a case's entire history.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: Matches ``timeline_events.event_type``.
MAX_EVENT_TYPE_LENGTH = 60

#: Re-exported so the timeline API's sort direction is literally the same type as
#: the case and document APIs', rather than a third enum that happens to agree.
__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_EVENT_TYPE_LENGTH",
    "MAX_PAGE_SIZE",
    "SortOrder",
    "TimelineEventPage",
    "TimelineEventRead",
    "TimelineListQuery",
    "TimelineSortField",
]


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class TimelineEventRead(BaseModel):
    """One timeline event as returned to authorized clients.

    Carries exactly the fields ``08-timeline.md`` lists, plus a **computed**
    ``category`` so the client can pick an icon without a second copy of the
    event-type → category mapping.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID = Field(description="Unique event identifier.")
    case_id: uuid.UUID = Field(description="Case this event belongs to.")

    event_type: str = Field(
        description=(
            "What happened, as a registry identifier. The platform currently records: "
            f"{', '.join(f'`{value}`' for value in known_event_types())}. Treat this as an "
            "open set — a later module may publish a type not listed here."
        )
    )
    title: str = Field(description="Short headline, e.g. `Document Uploaded`.")
    description: str | None = Field(
        default=None, description="Human sentence describing what happened."
    )

    actor_id: uuid.UUID | None = Field(
        default=None,
        description="Identifier of the user who acted, if the account still exists.",
    )
    actor_name: str | None = Field(
        default=None,
        description=(
            "The actor's display name **at the time of the event**. A snapshot, not a lookup: "
            "renaming a user must not rewrite history."
        ),
    )
    actor_role: str | None = Field(
        default=None, description="The actor's platform role at the time of the event."
    )

    metadata: dict[str, Any] = Field(
        default_factory=dict,
        # The ORM attribute is `event_metadata`, because `metadata` is
        # SQLAlchemy's own attribute on the declarative base. The wire field is
        # `metadata`, as the spec specifies, and both names validate.
        validation_alias=AliasChoices("event_metadata", "metadata"),
        description=(
            "Structured specifics of this event, e.g. `{\"from\": \"open\", \"to\": "
            "\"in_progress\"}`. Shape depends on the event type; always an object, never null."
        ),
    )

    created_at: datetime = Field(description="When the event happened.")

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Family the event belongs to — `case`, `status`, `priority`, `assignment`, or "
            "`document`. Derived from the event type rather than stored, so it cannot drift; "
            "an unrecognised type is presented as `case`."
        ),
    )
    @property
    def category(self) -> TimelineEventCategory:
        """Computed, so the client's icon choice cannot disagree with the server."""
        return category_for(self.event_type)


class TimelineEventPage(BaseModel):
    """One page of a timeline.

    Carries the totals the spec requires so a client can render pagination
    controls without a second count query.
    """

    items: list[TimelineEventRead] = Field(description="Events on this page.")
    total_records: int = Field(description="Total events matching the filters, across all pages.")
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of events per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(
        cls, items: list[TimelineEventRead], *, total: int, page: int, page_size: int
    ) -> TimelineEventPage:
        """Assemble a page, deriving ``total_pages`` from the total and size.

        An empty result still reports one page, so a client never renders
        "page 1 of 0".
        """
        return cls(
            items=items,
            total_records=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)) if page_size else 1,
        )


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class TimelineSortField(StrEnum):
    """Sortable columns of the timeline.

    One member, because the spec names one: *Event Date*. Declared as an enum
    rather than hard-coded so the parameter is self-documenting in OpenAPI and so
    a second sort key is an addition rather than a signature change.
    """

    #: "Event Date" — when the event happened.
    CREATED_AT = "created_at"


class TimelineListQuery(BaseModel):
    """Validated query parameters for the timeline endpoints.

    Every filter is optional and they combine with AND, which is what the spec's
    "filters should be combinable" requires.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Events per page (max {MAX_PAGE_SIZE}).",
    )
    search: str | None = Field(
        default=None,
        max_length=200,
        description="Case-insensitive match against the event title or description.",
    )
    event_type: str | None = Field(
        default=None,
        max_length=MAX_EVENT_TYPE_LENGTH,
        description=(
            "Only events of this type. A free identifier rather than a closed enum, so a type "
            "published by a later module stays filterable."
        ),
    )
    actor_id: uuid.UUID | None = Field(
        default=None, description="Only events recorded for this user."
    )
    date_from: date | None = Field(
        default=None, description="Only events that happened on or after this date."
    )
    date_to: date | None = Field(
        default=None, description="Only events that happened on or before this date."
    )
    sort_by: TimelineSortField = Field(
        default=TimelineSortField.CREATED_AT, description="Column to sort by."
    )
    sort_order: SortOrder = Field(
        default=SortOrder.DESC,
        description=(
            "Sort direction. Defaults to descending, so a timeline reads newest first — which "
            "is the reverse-chronological order the spec's display requires."
        ),
    )

    @field_validator("search")
    @classmethod
    def _normalize_search(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("event_type")
    @classmethod
    def _normalize_event_type(cls, value: str | None) -> str | None:
        # Identifiers are stored lowercase, so a filter typed in any case matches
        # rather than silently returning nothing.
        if value is None:
            return None
        return value.strip().lower() or None

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        # An inverted range matches nothing, which reads as "the filter is
        # broken" rather than as the input error it is.
        if self.date_from is not None and self.date_to is not None and self.date_to < self.date_from:
            raise ValueError("`date_to` cannot be before `date_from`.")
        return self

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size
