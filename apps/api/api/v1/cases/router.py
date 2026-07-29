"""Case management endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.case.CaseService`, and shape the HTTP response. No business
logic lives here.

Authorization is layered, and the two layers answer different questions:

* the ``cases:*`` **capability** is required by the reusable dependencies in
  :mod:`api.authorization`, declared next to the route so it appears in the
  OpenAPI schema. Because ``CurrentUser`` resolves first, an anonymous caller
  gets **401** and only an authenticated-but-unentitled one gets **403**;
* the **resource** — whether this caller is party to *this* case, and whether
  their permissions reach the fields they are writing — is checked by the
  service, which is the only layer that has the case row.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from api.authorization import require_any_permission, require_permission
from api.deps import CaseServiceDep
from core.permissions import Permission
from models.user import User
from schemas.case import (
    CaseAssignmentUpdate,
    CaseCreate,
    CaseListQuery,
    CasePage,
    CaseRead,
    CaseUpdate,
)
from schemas.errors import ErrorResponse

router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
#
# Each dependency yields the *authorized* user, so an endpoint that needs to
# record who acted does not have to depend on CurrentUser as well.
# --------------------------------------------------------------------------- #

CaseViewer = Annotated[User, Depends(require_permission(Permission.CASES_VIEW))]
CaseCreator = Annotated[User, Depends(require_permission(Permission.CASES_CREATE))]
CaseArchiver = Annotated[User, Depends(require_permission(Permission.CASES_DELETE))]
CaseAssigner = Annotated[User, Depends(require_permission(Permission.CASES_ASSIGN))]

#: Any of the three write capabilities gets a caller *into* the update endpoint;
#: which fields they may actually change is then decided per field by
#: :class:`~services.case_access.CaseAccessPolicy`. Requiring the full
#: ``cases:update`` here would lock out court representatives, whose whole role
#: is to update the hearing fields.
CaseEditor = Annotated[
    User,
    Depends(
        require_any_permission(
            Permission.CASES_UPDATE,
            Permission.CASES_UPDATE_HEARING,
            Permission.CASES_ASSIGN,
        )
    ),
]

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
            "The account is disabled, lacks the required permission, is not assigned to this "
            "case, or is writing a field its permissions do not cover."
        ),
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No case has this identifier.",
    }
}
_CONFLICT: dict[int | str, dict[str, object]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The case number is already in use.",
    }
}
_TRANSITION_CONFLICT: dict[int | str, dict[str, object]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The requested status is not a legal move from the case's current status.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "Request validation failed, or an assignee does not exist, is disabled, or holds "
            "the wrong role; `details` lists the offending fields."
        ),
    }
}


@router.get(
    "",
    response_model=CasePage,
    status_code=status.HTTP_200_OK,
    summary="List cases",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def list_cases(
    actor: CaseViewer,
    cases: CaseServiceDep,
    query: Annotated[CaseListQuery, Query()],
) -> CasePage:
    """Return a page of cases the caller is entitled to see.

    Supports **search** (case-insensitive, across case number, title,
    description, and court name), **filtering** by status, priority, assigned
    lawyer, assigned court representative, court name, filing-date range, and
    hearing-date range, **sorting** by case number, created date, updated date,
    filing date, hearing date, or priority in either direction, and
    **pagination**. Filters combine, so
    `?status=open&priority=urgent&court_name=casablanca` is a valid narrowing.

    Callers holding `cases:view-all` see every case. Everyone else sees only the
    cases they are assigned to, as lawyer or as court representative — the scope
    is applied in the query, so `total_records` counts only what the caller may
    access. Archived cases remain listed and searchable; filter on
    `status=archived` to isolate them.

    The response carries `total_records`, `page`, `page_size`, and `total_pages`,
    so pagination controls can be rendered without a second request.
    """
    result = cases.list_cases(query, actor=actor)
    return CasePage.build(
        [CaseRead.model_validate(legal_case) for legal_case in result.cases],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get(
    "/{case_id}",
    response_model=CaseRead,
    status_code=status.HTTP_200_OK,
    summary="Get a case",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_case(actor: CaseViewer, cases: CaseServiceDep, case_id: uuid.UUID) -> CaseRead:
    """Return the complete record for one case.

    Includes general information, lifecycle state and priority, court
    information, both assignments (as people, not bare identifiers), the audit
    trail, and `allowed_transitions` — the statuses this case may legally move
    to, so a client offers only valid moves.

    A caller without `cases:view-all` must be assigned to the case; otherwise the
    request is refused with **403**.
    """
    return CaseRead.model_validate(cases.get_case(case_id, actor=actor))


@router.post(
    "",
    response_model=CaseRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a case",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_CONFLICT, **_VALIDATION},
)
def create_case(actor: CaseCreator, cases: CaseServiceDep, payload: CaseCreate) -> CaseRead:
    """Open a new case.

    The case number must be unique (409 otherwise) and is **generated
    automatically** in the form `CASE-YYYY-NNNN` when the request omits it. A
    supplied number is uppercased, so the same reference cannot be filed twice in
    different casings.

    Assignments are optional and validated: an assigned lawyer must be an active
    account holding the lawyer role, and an assigned court representative an
    active account holding the court role (422 otherwise). The audit fields
    (`created_by`, `updated_by`) are populated from the authenticated caller and
    cannot be set by the request.
    """
    return CaseRead.model_validate(cases.create_case(payload, actor=actor))


@router.patch(
    "/{case_id}",
    response_model=CaseRead,
    status_code=status.HTTP_200_OK,
    summary="Update a case",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_TRANSITION_CONFLICT,
        **_VALIDATION,
    },
)
def update_case(
    actor: CaseEditor,
    cases: CaseServiceDep,
    case_id: uuid.UUID,
    payload: CaseUpdate,
) -> CaseRead:
    """Apply a partial update to a case.

    Accepts general information, status, priority, court information, hearing
    date, and assignments. Only the fields present in the body are written, so an
    omitted field is left untouched while an explicit `null` clears an optional
    one — which is how an assignment is removed.

    The immutable fields (`id`, `case_number`, `created_by`, `created_at`) are not
    part of this schema at all; sending one is a **422**.

    A status change must be a legal move from the case's current status
    (**409** otherwise); `allowed_transitions` on the case says which moves are
    available. Field-level permissions apply: `cases:update` covers the whole
    case, `cases:update-hearing` only the court-facing fields (court name, filing
    date, next hearing date, status), and `cases:assign` only the two assignment
    fields. A request touching a field the caller's permissions do not reach is
    refused **in full** with a 403 rather than partially applied.
    """
    return CaseRead.model_validate(cases.update_case(case_id, payload, actor=actor))


@router.patch(
    "/{case_id}/assignments",
    response_model=CaseRead,
    status_code=status.HTTP_200_OK,
    summary="Assign, change, or remove case assignments",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_VALIDATION},
)
def update_case_assignments(
    actor: CaseAssigner,
    cases: CaseServiceDep,
    case_id: uuid.UUID,
    payload: CaseAssignmentUpdate,
) -> CaseRead:
    """Change who works on a case.

    Covers all six operations in one place — assign, change, and remove, for both
    the lawyer and the court representative — through the same convention as
    every other PATCH: omit a field to leave that assignment alone, send an
    identifier to assign or change it, send `null` to remove it.

    Assignees are validated against their role: a lawyer position accepts only an
    active account holding the lawyer role, and a court-representative position
    only an active account holding the court role (**422** otherwise). An
    unchanged assignment is not re-validated, so a case whose lawyer was later
    deactivated stays editable.

    Equivalent to sending the same two fields to `PATCH /cases/{case_id}`; both
    run through the same service, so neither can drift from the other.
    """
    return CaseRead.model_validate(
        cases.update_case(case_id, payload.to_case_update(), actor=actor)
    )


@router.delete(
    "/{case_id}",
    response_model=CaseRead,
    status_code=status.HTTP_200_OK,
    summary="Archive a case (soft delete)",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def archive_case(actor: CaseArchiver, cases: CaseServiceDep, case_id: uuid.UUID) -> CaseRead:
    """Archive a case.

    A **soft delete**: the row is never removed, because documents, timeline
    entries, hearings, and audit records all reference it. The case is marked
    `archived`, which takes it out of the working set while leaving it listed and
    searchable, exactly as the spec requires.

    Returns the updated case, so a client can refresh its view without a second
    request. Idempotent — archiving an already-archived case succeeds. Restoring
    is an ordinary update (`PATCH` with `status: "open"`).
    """
    return CaseRead.model_validate(cases.archive_case(case_id, actor=actor))
