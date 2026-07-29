"""Case request and response schemas.

Two responsibilities, both required by the code standards ("validate every
request using Pydantic models", "return standardized API response structures"):

* **Validation** — required fields, the case-number format, date coherence, the
  status and priority enums, and the length limits are all enforced here, so
  routes stay thin and every rejection comes back in the same envelope with a
  per-field message.
* **Serialization** — the assignment and audit columns are exposed as *people*
  (:class:`CaseUserSummary`) rather than as bare identifiers, because a
  identifier alone is useless in a table and would otherwise force the client to
  resolve every one of them itself.

Business rules that need the database — case-number uniqueness, whether an
assignee actually holds the right role, whether a status change is a legal move —
live in :mod:`services.case` and cannot be expressed here.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from core.cases import (
    MAX_CASE_NUMBER_LENGTH,
    MAX_CATEGORY_LENGTH,
    MAX_COURT_NAME_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_TITLE_LENGTH,
    InvalidCaseNumberError,
    allowed_transitions,
    normalize_case_number,
    normalize_description,
    normalize_text,
)
from models.case import CasePriority, CaseStatus
from models.user import UserRole

#: Default and maximum page sizes for the case list. The ceiling exists so a
#: single request cannot be used to dump the whole caseload.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _validate_title(value: str) -> str:
    """Normalize a title and reject one that is blank once trimmed."""
    normalized = normalize_text(value)
    if not normalized:
        raise ValueError("Title must not be blank.")
    if len(normalized) > MAX_TITLE_LENGTH:
        raise ValueError(f"Title must be at most {MAX_TITLE_LENGTH} characters.")
    return normalized


def _validate_optional_line(value: str | None, *, label: str, limit: int) -> str | None:
    """Normalize an optional single-line field, treating a blank string as absent."""
    if value is None:
        return None

    normalized = normalize_text(value)
    if not normalized:
        # An empty field in a form means "not recorded", not "a value of length
        # zero" — the same rule the user schemas apply to an optional phone.
        return None
    if len(normalized) > limit:
        raise ValueError(f"{label} must be at most {limit} characters.")
    return normalized


def _validate_optional_description(value: str | None) -> str | None:
    """Trim a description without flattening its paragraphs."""
    if value is None:
        return None

    normalized = normalize_description(value)
    if not normalized:
        return None
    if len(normalized) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Description must be at most {MAX_DESCRIPTION_LENGTH} characters.")
    return normalized


def _validate_optional_case_number(value: str | None) -> str | None:
    """Validate an optional client-supplied case number.

    Blank means "generate one", which is what the spec's "generate a unique case
    number if not provided" asks for — and is how an empty form field arrives.
    """
    if value is None or not value.strip():
        return None

    try:
        return normalize_case_number(value)
    except InvalidCaseNumberError as exc:
        raise ValueError(str(exc)) from exc


def _check_hearing_after_filing(filing: date | None, hearing: date | None) -> None:
    """Reject a hearing scheduled before the case was filed.

    The one date rule that is knowable without the database: a hearing cannot
    precede the filing that produced it. Everything else — a filing date in the
    future, a hearing long past — is legitimate (a case can be pre-registered, and
    a past hearing is simply history), so it is not rejected.
    """
    if filing is not None and hearing is not None and hearing < filing:
        raise ValueError("The next hearing date cannot be before the filing date.")


CaseTitle = Annotated[
    str,
    Field(min_length=1, max_length=MAX_TITLE_LENGTH, description="Short name identifying the case."),
]

OptionalDescription = Annotated[
    str | None,
    Field(
        default=None,
        max_length=MAX_DESCRIPTION_LENGTH,
        description="Free-text summary of the matter.",
    ),
]

OptionalCategory = Annotated[
    str | None,
    Field(
        default=None,
        max_length=MAX_CATEGORY_LENGTH,
        description="Classification of the matter (free text; the platform defines no fixed list).",
    ),
]

OptionalCourtName = Annotated[
    str | None,
    Field(
        default=None,
        max_length=MAX_COURT_NAME_LENGTH,
        description="Court hearing the case.",
    ),
]


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class CaseUserSummary(BaseModel):
    """A person referenced by a case: an assignee or an auditor.

    Deliberately *not* :class:`~schemas.user.UserRead`. A case is readable by
    lawyers and court representatives, who hold no ``users:view`` permission —
    embedding the full directory record would hand them account status, audit
    trail, and the assignee's effective permissions through a side door. This
    carries only what a case screen displays.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique user identifier.")
    full_name: str = Field(description="Display name.")
    email: EmailStr = Field(description="Contact email address.")
    role: UserRole = Field(description="Platform role.")


class CaseRead(BaseModel):
    """A case as returned to authorized clients."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique case identifier.")
    case_number: str = Field(description="Unique, human-facing case reference.")
    title: str = Field(description="Short name identifying the case.")
    description: str | None = Field(default=None, description="Free-text summary of the matter.")
    category: str | None = Field(default=None, description="Classification of the matter.")
    status: CaseStatus = Field(description="Current lifecycle state.")
    priority: CasePriority = Field(description="How urgently the case needs attention.")
    court_name: str | None = Field(default=None, description="Court hearing the case.")
    filing_date: date | None = Field(default=None, description="Date the case was filed.")
    next_hearing_date: date | None = Field(default=None, description="Date of the next hearing.")

    assigned_lawyer_id: uuid.UUID | None = Field(
        default=None, description="Identifier of the assigned lawyer, if any."
    )
    assigned_court_representative_id: uuid.UUID | None = Field(
        default=None, description="Identifier of the assigned court representative, if any."
    )
    assigned_lawyer: CaseUserSummary | None = Field(
        default=None, description="The assigned lawyer, if any."
    )
    assigned_court_representative: CaseUserSummary | None = Field(
        default=None, description="The assigned court representative, if any."
    )

    created_by: uuid.UUID | None = Field(
        default=None, description="Identifier of the user who created the case, if recorded."
    )
    updated_by: uuid.UUID | None = Field(
        default=None, description="Identifier of the user who last modified the case, if recorded."
    )
    creator: CaseUserSummary | None = Field(
        default=None, description="The user who created the case, if recorded."
    )
    updater: CaseUserSummary | None = Field(
        default=None, description="The user who last modified the case, if recorded."
    )

    created_at: datetime = Field(description="When the case was created.")
    updated_at: datetime = Field(description="When the case was last modified.")

    is_archived: bool = Field(description="Whether the case has been archived (`status == archived`).")

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "Statuses this case may move to from its current one, in lifecycle order. "
            "Served so a client offers only legal moves instead of re-implementing the "
            "transition rules — and so a policy change reaches the UI without a release."
        ),
    )
    @property
    def allowed_transitions(self) -> list[CaseStatus]:
        """The legal next statuses for this case.

        Derived from the status rather than stored, so it cannot drift from
        :data:`~core.cases.STATUS_TRANSITIONS` — exactly the reasoning behind
        ``UserRead.permissions``. Ordered by the lifecycle rather than by the
        set's arbitrary iteration order, so the client's menu is stable.
        """
        allowed = allowed_transitions(self.status)
        return [status for status in CaseStatus if status in allowed]


class CasePage(BaseModel):
    """One page of the case list.

    Carries the totals the spec requires so a client can render pagination
    controls without a second count query or a guess at how many pages exist.
    """

    items: list[CaseRead] = Field(description="Cases on this page.")
    total_records: int = Field(description="Total cases matching the filters, across all pages.")
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of cases per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(cls, items: list[CaseRead], *, total: int, page: int, page_size: int) -> CasePage:
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
# Requests
# --------------------------------------------------------------------------- #


class CaseCreate(BaseModel):
    """Body for ``POST /cases``."""

    model_config = ConfigDict(extra="forbid")

    case_number: str | None = Field(
        default=None,
        max_length=MAX_CASE_NUMBER_LENGTH,
        description="Case reference. Generated automatically when omitted.",
    )
    title: CaseTitle
    description: OptionalDescription
    category: OptionalCategory
    status: CaseStatus = Field(
        default=CaseStatus.DRAFT,
        description="Initial lifecycle state. A new case starts as a draft unless stated otherwise.",
    )
    priority: CasePriority = Field(
        default=CasePriority.MEDIUM, description="Initial priority."
    )
    court_name: OptionalCourtName
    filing_date: date | None = Field(default=None, description="Date the case was filed.")
    next_hearing_date: date | None = Field(default=None, description="Date of the next hearing.")
    assigned_lawyer_id: uuid.UUID | None = Field(
        default=None, description="Lawyer to assign. Must be an active user holding the lawyer role."
    )
    assigned_court_representative_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Court representative to assign. Must be an active user holding the court role."
        ),
    )

    @field_validator("case_number")
    @classmethod
    def _normalize_case_number(cls, value: str | None) -> str | None:
        return _validate_optional_case_number(value)

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str) -> str:
        return _validate_title(value)

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, value: str | None) -> str | None:
        return _validate_optional_description(value)

    @field_validator("category")
    @classmethod
    def _normalize_category(cls, value: str | None) -> str | None:
        return _validate_optional_line(value, label="Category", limit=MAX_CATEGORY_LENGTH)

    @field_validator("court_name")
    @classmethod
    def _normalize_court_name(cls, value: str | None) -> str | None:
        return _validate_optional_line(value, label="Court name", limit=MAX_COURT_NAME_LENGTH)

    @model_validator(mode="after")
    def _check_dates(self) -> Self:
        _check_hearing_after_filing(self.filing_date, self.next_hearing_date)
        return self


class CaseUpdate(BaseModel):
    """Body for ``PATCH /cases/{id}``.

    Every field is optional: a PATCH describes only what changes, and each field
    distinguishes "not supplied" from "explicitly cleared" (``court_name: null``,
    ``assigned_lawyer_id: null`` — which is how an assignment is *removed*)
    through :meth:`provided_fields`.

    The immutable fields are absent entirely rather than validated and rejected:
    ``id``, ``case_number``, ``created_by``, and ``created_at`` identify the case
    and record who filed it, so there is no field here to forget to guard. With
    ``extra="forbid"``, sending one is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    title: CaseTitle | None = Field(default=None, description="New title.")
    description: OptionalDescription
    category: OptionalCategory
    status: CaseStatus | None = Field(default=None, description="New lifecycle state.")
    priority: CasePriority | None = Field(default=None, description="New priority.")
    court_name: OptionalCourtName
    filing_date: date | None = Field(default=None, description="New filing date.")
    next_hearing_date: date | None = Field(default=None, description="New next hearing date.")
    assigned_lawyer_id: uuid.UUID | None = Field(
        default=None, description="New assigned lawyer; `null` removes the assignment."
    )
    assigned_court_representative_id: uuid.UUID | None = Field(
        default=None, description="New assigned court representative; `null` removes the assignment."
    )

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str | None) -> str | None:
        return _validate_title(value) if value is not None else None

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, value: str | None) -> str | None:
        return _validate_optional_description(value)

    @field_validator("category")
    @classmethod
    def _normalize_category(cls, value: str | None) -> str | None:
        return _validate_optional_line(value, label="Category", limit=MAX_CATEGORY_LENGTH)

    @field_validator("court_name")
    @classmethod
    def _normalize_court_name(cls, value: str | None) -> str | None:
        return _validate_optional_line(value, label="Court name", limit=MAX_COURT_NAME_LENGTH)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self

    @model_validator(mode="after")
    def _check_dates(self) -> Self:
        # Only checkable here when the request carries *both* dates. A PATCH that
        # moves one of them is validated against the stored case in the service,
        # which is the only place the other value is known.
        if {"filing_date", "next_hearing_date"} <= self.model_fields_set:
            _check_hearing_after_filing(self.filing_date, self.next_hearing_date)
        return self

    def provided_fields(self) -> dict[str, object]:
        """The fields the client actually sent, validated and normalized.

        ``exclude_unset`` is what separates "leave the court alone" from "clear
        the court": both serialize identically otherwise, and a plain
        ``model_dump`` would silently wipe every field the client omitted — and,
        worse here, unassign everyone.
        """
        return self.model_dump(exclude_unset=True)


class CaseAssignmentUpdate(BaseModel):
    """Body for ``PATCH /cases/{id}/assignments``.

    Covers all six operations the spec lists — assign, change, and remove, for
    each of the two roles — through the same omitted-versus-null distinction the
    rest of the API uses: omit a field to leave that assignment alone, send an
    identifier to assign or change it, send ``null`` to remove it.
    """

    model_config = ConfigDict(extra="forbid")

    assigned_lawyer_id: uuid.UUID | None = Field(
        default=None, description="Lawyer to assign; `null` removes the current assignment."
    )
    assigned_court_representative_id: uuid.UUID | None = Field(
        default=None,
        description="Court representative to assign; `null` removes the current assignment.",
    )

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one assignment to change.")
        return self

    def to_case_update(self) -> CaseUpdate:
        """Express this as a partial case update.

        The assignment endpoint is a convenience over the same operation, so it
        delegates to the one update path rather than duplicating the validation,
        audit, and logging that path already performs.
        """
        return CaseUpdate(**self.model_dump(exclude_unset=True))


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class CaseSortField(StrEnum):
    """Sortable columns of the case list, per the spec."""

    CASE_NUMBER = "case_number"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    FILING_DATE = "filing_date"
    NEXT_HEARING_DATE = "next_hearing_date"
    PRIORITY = "priority"


class SortOrder(StrEnum):
    """Sort direction."""

    ASC = "asc"
    DESC = "desc"


class CaseListQuery(BaseModel):
    """Validated query parameters for ``GET /cases``.

    A model rather than loose function arguments so the same rules (page bounds,
    enum membership, trimmed search term, date coherence) apply wherever the
    listing is invoked, and so FastAPI documents them in one place.

    Every filter is optional and they combine with AND, which is what the spec's
    "filters should be combinable" requires.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Cases per page (max {MAX_PAGE_SIZE}).",
    )
    search: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Case-insensitive match against case number, title, description, or court name."
        ),
    )
    status: CaseStatus | None = Field(default=None, description="Only cases in this status.")
    priority: CasePriority | None = Field(default=None, description="Only cases at this priority.")
    assigned_lawyer_id: uuid.UUID | None = Field(
        default=None, description="Only cases assigned to this lawyer."
    )
    assigned_court_representative_id: uuid.UUID | None = Field(
        default=None, description="Only cases assigned to this court representative."
    )
    court_name: str | None = Field(
        default=None,
        max_length=MAX_COURT_NAME_LENGTH,
        description="Only cases whose court name contains this text (case-insensitive).",
    )
    filing_date_from: date | None = Field(
        default=None, description="Only cases filed on or after this date."
    )
    filing_date_to: date | None = Field(
        default=None, description="Only cases filed on or before this date."
    )
    hearing_date_from: date | None = Field(
        default=None, description="Only cases with a next hearing on or after this date."
    )
    hearing_date_to: date | None = Field(
        default=None, description="Only cases with a next hearing on or before this date."
    )
    sort_by: CaseSortField = Field(
        default=CaseSortField.CREATED_AT, description="Column to sort by."
    )
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="Sort direction.")

    @field_validator("search", "court_name")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_text(value)
        return normalized or None

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        # An inverted range matches nothing, which reads as "the filter is
        # broken" rather than as the input error it is.
        if (
            self.filing_date_from is not None
            and self.filing_date_to is not None
            and self.filing_date_to < self.filing_date_from
        ):
            raise ValueError("`filing_date_to` cannot be before `filing_date_from`.")

        if (
            self.hearing_date_from is not None
            and self.hearing_date_to is not None
            and self.hearing_date_to < self.hearing_date_from
        ):
            raise ValueError("`hearing_date_to` cannot be before `hearing_date_from`.")

        return self

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size
