"""Unit tests for the user management schemas.

These are the validation layer: every rule the spec lists (email format and
uniqueness shape, required fields, phone format, role and status membership) is
enforced here before a request reaches the service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from models.user import UserRole, UserStatus
from schemas.password import MIN_PASSWORD_LENGTH
from schemas.user import (
    MAX_PAGE_SIZE,
    SortOrder,
    UserCreate,
    UserListQuery,
    UserPage,
    UserRead,
    UserSortField,
    UserUpdate,
)

VALID_CREATE = {
    "email": "Amina.Benali@Example.com",
    "first_name": "  Amina ",
    "last_name": "Benali",
    "password": "correct-horse-battery",
    "role": "lawyer",
}


class TestUserCreate:
    def test_accepts_a_valid_payload_and_normalizes_it(self) -> None:
        payload = UserCreate.model_validate(VALID_CREATE)

        assert payload.email == "amina.benali@example.com"
        assert payload.first_name == "Amina"
        assert payload.role is UserRole.LAWYER
        # Status defaults to active so the common case needs no field.
        assert payload.status is UserStatus.ACTIVE
        assert payload.must_change_password is False

    @pytest.mark.parametrize("field", ["email", "first_name", "last_name", "password", "role"])
    def test_rejects_a_missing_required_field(self, field: str) -> None:
        payload = {key: value for key, value in VALID_CREATE.items() if key != field}

        with pytest.raises(ValidationError) as error:
            UserCreate.model_validate(payload)

        assert field in str(error.value)

    @pytest.mark.parametrize("email", ["not-an-email", "amina@", "@example.com", "amina benali@example.com"])
    def test_rejects_a_malformed_email(self, email: str) -> None:
        with pytest.raises(ValidationError):
            UserCreate.model_validate({**VALID_CREATE, "email": email})

    @pytest.mark.parametrize("name", ["", "   "])
    def test_rejects_a_blank_name(self, name: str) -> None:
        with pytest.raises(ValidationError):
            UserCreate.model_validate({**VALID_CREATE, "first_name": name})

    def test_rejects_a_password_below_the_policy(self) -> None:
        with pytest.raises(ValidationError):
            UserCreate.model_validate({**VALID_CREATE, "password": "a" * (MIN_PASSWORD_LENGTH - 1)})

    def test_rejects_a_password_bcrypt_would_truncate(self) -> None:
        # 73 bytes of multi-byte characters: within any character limit, past
        # bcrypt's byte limit. Truncating would make two passwords equivalent.
        with pytest.raises(ValidationError):
            UserCreate.model_validate({**VALID_CREATE, "password": "é" * 40})

    def test_accepts_and_normalizes_a_phone(self) -> None:
        payload = UserCreate.model_validate({**VALID_CREATE, "phone": " +212  612345678 "})

        assert payload.phone == "+212 612345678"

    def test_treats_a_blank_phone_as_absent(self) -> None:
        # An empty form field means "no phone", not "a phone of length zero".
        assert UserCreate.model_validate({**VALID_CREATE, "phone": "   "}).phone is None

    def test_rejects_a_malformed_phone_with_a_useful_message(self) -> None:
        with pytest.raises(ValidationError) as error:
            UserCreate.model_validate({**VALID_CREATE, "phone": "call me"})

        assert "Phone number" in str(error.value)

    def test_rejects_an_unknown_role(self) -> None:
        with pytest.raises(ValidationError):
            UserCreate.model_validate({**VALID_CREATE, "role": "judge"})

    def test_rejects_an_unknown_status(self) -> None:
        with pytest.raises(ValidationError):
            UserCreate.model_validate({**VALID_CREATE, "status": "archived"})

    def test_rejects_unknown_fields(self) -> None:
        # extra="forbid": a client cannot smuggle in `created_by` or `is_active`.
        with pytest.raises(ValidationError):
            UserCreate.model_validate({**VALID_CREATE, "created_by": str(uuid.uuid4())})


class TestUserUpdate:
    def test_distinguishes_omitted_from_explicitly_cleared(self) -> None:
        # The whole reason a PATCH body needs exclude_unset: both serialize to
        # None otherwise, and an omitted field would silently wipe the column.
        omitted = UserUpdate.model_validate({"first_name": "Amina"})
        cleared = UserUpdate.model_validate({"phone": None})

        assert "phone" not in omitted.provided_fields()
        assert cleared.provided_fields() == {"phone": None}

    def test_rejects_an_empty_body(self) -> None:
        with pytest.raises(ValidationError):
            UserUpdate.model_validate({})

    def test_has_no_password_field(self) -> None:
        # Changing a password must revoke sessions, so it never rides along on a
        # profile edit.
        assert "password" not in UserUpdate.model_fields
        with pytest.raises(ValidationError):
            UserUpdate.model_validate({"password": "correct-horse-battery"})

    def test_normalizes_the_fields_it_does_accept(self) -> None:
        payload = UserUpdate.model_validate(
            {"email": " NEW@Example.com ", "last_name": "  Ben   Salah "}
        )

        assert payload.provided_fields() == {"email": "new@example.com", "last_name": "Ben Salah"}


class TestUserListQuery:
    def test_defaults_to_the_newest_users_first(self) -> None:
        query = UserListQuery()

        assert (query.page, query.sort_by, query.sort_order) == (
            1,
            UserSortField.CREATED_AT,
            SortOrder.DESC,
        )

    def test_computes_the_offset_from_the_page(self) -> None:
        assert UserListQuery(page=3, page_size=20).offset == 40

    @pytest.mark.parametrize("page", [0, -1])
    def test_rejects_a_page_below_one(self, page: int) -> None:
        with pytest.raises(ValidationError):
            UserListQuery(page=page)

    def test_caps_the_page_size(self) -> None:
        # The ceiling is what stops one request dumping the whole directory.
        with pytest.raises(ValidationError):
            UserListQuery(page_size=MAX_PAGE_SIZE + 1)

    def test_rejects_an_unknown_sort_field(self) -> None:
        with pytest.raises(ValidationError):
            UserListQuery.model_validate({"sort_by": "password"})

    def test_treats_a_blank_search_as_absent(self) -> None:
        assert UserListQuery.model_validate({"search": "  "}).search is None


class TestUserRead:
    def _user(self, **overrides: object) -> UserRead:
        base = {
            "id": uuid.uuid4(),
            "email": "amina@example.com",
            "first_name": "Amina",
            "last_name": "Benali",
            "full_name": "Amina Benali",
            "role": UserRole.LAWYER,
            "status": UserStatus.ACTIVE,
            "is_active": True,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        return UserRead.model_validate({**base, **overrides})

    def test_never_carries_a_password_field(self) -> None:
        # The hash cannot leak through a response however an endpoint is written,
        # because no field exists to hold it.
        assert not {"password", "hashed_password"} & UserRead.model_fields.keys()

    def test_exposes_the_permissions_of_the_role(self) -> None:
        assert "cases:view" in self._user().model_dump()["permissions"]
        assert "users:create" not in self._user().model_dump()["permissions"]


class TestUserPage:
    def test_derives_the_page_count(self) -> None:
        page = UserPage.build([], total=41, page=1, page_size=20)

        assert page.total_pages == 3

    def test_an_empty_directory_still_reports_one_page(self) -> None:
        # A client must never render "page 1 of 0".
        assert UserPage.build([], total=0, page=1, page_size=20).total_pages == 1
