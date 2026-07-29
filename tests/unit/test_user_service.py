"""Unit tests for :class:`~services.user.UserService`.

Cover the business rules the service owns — email uniqueness, partial updates,
soft delete, password reset, and the self-lockout guard — against a real (SQLite
in-memory) repository, so the query layer is exercised alongside them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from core import security
from core.exceptions import DuplicateEmailError, SelfModificationError, UserNotFoundError
from models.user import User, UserRole, UserStatus
from repositories.user import UserRepository
from schemas.user import SortOrder, UserCreate, UserListQuery, UserSortField, UserUpdate
from services.user import UserService

MakeUser = Callable[..., User]

PASSWORD = "correct-horse-battery"


@pytest.fixture
def users(db_session: Session) -> UserService:
    return UserService(UserRepository(db_session))


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


def creation(**overrides: object) -> UserCreate:
    payload: dict[str, object] = {
        "email": "new.user@example.com",
        "first_name": "New",
        "last_name": "User",
        "password": PASSWORD,
        "role": "lawyer",
    }
    return UserCreate.model_validate({**payload, **overrides})


class TestCreateUser:
    def test_creates_an_account_with_a_hashed_password(
        self, users: UserService, admin: User
    ) -> None:
        created = users.create_user(creation(), actor=admin)

        assert created.email == "new.user@example.com"
        assert created.full_name == "New User"
        assert created.role is UserRole.LAWYER
        # Stored as a bcrypt hash, never as the plain password.
        assert created.hashed_password != PASSWORD
        assert created.hashed_password.startswith("$2b$")
        assert security.verify_password(PASSWORD, created.hashed_password)

    def test_populates_the_audit_fields_from_the_caller(
        self, users: UserService, admin: User
    ) -> None:
        created = users.create_user(creation(), actor=admin)

        assert created.created_by == admin.id
        assert created.updated_by == admin.id
        assert created.created_at is not None
        assert created.updated_at is not None

    def test_rejects_a_duplicate_email(self, users: UserService, admin: User) -> None:
        users.create_user(creation(), actor=admin)

        with pytest.raises(DuplicateEmailError):
            users.create_user(creation(), actor=admin)

    def test_email_uniqueness_is_case_insensitive(self, users: UserService, admin: User) -> None:
        # Emails are normalized to lowercase, so New.User@ and new.user@ are the
        # same login and must not both exist.
        users.create_user(creation(), actor=admin)

        with pytest.raises(DuplicateEmailError):
            users.create_user(creation(email="New.User@Example.com"), actor=admin)

    def test_assigns_the_requested_status(self, users: UserService, admin: User) -> None:
        created = users.create_user(creation(status="suspended"), actor=admin)

        assert created.status is UserStatus.SUSPENDED
        assert created.is_active is False


class TestGetUser:
    def test_returns_the_user(self, users: UserService, admin: User) -> None:
        assert users.get_user(admin.id).id == admin.id

    def test_raises_for_an_unknown_id(self, users: UserService) -> None:
        with pytest.raises(UserNotFoundError):
            users.get_user(uuid.uuid4())


class TestUpdateUser:
    def test_applies_only_the_fields_supplied(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com", role=UserRole.LAWYER, phone="+212 612345678")

        updated = users.update_user(
            target.id, UserUpdate.model_validate({"first_name": "Yasmine"}), actor=admin
        )

        assert updated.first_name == "Yasmine"
        # Untouched fields survive a partial update.
        assert updated.last_name == "Benali"
        assert updated.phone == "+212 612345678"

    def test_clears_an_optional_field_when_explicitly_nulled(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com", phone="+212 612345678")

        updated = users.update_user(target.id, UserUpdate.model_validate({"phone": None}), actor=admin)

        assert updated.phone is None

    def test_records_who_made_the_change(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com")

        updated = users.update_user(
            target.id, UserUpdate.model_validate({"last_name": "Nour"}), actor=admin
        )

        assert updated.updated_by == admin.id

    def test_can_change_role_and_status(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com", role=UserRole.LAWYER)

        updated = users.update_user(
            target.id,
            UserUpdate.model_validate({"role": "court", "status": "suspended"}),
            actor=admin,
        )

        assert updated.role is UserRole.COURT_REPRESENTATIVE
        assert updated.status is UserStatus.SUSPENDED

    def test_reactivates_a_deactivated_account(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com", is_active=False)

        updated = users.update_user(
            target.id, UserUpdate.model_validate({"status": "active"}), actor=admin
        )

        assert updated.is_active is True

    def test_rejects_an_email_already_used_by_another_account(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        make_user(email="taken@example.com")
        target = make_user(email="lawyer@example.com")

        with pytest.raises(DuplicateEmailError):
            users.update_user(
                target.id, UserUpdate.model_validate({"email": "taken@example.com"}), actor=admin
            )

    def test_allows_resubmitting_the_users_own_email(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        # A form that round-trips every field must not collide with the record
        # it is editing.
        target = make_user(email="lawyer@example.com")

        updated = users.update_user(
            target.id, UserUpdate.model_validate({"email": "Lawyer@Example.com"}), actor=admin
        )

        assert updated.email == "lawyer@example.com"

    def test_raises_for_an_unknown_user(self, users: UserService, admin: User) -> None:
        with pytest.raises(UserNotFoundError):
            users.update_user(
                uuid.uuid4(), UserUpdate.model_validate({"first_name": "X"}), actor=admin
            )


class TestSelfModificationGuard:
    def test_refuses_changing_your_own_role(self, users: UserService, admin: User) -> None:
        # A self-demotion cannot be undone by the person who made it.
        with pytest.raises(SelfModificationError):
            users.update_user(admin.id, UserUpdate.model_validate({"role": "lawyer"}), actor=admin)

    def test_refuses_disabling_your_own_account(self, users: UserService, admin: User) -> None:
        with pytest.raises(SelfModificationError):
            users.update_user(
                admin.id, UserUpdate.model_validate({"status": "inactive"}), actor=admin
            )

    def test_refuses_deactivating_yourself(self, users: UserService, admin: User) -> None:
        with pytest.raises(SelfModificationError):
            users.deactivate_user(admin.id, actor=admin)

    def test_allows_editing_your_own_profile(self, users: UserService, admin: User) -> None:
        # Only role and status are protected; a name or phone change is harmless.
        updated = users.update_user(
            admin.id, UserUpdate.model_validate({"first_name": "Amina", "phone": None}), actor=admin
        )

        assert updated.first_name == "Amina"

    def test_allows_resubmitting_your_own_unchanged_role(
        self, users: UserService, admin: User
    ) -> None:
        # Submitting the current value changes nothing, so it is not a lockout
        # risk — an edit form that posts every field still works.
        updated = users.update_user(
            admin.id,
            UserUpdate.model_validate({"role": "administrator", "first_name": "Amina"}),
            actor=admin,
        )

        assert updated.role is UserRole.ADMINISTRATOR


class TestDeactivateUser:
    def test_marks_the_account_inactive_without_deleting_it(
        self, users: UserService, admin: User, make_user: MakeUser, db_session: Session
    ) -> None:
        target = make_user(email="lawyer@example.com")

        deactivated = users.deactivate_user(target.id, actor=admin)

        assert deactivated.status is UserStatus.INACTIVE
        assert deactivated.is_active is False
        # Soft delete: the row survives, so audit history keeps resolving.
        assert db_session.get(User, target.id) is not None

    def test_revokes_every_session_the_user_holds(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com")
        before = target.session_generation

        deactivated = users.deactivate_user(target.id, actor=admin)

        assert deactivated.session_generation == before + 1

    def test_is_idempotent(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com")
        first = users.deactivate_user(target.id, actor=admin)
        generation = first.session_generation

        second = users.deactivate_user(target.id, actor=admin)

        assert second.status is UserStatus.INACTIVE
        # No second revocation: there was nothing left to revoke.
        assert second.session_generation == generation

    def test_records_who_deactivated_the_account(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com")

        assert users.deactivate_user(target.id, actor=admin).updated_by == admin.id

    def test_raises_for_an_unknown_user(self, users: UserService, admin: User) -> None:
        with pytest.raises(UserNotFoundError):
            users.deactivate_user(uuid.uuid4(), actor=admin)


class TestResetPassword:
    def test_issues_a_working_temporary_password(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com", password=PASSWORD)

        result = users.reset_password(target.id, actor=admin)

        assert security.verify_password(result.temporary_password, target.hashed_password)
        # The old password stops working.
        assert not security.verify_password(PASSWORD, target.hashed_password)

    def test_stores_only_the_hash(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com")

        result = users.reset_password(target.id, actor=admin)

        assert target.hashed_password != result.temporary_password
        assert target.hashed_password.startswith("$2b$")

    def test_forces_a_password_change_on_next_use(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com")

        assert users.reset_password(target.id, actor=admin).user.must_change_password is True

    def test_revokes_every_existing_session(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        # A reset is what an administrator does when an account may be
        # compromised; leaving live sessions alone would defeat the point.
        target = make_user(email="lawyer@example.com")
        before = target.session_generation

        users.reset_password(target.id, actor=admin)

        assert target.session_generation == before + 1

    def test_generates_a_different_password_each_time(
        self, users: UserService, admin: User, make_user: MakeUser
    ) -> None:
        target = make_user(email="lawyer@example.com")

        first = users.reset_password(target.id, actor=admin).temporary_password
        second = users.reset_password(target.id, actor=admin).temporary_password

        assert first != second

    def test_raises_for_an_unknown_user(self, users: UserService, admin: User) -> None:
        with pytest.raises(UserNotFoundError):
            users.reset_password(uuid.uuid4(), actor=admin)


class TestListUsers:
    @pytest.fixture
    def directory(self, make_user: MakeUser) -> list[User]:
        """A small, deterministic directory covering both roles and statuses."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            make_user(
                email="amina.benali@example.com",
                first_name="Amina",
                last_name="Benali",
                role=UserRole.ADMINISTRATOR,
                created_at=base,
                last_login_at=base + timedelta(days=3),
            ),
            make_user(
                email="karim.zahra@example.com",
                first_name="Karim",
                last_name="Zahra",
                role=UserRole.LAWYER,
                created_at=base + timedelta(days=1),
                last_login_at=base + timedelta(days=1),
            ),
            make_user(
                email="yasmine.alami@example.com",
                first_name="Yasmine",
                last_name="Alami",
                role=UserRole.LAWYER,
                status=UserStatus.SUSPENDED,
                created_at=base + timedelta(days=2),
            ),
            make_user(
                email="omar.court@example.com",
                first_name="Omar",
                last_name="Cherkaoui",
                role=UserRole.COURT_REPRESENTATIVE,
                is_active=False,
                created_at=base + timedelta(days=3),
            ),
        ]

    def emails(self, users: UserService, **query: object) -> list[str]:
        result = users.list_users(UserListQuery.model_validate(query))
        return [user.email for user in result.users]

    def test_returns_every_user_by_default(
        self, users: UserService, directory: list[User]
    ) -> None:
        result = users.list_users(UserListQuery())

        assert result.total == len(directory)
        assert len(result.users) == len(directory)

    # --- search ----------------------------------------------------------- #

    def test_searches_by_first_name(self, users: UserService, directory: list[User]) -> None:
        assert self.emails(users, search="Amina") == ["amina.benali@example.com"]

    def test_searches_by_last_name(self, users: UserService, directory: list[User]) -> None:
        assert self.emails(users, search="Zahra") == ["karim.zahra@example.com"]

    def test_searches_by_email(self, users: UserService, directory: list[User]) -> None:
        assert self.emails(users, search="omar.court") == ["omar.court@example.com"]

    def test_search_is_case_insensitive(self, users: UserService, directory: list[User]) -> None:
        assert self.emails(users, search="aMiNa") == ["amina.benali@example.com"]

    def test_search_matches_a_partial_term(
        self, users: UserService, directory: list[User]
    ) -> None:
        assert sorted(self.emails(users, search="ka")) == [
            "karim.zahra@example.com",
            "omar.court@example.com",  # matches "Cherkaoui"
        ]

    def test_search_treats_wildcards_literally(
        self, users: UserService, directory: list[User]
    ) -> None:
        # An unescaped "%" would match every user instead of none.
        assert self.emails(users, search="%") == []

    def test_search_with_no_matches_returns_an_empty_page(
        self, users: UserService, directory: list[User]
    ) -> None:
        result = users.list_users(UserListQuery(search="nobody"))

        assert (result.users, result.total) == ([], 0)

    # --- filtering -------------------------------------------------------- #

    def test_filters_by_role(self, users: UserService, directory: list[User]) -> None:
        assert sorted(self.emails(users, role="lawyer")) == [
            "karim.zahra@example.com",
            "yasmine.alami@example.com",
        ]

    def test_filters_by_status(self, users: UserService, directory: list[User]) -> None:
        assert self.emails(users, status="suspended") == ["yasmine.alami@example.com"]

    def test_filters_combine(self, users: UserService, directory: list[User]) -> None:
        assert self.emails(users, role="lawyer", status="active") == ["karim.zahra@example.com"]

    def test_a_filter_combines_with_search(
        self, users: UserService, directory: list[User]
    ) -> None:
        # "zahra" alone would also be a lawyer; the point is that the role filter
        # narrows the search rather than replacing it.
        assert self.emails(users, role="lawyer", search="zahra") == ["karim.zahra@example.com"]
        # The same term with a role that excludes the match returns nothing.
        assert self.emails(users, role="court", search="zahra") == []

    # --- sorting ---------------------------------------------------------- #

    def test_sorts_by_name_ascending(self, users: UserService, directory: list[User]) -> None:
        # "Name" orders by family name, as a directory conventionally does.
        assert self.emails(users, sort_by="name", sort_order="asc") == [
            "yasmine.alami@example.com",  # Alami
            "amina.benali@example.com",  # Benali
            "omar.court@example.com",  # Cherkaoui
            "karim.zahra@example.com",  # Zahra
        ]

    def test_sorts_by_name_descending(self, users: UserService, directory: list[User]) -> None:
        ascending = self.emails(users, sort_by="name", sort_order="asc")

        assert self.emails(users, sort_by="name", sort_order="desc") == list(reversed(ascending))

    def test_sorts_by_email(self, users: UserService, directory: list[User]) -> None:
        result = self.emails(users, sort_by="email", sort_order="asc")

        assert result == sorted(result)

    def test_sorts_by_created_date(self, users: UserService, directory: list[User]) -> None:
        assert self.emails(users, sort_by="created_at", sort_order="asc")[0] == (
            "amina.benali@example.com"
        )

    def test_sorts_by_last_login(self, users: UserService, directory: list[User]) -> None:
        newest_first = self.emails(users, sort_by="last_login", sort_order="desc")

        assert newest_first[0] == "amina.benali@example.com"

    def test_sorting_is_stable_across_ties(
        self, users: UserService, make_user: MakeUser
    ) -> None:
        # Users who have never logged in all tie on that column; without the
        # primary-key tiebreaker they could be duplicated or skipped between
        # pages.
        for index in range(5):
            make_user(email=f"tie{index}@example.com", last_name="Same")

        first = self.emails(users, sort_by="last_login", page_size=5)
        second = self.emails(users, sort_by="last_login", page_size=5)

        assert first == second

    # --- pagination -------------------------------------------------------- #

    def test_paginates(self, users: UserService, directory: list[User]) -> None:
        def page(number: int) -> list[User]:
            query = UserListQuery(
                page=number,
                page_size=2,
                sort_by=UserSortField.EMAIL,
                sort_order=SortOrder.ASC,
            )
            return users.list_users(query).users

        first, second = page(1), page(2)

        assert len(first) == 2
        assert len(second) == 2
        # No overlap between consecutive pages.
        assert {user.id for user in first}.isdisjoint({user.id for user in second})

    def test_reports_the_total_across_all_pages(
        self, users: UserService, directory: list[User]
    ) -> None:
        result = users.list_users(UserListQuery(page=1, page_size=2))

        assert result.total == len(directory)
        assert len(result.users) == 2

    def test_the_total_reflects_the_filters(
        self, users: UserService, directory: list[User]
    ) -> None:
        result = users.list_users(UserListQuery(role=UserRole.LAWYER, page_size=1))

        assert result.total == 2
        assert len(result.users) == 1

    def test_a_page_past_the_end_is_empty_not_an_error(
        self, users: UserService, directory: list[User]
    ) -> None:
        result = users.list_users(UserListQuery(page=99, page_size=20))

        assert result.users == []
        assert result.total == len(directory)
