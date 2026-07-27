"""Tests for :class:`~services.authorization.AuthorizationService`.

Covers the four supported checks (role, permission, any, all) in both their
boolean and raising forms, for all three roles, plus the failure modes that must
not be silently tolerated.
"""

from __future__ import annotations

import uuid

import pytest

from core.exceptions import AuthorizationConfigurationError, AuthorizationError
from core.permissions import ALL_PERMISSIONS, Permission
from core.roles import UserRole, permissions_for_role
from models.user import User
from services.authorization import AuthorizationService


def make_user(role: UserRole) -> User:
    """A minimal in-memory user; authorization never touches the database."""
    return User(
        id=uuid.uuid4(),
        email=f"{role.value}@example.com",
        full_name="Test User",
        role=role,
        is_active=True,
        hashed_password="not-used",
    )


@pytest.fixture
def authorization() -> AuthorizationService:
    return AuthorizationService()


@pytest.fixture
def administrator() -> User:
    return make_user(UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer() -> User:
    return make_user(UserRole.LAWYER)


@pytest.fixture
def court() -> User:
    return make_user(UserRole.COURT_REPRESENTATIVE)


class TestEffectivePermissions:
    def test_administrator_holds_every_permission(
        self, authorization: AuthorizationService, administrator: User
    ) -> None:
        assert authorization.permissions_for(administrator) == ALL_PERMISSIONS

    @pytest.mark.parametrize("role", list(UserRole))
    def test_permissions_come_from_the_central_policy(
        self, authorization: AuthorizationService, role: UserRole
    ) -> None:
        # The service must not carry a second copy of the policy.
        assert authorization.permissions_for(make_user(role)) == permissions_for_role(role)


class TestRequireRole:
    def test_allows_a_matching_role(self, authorization: AuthorizationService, administrator: User) -> None:
        authorization.require_role(administrator, [UserRole.ADMINISTRATOR])

    def test_allows_any_listed_role(self, authorization: AuthorizationService, court: User) -> None:
        authorization.require_role(court, [UserRole.ADMINISTRATOR, UserRole.COURT_REPRESENTATIVE])

    def test_denies_an_unlisted_role(self, authorization: AuthorizationService, lawyer: User) -> None:
        with pytest.raises(AuthorizationError) as excinfo:
            authorization.require_role(lawyer, [UserRole.ADMINISTRATOR])

        assert excinfo.value.status_code == 403

    def test_has_role_reports_without_raising(
        self, authorization: AuthorizationService, lawyer: User
    ) -> None:
        assert authorization.has_role(lawyer, [UserRole.LAWYER]) is True
        assert authorization.has_role(lawyer, [UserRole.ADMINISTRATOR]) is False

    def test_an_empty_role_list_is_a_configuration_error(
        self, authorization: AuthorizationService, administrator: User
    ) -> None:
        # Denying everyone (including administrators) is never the intent.
        with pytest.raises(AuthorizationConfigurationError):
            authorization.require_role(administrator, [])


class TestRequirePermission:
    def test_allows_a_granted_permission(
        self, authorization: AuthorizationService, lawyer: User
    ) -> None:
        authorization.require_permission(lawyer, Permission.CASES_VIEW)

    def test_denies_a_missing_permission(
        self, authorization: AuthorizationService, lawyer: User
    ) -> None:
        with pytest.raises(AuthorizationError) as excinfo:
            authorization.require_permission(lawyer, Permission.USERS_VIEW)

        assert excinfo.value.status_code == 403

    def test_administrators_pass_every_permission_check(
        self, authorization: AuthorizationService, administrator: User
    ) -> None:
        for permission in Permission:
            authorization.require_permission(administrator, permission)

    def test_has_permission_reports_without_raising(
        self, authorization: AuthorizationService, court: User
    ) -> None:
        assert authorization.has_permission(court, Permission.CASES_UPDATE) is True
        assert authorization.has_permission(court, Permission.AI_CHAT) is False


class TestRequireAnyPermission:
    def test_allows_when_one_is_held(self, authorization: AuthorizationService, court: User) -> None:
        authorization.require_any_permission(court, [Permission.USERS_VIEW, Permission.CASES_VIEW])

    def test_denies_when_none_are_held(self, authorization: AuthorizationService, court: User) -> None:
        with pytest.raises(AuthorizationError):
            authorization.require_any_permission(court, [Permission.AI_CHAT, Permission.REPORTS_VIEW])

    def test_has_any_permission_reports_without_raising(
        self, authorization: AuthorizationService, lawyer: User
    ) -> None:
        assert authorization.has_any_permission(lawyer, [Permission.USERS_VIEW, Permission.AI_CHAT]) is True
        assert authorization.has_any_permission(lawyer, [Permission.USERS_VIEW]) is False

    def test_an_empty_permission_list_is_a_configuration_error(
        self, authorization: AuthorizationService, administrator: User
    ) -> None:
        with pytest.raises(AuthorizationConfigurationError):
            authorization.require_any_permission(administrator, [])


class TestRequireAllPermissions:
    def test_allows_when_all_are_held(self, authorization: AuthorizationService, lawyer: User) -> None:
        authorization.require_all_permissions(
            lawyer, [Permission.REPORTS_GENERATE, Permission.DOCUMENTS_VIEW]
        )

    def test_denies_when_one_is_missing(self, authorization: AuthorizationService, lawyer: User) -> None:
        with pytest.raises(AuthorizationError):
            authorization.require_all_permissions(
                lawyer, [Permission.DOCUMENTS_VIEW, Permission.DOCUMENTS_DELETE]
            )

    def test_has_all_permissions_reports_without_raising(
        self, authorization: AuthorizationService, court: User
    ) -> None:
        assert (
            authorization.has_all_permissions(court, [Permission.CASES_VIEW, Permission.CASES_UPDATE])
            is True
        )
        assert (
            authorization.has_all_permissions(court, [Permission.CASES_VIEW, Permission.CASES_DELETE])
            is False
        )

    def test_an_empty_permission_list_is_a_configuration_error(
        self, authorization: AuthorizationService, lawyer: User
    ) -> None:
        # Requiring nothing would wave everyone through — worse than failing loudly.
        with pytest.raises(AuthorizationConfigurationError):
            authorization.require_all_permissions(lawyer, [])


class TestDenialDoesNotLeakPolicy:
    def test_the_message_never_names_the_required_permission(
        self, authorization: AuthorizationService, lawyer: User
    ) -> None:
        with pytest.raises(AuthorizationError) as excinfo:
            authorization.require_permission(lawyer, Permission.USERS_DELETE)

        assert Permission.USERS_DELETE.value not in excinfo.value.message
        assert excinfo.value.error_code == "forbidden"

    def test_every_denial_looks_identical(
        self, authorization: AuthorizationService, lawyer: User, court: User
    ) -> None:
        # A caller must not be able to tell *which* rule refused them.
        denials = []
        for user, check in (
            (lawyer, lambda u: authorization.require_permission(u, Permission.USERS_VIEW)),
            (court, lambda u: authorization.require_role(u, [UserRole.ADMINISTRATOR])),
            (court, lambda u: authorization.require_any_permission(u, [Permission.AI_CHAT])),
            (lawyer, lambda u: authorization.require_all_permissions(u, [Permission.USERS_VIEW])),
        ):
            with pytest.raises(AuthorizationError) as excinfo:
                check(user)
            denials.append((excinfo.value.status_code, excinfo.value.error_code, excinfo.value.message))

        assert len(set(denials)) == 1
