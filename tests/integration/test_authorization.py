"""Integration tests for RBAC over HTTP.

Verifies the contract every future endpoint inherits from the reusable
authorization dependencies: **401** without a valid token, **403** with a valid
token that lacks the permission, **200** otherwise — and that a 403 body never
reveals what would have been required.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Annotated

import pytest
from fastapi import Depends, FastAPI, status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # Importing conftest at runtime would create *second* class
    # objects (pytest loads it as top-level `conftest`), breaking isinstance.
    from tests.conftest import InMemoryLoginThrottle, InMemoryRevocationStore

from api.authorization import (
    require_all_permissions,
    require_any_permission,
    require_permission,
    require_role,
)
from core.config import settings
from core.permissions import ALL_PERMISSIONS, Permission, sort_permissions
from core.roles import ROLE_PERMISSIONS, permissions_for_role
from models.user import User, UserRole, UserStatus
from tests.helpers import expired_access_token

PASSWORD = "correct-horse-battery"

AUTH_PREFIX = f"{settings.API_V1_PREFIX}/auth"
LOGIN_URL = f"{AUTH_PREFIX}/login"
ME_URL = f"{AUTH_PREFIX}/me"

AUTHZ_PREFIX = f"{settings.API_V1_PREFIX}/authorization"
AUTHZ_ME_URL = f"{AUTHZ_PREFIX}/me"
ROLES_URL = f"{AUTHZ_PREFIX}/roles"

MakeUser = Callable[..., User]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


@pytest.fixture
def sign_in(api_client: TestClient, make_user: MakeUser) -> Callable[[UserRole], dict[str, str]]:
    """Create a user with the given role and return its Authorization header."""

    def _sign_in(role: UserRole) -> dict[str, str]:
        email = f"{role.value}@example.com"
        make_user(email=email, password=PASSWORD, role=role)
        return bearer(token_for(api_client, email))

    return _sign_in


SignIn = Callable[[UserRole], dict[str, str]]


class TestUnauthenticatedAccess:
    """No credentials must be a 401, never a 403 — the two are not interchangeable."""

    @pytest.mark.parametrize("url", [AUTHZ_ME_URL, ROLES_URL])
    def test_missing_token_returns_401(self, api_client: TestClient, url: str) -> None:
        response = api_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["error"] == "missing_token"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize("url", [AUTHZ_ME_URL, ROLES_URL])
    def test_malformed_token_returns_401(self, api_client: TestClient, url: str) -> None:
        response = api_client.get(url, headers=bearer("not-a-jwt"))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["error"] == "invalid_token"

    def test_expired_token_returns_401_not_403(
        self, api_client: TestClient, make_user: MakeUser
    ) -> None:
        # Authentication is evaluated before authorization, so an expired token
        # must never be reported as a permission problem.
        user = make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)

        response = api_client.get(ROLES_URL, headers=bearer(expired_access_token(str(user.id))))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["error"] == "token_expired"


class TestCurrentAuthorization:
    @pytest.mark.parametrize("role", list(UserRole))
    def test_reports_the_callers_role_and_permissions(
        self, api_client: TestClient, sign_in: SignIn, role: UserRole
    ) -> None:
        response = api_client.get(AUTHZ_ME_URL, headers=sign_in(role))

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["role"] == role.value
        assert body["permissions"] == [p.value for p in sort_permissions(permissions_for_role(role))]

    def test_is_available_to_every_authenticated_role(self, api_client: TestClient, sign_in: SignIn) -> None:
        # It only ever describes the caller's own grants, so no role is excluded.
        for role in UserRole:
            assert api_client.get(AUTHZ_ME_URL, headers=sign_in(role)).status_code == 200


class TestRoleCatalogRequiresPermission:
    def test_administrator_can_read_the_catalog(self, api_client: TestClient, sign_in: SignIn) -> None:
        response = api_client.get(ROLES_URL, headers=sign_in(UserRole.ADMINISTRATOR))

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert {entry["role"] for entry in body["roles"]} == {role.value for role in UserRole}
        assert {entry["id"] for entry in body["permissions"]} == {p.value for p in ALL_PERMISSIONS}

    @pytest.mark.parametrize("role", [UserRole.LAWYER, UserRole.COURT_REPRESENTATIVE])
    def test_restricted_roles_receive_403(
        self, api_client: TestClient, sign_in: SignIn, role: UserRole
    ) -> None:
        response = api_client.get(ROLES_URL, headers=sign_in(role))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error"] == "forbidden"

    def test_a_denial_never_names_the_required_permission(
        self, api_client: TestClient, sign_in: SignIn
    ) -> None:
        response = api_client.get(ROLES_URL, headers=sign_in(UserRole.LAWYER))

        # Naming the missing permission would let a caller map the platform's
        # capability model by probing endpoints.
        assert Permission.USERS_VIEW.value not in response.text
        assert UserRole.ADMINISTRATOR.value not in response.text

    def test_the_denial_still_carries_a_request_id(
        self, api_client: TestClient, sign_in: SignIn
    ) -> None:
        # Operators need to correlate a 403 with the logged reason, since the
        # response body deliberately omits it.
        response = api_client.get(ROLES_URL, headers=sign_in(UserRole.LAWYER))

        assert response.json()["request_id"]


class TestPermissionsInTheAuthenticationContext:
    @pytest.mark.parametrize("role", list(UserRole))
    def test_me_exposes_role_and_permissions(
        self, api_client: TestClient, sign_in: SignIn, role: UserRole
    ) -> None:
        response = api_client.get(ME_URL, headers=sign_in(role))

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["role"] == role.value
        assert body["permissions"] == [p.value for p in sort_permissions(permissions_for_role(role))]

    def test_login_response_carries_permissions(
        self, api_client: TestClient, make_user: MakeUser
    ) -> None:
        make_user(email="lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)

        response = api_client.post(
            LOGIN_URL, json={"email": "lawyer@example.com", "password": PASSWORD}
        )

        assert response.status_code == 200
        permissions = response.json()["user"]["permissions"]
        assert Permission.CASES_VIEW.value in permissions
        assert Permission.USERS_VIEW.value not in permissions

    def test_permissions_are_never_stored_on_the_user_record(
        self, db_session: Session, make_user: MakeUser
    ) -> None:
        # Derived from the role on every read, so a policy change takes effect
        # immediately and no row can hold a stale grant.
        user = make_user(email="court@example.com", password=PASSWORD, role=UserRole.COURT_REPRESENTATIVE)

        assert not hasattr(user, "permissions")


class TestReusableDependencies:
    """The dependency factories are the contract future features build on.

    They are exercised against throwaway routes rather than real endpoints,
    because no business endpoints exist yet — and because a guard should be
    testable independently of whatever it happens to guard.
    """

    @pytest.fixture
    def guarded_client(
        self,
        db_session: Session,
        revocations: InMemoryRevocationStore,
        throttle: InMemoryLoginThrottle,
    ) -> Iterator[TestClient]:
        from api.deps import get_login_throttle, get_token_revocation_store
        from core.exceptions import register_exception_handlers
        from db.session import get_db

        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/role-guarded", dependencies=[Depends(require_role(UserRole.ADMINISTRATOR))])
        def role_guarded() -> dict[str, bool]:
            return {"ok": True}

        @app.get("/permission-guarded", dependencies=[Depends(require_permission(Permission.CASES_VIEW))])
        def permission_guarded() -> dict[str, bool]:
            return {"ok": True}

        @app.get(
            "/any-guarded",
            dependencies=[Depends(require_any_permission(Permission.AI_CHAT, Permission.CASES_UPDATE))],
        )
        def any_guarded() -> dict[str, bool]:
            return {"ok": True}

        @app.get(
            "/all-guarded",
            dependencies=[
                Depends(require_all_permissions(Permission.REPORTS_GENERATE, Permission.DOCUMENTS_VIEW))
            ],
        )
        def all_guarded() -> dict[str, bool]:
            return {"ok": True}

        @app.get("/identity")
        def identity(
            user: Annotated[User, Depends(require_permission(Permission.CASES_VIEW))],
        ) -> dict[str, str]:
            # The dependency also *yields* the authorized user, so an endpoint
            # need not depend on CurrentUser a second time.
            return {"role": user.role.value}

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_token_revocation_store] = lambda: revocations
        app.dependency_overrides[get_login_throttle] = lambda: throttle
        with TestClient(app) as client:
            yield client

    @pytest.fixture
    def headers_for(
        self, api_client: TestClient, make_user: MakeUser
    ) -> Callable[[UserRole], dict[str, str]]:
        def _headers(role: UserRole) -> dict[str, str]:
            email = f"{role.value}@example.com"
            make_user(email=email, password=PASSWORD, role=role)
            # Tokens are signed, not session-bound, so one issued through the main
            # app authenticates against the throwaway app just the same.
            return bearer(token_for(api_client, email))

        return _headers

    @pytest.mark.parametrize(
        ("path", "role", "expected"),
        [
            ("/role-guarded", UserRole.ADMINISTRATOR, 200),
            ("/role-guarded", UserRole.LAWYER, 403),
            ("/role-guarded", UserRole.COURT_REPRESENTATIVE, 403),
            ("/permission-guarded", UserRole.ADMINISTRATOR, 200),
            ("/permission-guarded", UserRole.LAWYER, 200),
            ("/permission-guarded", UserRole.COURT_REPRESENTATIVE, 200),
            # any: lawyer has ai:chat, court has cases:update, both pass.
            ("/any-guarded", UserRole.LAWYER, 200),
            ("/any-guarded", UserRole.COURT_REPRESENTATIVE, 200),
            # all: only roles holding *both* reporting and document access pass.
            ("/all-guarded", UserRole.LAWYER, 200),
            ("/all-guarded", UserRole.COURT_REPRESENTATIVE, 403),
            ("/all-guarded", UserRole.ADMINISTRATOR, 200),
        ],
    )
    def test_guards_enforce_the_policy(
        self,
        guarded_client: TestClient,
        headers_for: Callable[[UserRole], dict[str, str]],
        path: str,
        role: UserRole,
        expected: int,
    ) -> None:
        response = guarded_client.get(path, headers=headers_for(role))

        assert response.status_code == expected, response.text

    @pytest.mark.parametrize(
        "path", ["/role-guarded", "/permission-guarded", "/any-guarded", "/all-guarded"]
    )
    def test_every_guard_returns_401_without_a_token(
        self, guarded_client: TestClient, path: str
    ) -> None:
        assert guarded_client.get(path).status_code == status.HTTP_401_UNAUTHORIZED

    def test_a_guard_yields_the_authorized_user(
        self, guarded_client: TestClient, headers_for: Callable[[UserRole], dict[str, str]]
    ) -> None:
        response = guarded_client.get("/identity", headers=headers_for(UserRole.LAWYER))

        assert response.status_code == 200
        assert response.json() == {"role": UserRole.LAWYER.value}

    def test_a_disabled_account_is_rejected_before_authorization(
        self,
        guarded_client: TestClient,
        api_client: TestClient,
        db_session: Session,
        make_user: MakeUser,
    ) -> None:
        user = make_user(email="disabled@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)
        headers = bearer(token_for(api_client, "disabled@example.com"))

        user.status = UserStatus.INACTIVE
        db_session.commit()

        response = guarded_client.get("/role-guarded", headers=headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        # Distinct from a permission denial: the account itself is disabled.
        assert response.json()["error"] == "account_disabled"


class TestPolicyIsSingleSourced:
    def test_the_catalog_matches_the_policy_module(
        self, api_client: TestClient, sign_in: SignIn
    ) -> None:
        # If the endpoint ever grew its own copy of the mapping, this fails.
        body = api_client.get(ROLES_URL, headers=sign_in(UserRole.ADMINISTRATOR)).json()

        served = {entry["role"]: entry["permissions"] for entry in body["roles"]}
        expected = {
            role.value: [p.value for p in sort_permissions(permissions)]
            for role, permissions in ROLE_PERMISSIONS.items()
        }
        assert served == expected
