"""User request and response schemas.

Two responsibilities, both required by the code standards ("validate every
request using Pydantic models", "return standardized API response structures"):

* **Validation** — email format, required fields, name and phone shape, and the
  role/status enums are all enforced here, so routes stay thin and every
  rejection comes back in the same envelope with a per-field message.
* **Serialization** — the password hash is not part of *any* schema below, so it
  cannot leak through a response model however an endpoint is written.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
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

from core.permissions import Permission, sort_permissions
from core.roles import permissions_for_role
from core.users import (
    MAX_NAME_LENGTH,
    MAX_PHONE_LENGTH,
    InvalidPhoneNumberError,
    normalize_email,
    normalize_name,
    normalize_phone,
)
from models.user import UserRole, UserStatus
from schemas.password import NewPassword, validate_password_bytes

#: Longest ``profile_image`` value accepted, matching the column.
MAX_PROFILE_IMAGE_LENGTH = 512

#: Default and maximum page sizes for the user list. The ceiling exists so a
#: single request cannot be used to dump the whole directory.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _validate_name(value: str) -> str:
    """Normalize a name part and reject one that is blank once trimmed."""
    normalized = normalize_name(value)
    if not normalized:
        raise ValueError("Name must not be blank.")
    return normalized


def _validate_optional_phone(value: str | None) -> str | None:
    """Validate an optional phone number, treating a blank string as absent."""
    if value is None:
        return None

    if not value.strip():
        # An empty field in a form means "no phone", not "phone of length zero".
        return None

    try:
        return normalize_phone(value)
    except InvalidPhoneNumberError as exc:
        raise ValueError(str(exc)) from exc


def _validate_optional_text(value: str | None) -> str | None:
    """Trim an optional free-text field, treating a blank string as absent."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


NamePart = Annotated[
    str,
    Field(min_length=1, max_length=MAX_NAME_LENGTH, description="Given or family name."),
]

OptionalPhone = Annotated[
    str | None,
    Field(default=None, max_length=MAX_PHONE_LENGTH, description="Contact phone number."),
]

OptionalProfileImage = Annotated[
    str | None,
    Field(
        default=None,
        max_length=MAX_PROFILE_IMAGE_LENGTH,
        description="Avatar location (object-storage key or absolute URL).",
    ),
]


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class UserRead(BaseModel):
    """A user as returned to authenticated clients.

    Used both for the caller's own profile (``GET /auth/me``) and for records in
    the administrative directory (``GET /users``), so there is one user shape on
    the wire rather than two that could drift apart.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique user identifier.")
    email: EmailStr = Field(description="Login email address.")
    first_name: str = Field(description="Given name.")
    last_name: str = Field(description="Family name.")
    full_name: str = Field(description="Display name, composed from the name parts.")
    phone: str | None = Field(default=None, description="Contact phone number, if provided.")
    profile_image: str | None = Field(default=None, description="Avatar location, if set.")
    # Added by `20-settings.md`, and added *here* rather than only on the Settings
    # response so that `GET /auth/me` — which every client already calls on load —
    # carries it. A second endpoint returning a second user shape is how two user
    # shapes start to drift, which is what this model exists to prevent.
    job_title: str | None = Field(
        default=None, description="Self-described position, e.g. 'Senior Associate'."
    )
    role: UserRole = Field(description="Platform role, which determines the permissions below.")
    status: UserStatus = Field(description="Account lifecycle state; only `active` may sign in.")
    is_active: bool = Field(description="Whether the account may authenticate (`status == active`).")
    must_change_password: bool = Field(
        default=False,
        description="Whether the user must set a new password before continuing.",
    )
    last_login_at: datetime | None = Field(
        default=None, description="Timestamp of the previous successful login."
    )
    created_at: datetime = Field(description="When the account was created.")
    updated_at: datetime = Field(description="When the account was last modified.")
    created_by: uuid.UUID | None = Field(
        default=None, description="Administrator who created the account, if recorded."
    )
    updated_by: uuid.UUID | None = Field(
        default=None, description="Administrator who last modified the account, if recorded."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Effective permissions granted by the user's role.",
    )
    @property
    def permissions(self) -> list[Permission]:
        """The user's effective permissions.

        Derived from the role rather than stored, so the payload cannot drift
        from the policy in :mod:`core.roles`. Every response carrying a user —
        login, refresh, ``/auth/me``, password change, the admin directory —
        therefore exposes the current role *and* its permissions, which is what
        lets the frontend hide inaccessible navigation without a second round
        trip.
        """
        return sort_permissions(permissions_for_role(self.role))


class UserPage(BaseModel):
    """One page of the user directory.

    Carries the totals the spec requires so a client can render pagination
    controls without a second count query or a guess at how many pages exist.
    """

    items: list[UserRead] = Field(description="Users on this page.")
    total_records: int = Field(description="Total users matching the filters, across all pages.")
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of users per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(cls, items: list[UserRead], *, total: int, page: int, page_size: int) -> UserPage:
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


class PasswordResetResponse(BaseModel):
    """Result of an administrator-initiated password reset.

    The generated password is returned **once, here, and nowhere else**: only its
    bcrypt hash is stored, and it is never logged. The administrator is expected
    to pass it to the user through a trusted channel; the account is flagged so
    the user must replace it.
    """

    user: UserRead = Field(description="The affected user.")
    temporary_password: str = Field(
        description="Generated single-use password. Shown only in this response."
    )
    must_change_password: bool = Field(
        default=True,
        description="Always true — the user must set a new password before continuing.",
    )
    message: str = Field(description="Human-readable result of the operation.")


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class UserCreate(BaseModel):
    """Body for ``POST /users`` — an administrator provisioning an account."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(description="Login email address. Must be unique.")
    first_name: NamePart
    last_name: NamePart
    # The same policy the user's own change-password endpoint enforces, so an
    # administrator cannot provision an account with a password the user would
    # not be allowed to choose.
    password: NewPassword
    phone: OptionalPhone
    profile_image: OptionalProfileImage
    role: UserRole = Field(description="Platform role to assign.")
    status: UserStatus = Field(
        default=UserStatus.ACTIVE, description="Initial account status."
    )
    must_change_password: bool = Field(
        default=False,
        description="Require the user to set a new password on their next sign-in.",
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("first_name", "last_name")
    @classmethod
    def _normalize_name_parts(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str | None) -> str | None:
        return _validate_optional_phone(value)

    @field_validator("profile_image")
    @classmethod
    def _normalize_profile_image(cls, value: str | None) -> str | None:
        return _validate_optional_text(value)

    @field_validator("password")
    @classmethod
    def _check_password_bytes(cls, value: str) -> str:
        return validate_password_bytes(value)


class UserUpdate(BaseModel):
    """Body for ``PATCH /users/{id}``.

    Every field is optional: a PATCH describes only what changes, and each field
    distinguishes "not supplied" from "explicitly cleared" (``phone: null``)
    through :meth:`provided_fields`.

    The password is deliberately absent — changing it goes through
    ``PATCH /auth/change-password`` (the user's own) or
    ``POST /users/{id}/reset-password`` (an administrator's), both of which have
    to revoke sessions and neither of which is a profile edit.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = Field(default=None, description="New login email address.")
    first_name: NamePart | None = Field(default=None, description="New given name.")
    last_name: NamePart | None = Field(default=None, description="New family name.")
    phone: OptionalPhone
    profile_image: OptionalProfileImage
    role: UserRole | None = Field(default=None, description="New platform role.")
    status: UserStatus | None = Field(default=None, description="New account status.")

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str | None) -> str | None:
        return normalize_email(value) if value is not None else None

    @field_validator("first_name", "last_name")
    @classmethod
    def _normalize_name_parts(cls, value: str | None) -> str | None:
        return _validate_name(value) if value is not None else None

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str | None) -> str | None:
        return _validate_optional_phone(value)

    @field_validator("profile_image")
    @classmethod
    def _normalize_profile_image(cls, value: str | None) -> str | None:
        return _validate_optional_text(value)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self

    def provided_fields(self) -> dict[str, object]:
        """The fields the client actually sent, validated and normalized.

        ``exclude_unset`` is what separates "leave the phone alone" from "clear
        the phone": both serialize identically otherwise, and a plain
        ``model_dump`` would silently wipe every field the client omitted.
        """
        return self.model_dump(exclude_unset=True)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class UserSortField(StrEnum):
    """Sortable columns of the user directory, per the spec."""

    NAME = "name"
    EMAIL = "email"
    CREATED_AT = "created_at"
    LAST_LOGIN = "last_login"


class SortOrder(StrEnum):
    """Sort direction."""

    ASC = "asc"
    DESC = "desc"


class UserListQuery(BaseModel):
    """Validated query parameters for ``GET /users``.

    A model rather than loose function arguments so the same rules (page bounds,
    enum membership, trimmed search term) apply wherever the listing is invoked,
    and so FastAPI documents them in one place.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Users per page (max {MAX_PAGE_SIZE}).",
    )
    search: str | None = Field(
        default=None,
        max_length=200,
        description="Case-insensitive match against first name, last name, or email.",
    )
    role: UserRole | None = Field(default=None, description="Only users holding this role.")
    status: UserStatus | None = Field(default=None, description="Only users in this status.")
    sort_by: UserSortField = Field(
        default=UserSortField.CREATED_AT, description="Column to sort by."
    )
    sort_order: SortOrder = Field(default=SortOrder.DESC, description="Sort direction.")

    @field_validator("search")
    @classmethod
    def _normalize_search(cls, value: str | None) -> str | None:
        return _validate_optional_text(value)

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size
