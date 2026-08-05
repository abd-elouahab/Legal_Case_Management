"""Timeline and audit-trail endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.timeline.TimelineService`, and shape the HTTP response. No
business logic lives here — and none lives in the service either, which is the
point of the module.

**Read-only.** ``08-timeline.md`` specifies exactly two endpoints, both ``GET``.
Events are published by the services that cause them; there is no route through
which a client can write, amend, or remove one, because an audit trail a client
can edit is not an audit trail.

Two routers rather than one, because the spec's two paths live under different
prefixes: :data:`case_timeline_router` is mounted under ``/cases`` to serve
``GET /cases/{case_id}/timeline``, and :data:`router` under ``/timeline`` to serve
``GET /timeline/{event_id}``. Keeping both here rather than adding the first to
the case router keeps every timeline endpoint in the timeline module.

Authorization is layered, and the two layers answer different questions:

* the ``timeline:view`` **capability** is required by the reusable dependencies in
  :mod:`api.authorization`, declared next to the route so it appears in the
  OpenAPI schema. Because ``CurrentUser`` resolves first, an anonymous caller gets
  **401** and only an authenticated-but-unentitled one gets **403**;
* the **resource** — whether this caller is party to the event's *case* — is
  checked by the service, which is the only layer that has the case row.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from api.authorization import require_permission
from api.deps import TimelineServiceDep
from core.permissions import Permission
from models.user import User
from schemas.errors import ErrorResponse
from schemas.timeline import TimelineEventPage, TimelineEventRead, TimelineListQuery

#: ``GET /timeline/{event_id}``.
router = APIRouter()

#: ``GET /cases/{case_id}/timeline``.
case_timeline_router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

TimelineViewer = Annotated[User, Depends(require_permission(Permission.TIMELINE_VIEW))]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": (
            "The account is disabled, lacks the required permission, or is not assigned to the "
            "case this timeline belongs to."
        ),
    }
}
_CASE_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No case has this identifier.",
    }
}
_EVENT_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No timeline event has this identifier.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "Request validation failed; `details` names the offending field.",
    }
}


@case_timeline_router.get(
    "/{case_id}/timeline",
    response_model=TimelineEventPage,
    status_code=status.HTTP_200_OK,
    summary="Get a case's timeline",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_CASE_NOT_FOUND, **_VALIDATION},
)
def get_case_timeline(
    actor: TimelineViewer,
    timeline: TimelineServiceDep,
    case_id: uuid.UUID,
    query: Annotated[TimelineListQuery, Query()],
) -> TimelineEventPage:
    """Return a page of the chronological history recorded against one case.

    Events are produced automatically by the modules that change the case — a
    case being created or archived, a status or priority change, a lawyer or
    court representative being assigned or removed, and every document uploaded,
    updated, replaced, deleted, or downloaded.

    Supports **search** (case-insensitive, across the event title and
    description), **filtering** by event type, actor, and date range, **sorting**
    by event date in either direction, and **pagination**. Filters combine, so
    `?event_type=status_changed&actor_id=…&date_from=2026-07-01` is a valid
    narrowing.

    Defaults to **newest first**, which is the reverse-chronological order the
    timeline is read in.

    A caller without `cases:view-all` must be assigned to the case; otherwise the
    request is refused with **403** rather than answered with an empty page, so an
    empty timeline and an inaccessible one are never confused.

    The response carries `total_records`, `page`, `page_size`, and `total_pages`,
    so pagination controls can be rendered without a second request.
    """
    result = timeline.list_case_timeline(case_id, query, actor=actor)
    return TimelineEventPage.build(
        [TimelineEventRead.model_validate(event) for event in result.events],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get(
    "/{event_id}",
    response_model=TimelineEventRead,
    status_code=status.HTTP_200_OK,
    summary="Get a timeline event",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_EVENT_NOT_FOUND},
)
def get_timeline_event(
    actor: TimelineViewer, timeline: TimelineServiceDep, event_id: uuid.UUID
) -> TimelineEventRead:
    """Return one timeline event.

    Carries what happened (`event_type`, `title`, `description`), who did it
    (`actor_id`, plus the `actor_name` and `actor_role` **as they were at the
    time**), when (`created_at`), and the event's structured `metadata` — whose
    shape depends on the event type.

    `category` is computed from the event type, so a client picks an icon without
    holding a second copy of the mapping.

    A caller without `cases:view-all` must be assigned to the event's case;
    otherwise the request is refused with **403**.
    """
    return TimelineEventRead.model_validate(timeline.get_event(event_id, actor=actor))
