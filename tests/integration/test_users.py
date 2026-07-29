"""Integration tests for the User Management API.

Exercise the endpoints over real HTTP: the CRUD contract, validation responses,
authorization (401 vs 403 for every route and every role), password reset,
search, filtering, sorting, and pagination.

The service-level rules are unit-tested in ``tests/unit/test_user_service.py``;
what these add is the wire contract — status codes, response shapes, error
envelopes, and the guarantee that a password hash never appears in a response.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from models.user import User, UserRole, UserStatus

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
ME_URL = f"{settings.API_V1_PREFIX}/auth/me"
USERS_URL = f"{settings.API_V1_PREFIX}/users"

MakeUser = Callable[..., User]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == status.HTTP_200_OK, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def creation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "email": "new.user@example.com",
        "first_name": "New",
        "last_name": "User",
        "password": PASSWORD,
        "role": "lawyer",
    }
    return {**payload, **overrides}


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def admin_headers(api_client: TestClient, admin: User) -> dict[str, str]:
    return bearer(token_for(api_client, admin.email))


@pytest.fixture
def target(make_user: MakeUser) -> User:
    """An ordinary account for the administrator to act on."""
    return make_user(
        email="karim.zahra@example.com",
        password=PASSWORD,
        first_name="Karim",
        last_name="Zahra",
        role=UserRole.LAWYER,
    )


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuthentication:
    """No credentials must be 401, never 403 — the two are not interchangeable."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", ""),
            ("POST", ""),
            ("GET", "/{id}"),
            ("PATCH", "/{id}"),
            ("DELETE", "/{id}"),
            ("POST", "/{id}/reset-password"),
        ],
    )
    def test_every_route_requires_authentication(
        self, api_client: TestClient, method: str, path: str
    ) -> None:
        url = USERS_URL + path.format(id=uuid.uuid4())

        response = api_client.request(method, url, json={})

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        # RFC 6750: tell the client that Bearer credentials are expected.
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_an_expired_or_malformed_token_is_also_401(self, api_client: TestClient) -> None:
        response = api_client.get(USERS_URL, headers=bearer("not-a-real-token"))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestAuthorization:
    """Only holders of the matching ``users:*`` permission may act."""

    @pytest.fixture(params=[UserRole.LAWYER, UserRole.COURT_REPRESENTATIVE])
    def restricted_headers(
        self, request: pytest.FixtureRequest, api_client: TestClient, make_user: MakeUser
    ) -> dict[str, str]:
        role: UserRole = request.param
        email = f"{role.value}@example.com"
        make_user(email=email, password=PASSWORD, role=role)
        return bearer(token_for(api_client, email))

    def test_a_restricted_role_cannot_list_users(
        self, api_client: TestClient, restricted_headers: dict[str, str]
    ) -> None:
        response = api_client.get(USERS_URL, headers=restricted_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error"] == "forbidden"

    def test_a_restricted_role_cannot_read_a_user(
        self, api_client: TestClient, restricted_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.get(f"{USERS_URL}/{target.id}", headers=restricted_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_restricted_role_cannot_create_a_user(
        self, api_client: TestClient, restricted_headers: dict[str, str]
    ) -> None:
        response = api_client.post(USERS_URL, json=creation_payload(), headers=restricted_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_restricted_role_cannot_update_a_user(
        self, api_client: TestClient, restricted_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.patch(
            f"{USERS_URL}/{target.id}", json={"first_name": "Hacked"}, headers=restricted_headers
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_restricted_role_cannot_deactivate_a_user(
        self, api_client: TestClient, restricted_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.delete(f"{USERS_URL}/{target.id}", headers=restricted_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_restricted_role_cannot_reset_a_password(
        self, api_client: TestClient, restricted_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.post(
            f"{USERS_URL}/{target.id}/reset-password", headers=restricted_headers
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_denial_never_names_the_required_permission(
        self, api_client: TestClient, restricted_headers: dict[str, str]
    ) -> None:
        # Naming it would let a caller map the platform's capability model.
        response = api_client.get(USERS_URL, headers=restricted_headers)

        assert "users:" not in response.text
        assert "administrator" not in response.text

    def test_a_disabled_administrator_is_refused(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str]
    ) -> None:
        admin.status = UserStatus.INACTIVE

        response = api_client.get(USERS_URL, headers=admin_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error"] == "account_disabled"


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


class TestCreateUser:
    def test_creates_a_user(self, api_client: TestClient, admin_headers: dict[str, str]) -> None:
        response = api_client.post(USERS_URL, json=creation_payload(), headers=admin_headers)

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert body["email"] == "new.user@example.com"
        assert body["full_name"] == "New User"
        assert body["role"] == "lawyer"
        assert body["status"] == "active"
        assert body["is_active"] is True

    def test_the_new_user_can_sign_in(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # The whole point of the endpoint: it replaces the provisioning script.
        api_client.post(USERS_URL, json=creation_payload(), headers=admin_headers)

        assert token_for(api_client, "new.user@example.com")

    def test_populates_the_audit_fields(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.post(USERS_URL, json=creation_payload(), headers=admin_headers).json()

        assert body["created_by"] == str(admin.id)
        assert body["updated_by"] == str(admin.id)
        assert body["created_at"] and body["updated_at"]

    def test_audit_fields_cannot_be_supplied_by_the_client(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            USERS_URL,
            json=creation_payload(created_by=str(uuid.uuid4())),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_rejects_a_duplicate_email_with_409(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.post(
            USERS_URL, json=creation_payload(email=target.email), headers=admin_headers
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error"] == "email_already_exists"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("email", "not-an-email"),
            ("first_name", "   "),
            ("password", "short"),
            ("role", "judge"),
            ("status", "archived"),
            ("phone", "call me"),
        ],
    )
    def test_returns_a_field_level_validation_error(
        self, api_client: TestClient, admin_headers: dict[str, str], field: str, value: str
    ) -> None:
        response = api_client.post(
            USERS_URL, json=creation_payload(**{field: value}), headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        body = response.json()
        assert body["error"] == "validation_error"
        assert any(detail["field"] == field for detail in body["details"]), body["details"]

    def test_never_returns_the_password(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(USERS_URL, json=creation_payload(), headers=admin_headers)

        assert PASSWORD not in response.text
        assert "password" not in response.json()
        assert "hashed_password" not in response.json()


class TestReadUser:
    def test_returns_the_complete_record(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.get(f"{USERS_URL}/{target.id}", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["id"] == str(target.id)
        assert body["first_name"] == "Karim"
        assert body["last_name"] == "Zahra"
        assert body["full_name"] == "Karim Zahra"
        assert body["role"] == "lawyer"
        assert set(body) >= {
            "email",
            "phone",
            "profile_image",
            "status",
            "must_change_password",
            "last_login_at",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "permissions",
        }

    def test_exposes_the_permissions_of_the_users_role(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        body = api_client.get(f"{USERS_URL}/{target.id}", headers=admin_headers).json()

        assert "cases:view" in body["permissions"]
        assert "users:create" not in body["permissions"]

    def test_never_exposes_the_password_hash(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.get(f"{USERS_URL}/{target.id}", headers=admin_headers)

        assert target.hashed_password not in response.text

    def test_returns_404_for_an_unknown_id(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{USERS_URL}/{uuid.uuid4()}", headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "user_not_found"

    def test_returns_422_for_a_malformed_id(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{USERS_URL}/not-a-uuid", headers=admin_headers)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestUpdateUser:
    def test_updates_personal_information(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.patch(
            f"{USERS_URL}/{target.id}",
            json={"first_name": "Yasmine", "phone": "+212 612345678"},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["first_name"] == "Yasmine"
        assert body["phone"] == "+212 612345678"
        # An omitted field is untouched.
        assert body["last_name"] == "Zahra"

    def test_updates_role_and_status(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.patch(
            f"{USERS_URL}/{target.id}",
            json={"role": "court", "status": "suspended"},
            headers=admin_headers,
        )

        body = response.json()
        assert body["role"] == "court"
        assert body["status"] == "suspended"
        assert body["is_active"] is False
        # Permissions follow the new role immediately — they are computed.
        assert "reports:view" not in body["permissions"]

    def test_records_who_made_the_change(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str], target: User
    ) -> None:
        body = api_client.patch(
            f"{USERS_URL}/{target.id}", json={"last_name": "Nour"}, headers=admin_headers
        ).json()

        assert body["updated_by"] == str(admin.id)

    def test_rejects_a_password_change_through_this_endpoint(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.patch(
            f"{USERS_URL}/{target.id}", json={"password": "brand-new-password"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_rejects_an_email_belonging_to_another_account(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.patch(
            f"{USERS_URL}/{target.id}", json={"email": admin.email}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_rejects_an_empty_body(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.patch(f"{USERS_URL}/{target.id}", json={}, headers=admin_headers)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_returns_404_for_an_unknown_id(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.patch(
            f"{USERS_URL}/{uuid.uuid4()}", json={"first_name": "X"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_refuses_a_self_applied_role_or_status_change(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.patch(
            f"{USERS_URL}/{admin.id}", json={"role": "lawyer"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["error"] == "cannot_modify_own_account"

    def test_allows_editing_your_own_profile(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.patch(
            f"{USERS_URL}/{admin.id}", json={"phone": "+212 600000000"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_200_OK


class TestDeactivateUser:
    def test_soft_deletes_the_account(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.delete(f"{USERS_URL}/{target.id}", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "inactive"

        # The record is still there — this is a soft delete, not a removal.
        lookup = api_client.get(f"{USERS_URL}/{target.id}", headers=admin_headers)
        assert lookup.status_code == status.HTTP_200_OK

    def test_a_deactivated_user_cannot_sign_in(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        api_client.delete(f"{USERS_URL}/{target.id}", headers=admin_headers)

        response = api_client.post(
            LOGIN_URL, json={"email": target.email, "password": PASSWORD}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error"] == "account_disabled"

    def test_deactivation_revokes_an_existing_session(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        # A signed-in user must lose access immediately, not when their access
        # token happens to expire.
        victim_headers = bearer(token_for(api_client, target.email))
        assert api_client.get(ME_URL, headers=victim_headers).status_code == status.HTTP_200_OK

        api_client.delete(f"{USERS_URL}/{target.id}", headers=admin_headers)

        assert api_client.get(ME_URL, headers=victim_headers).status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    def test_reactivation_is_a_normal_update(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        api_client.delete(f"{USERS_URL}/{target.id}", headers=admin_headers)

        response = api_client.patch(
            f"{USERS_URL}/{target.id}", json={"status": "active"}, headers=admin_headers
        )

        assert response.json()["is_active"] is True
        assert token_for(api_client, target.email)

    def test_refuses_self_deactivation(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.delete(f"{USERS_URL}/{admin.id}", headers=admin_headers)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["error"] == "cannot_modify_own_account"

    def test_returns_404_for_an_unknown_id(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.delete(f"{USERS_URL}/{uuid.uuid4()}", headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestResetPassword:
    def test_issues_a_usable_temporary_password(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        response = api_client.post(
            f"{USERS_URL}/{target.id}/reset-password", headers=admin_headers
        )

        assert response.status_code == status.HTTP_200_OK
        temporary = response.json()["temporary_password"]
        assert temporary

        # The generated password works and the old one does not.
        assert token_for(api_client, target.email, temporary)
        assert (
            api_client.post(LOGIN_URL, json={"email": target.email, "password": PASSWORD}).status_code
            == status.HTTP_401_UNAUTHORIZED
        )

    def test_flags_the_account_for_a_forced_change(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        body = api_client.post(
            f"{USERS_URL}/{target.id}/reset-password", headers=admin_headers
        ).json()

        assert body["must_change_password"] is True
        assert body["user"]["must_change_password"] is True

    def test_the_forced_change_flag_reaches_the_user_at_sign_in(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        temporary = api_client.post(
            f"{USERS_URL}/{target.id}/reset-password", headers=admin_headers
        ).json()["temporary_password"]

        login = api_client.post(LOGIN_URL, json={"email": target.email, "password": temporary})

        assert login.json()["user"]["must_change_password"] is True

    def test_changing_the_password_clears_the_flag(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        temporary = api_client.post(
            f"{USERS_URL}/{target.id}/reset-password", headers=admin_headers
        ).json()["temporary_password"]
        victim = bearer(token_for(api_client, target.email, temporary))

        response = api_client.patch(
            f"{settings.API_V1_PREFIX}/auth/change-password",
            json={"current_password": temporary, "new_password": "a-brand-new-password"},
            headers=victim,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["user"]["must_change_password"] is False

    def test_revokes_existing_sessions(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        victim = bearer(token_for(api_client, target.email))

        api_client.post(f"{USERS_URL}/{target.id}/reset-password", headers=admin_headers)

        assert api_client.get(ME_URL, headers=victim).status_code == status.HTTP_401_UNAUTHORIZED

    def test_stores_only_a_hash(
        self, api_client: TestClient, admin_headers: dict[str, str], target: User
    ) -> None:
        temporary = api_client.post(
            f"{USERS_URL}/{target.id}/reset-password", headers=admin_headers
        ).json()["temporary_password"]

        assert target.hashed_password != temporary
        assert target.hashed_password.startswith("$2b$")

    def test_returns_404_for_an_unknown_id(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            f"{USERS_URL}/{uuid.uuid4()}/reset-password", headers=admin_headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class TestListUsers:
    @pytest.fixture
    def directory(self, admin: User, make_user: MakeUser) -> list[User]:
        return [
            admin,
            make_user(
                email="karim.zahra@example.com",
                first_name="Karim",
                last_name="Zahra",
                role=UserRole.LAWYER,
            ),
            make_user(
                email="yasmine.alami@example.com",
                first_name="Yasmine",
                last_name="Alami",
                role=UserRole.LAWYER,
                status=UserStatus.SUSPENDED,
            ),
            make_user(
                email="omar.cherkaoui@example.com",
                first_name="Omar",
                last_name="Cherkaoui",
                role=UserRole.COURT_REPRESENTATIVE,
                is_active=False,
            ),
        ]

    def get(
        self, api_client: TestClient, headers: dict[str, str], **params: object
    ) -> dict[str, object]:
        response = api_client.get(USERS_URL, params=params, headers=headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        body: dict[str, object] = response.json()
        return body

    def emails(self, body: dict[str, object]) -> list[str]:
        items: list[dict[str, str]] = body["items"]  # type: ignore[assignment]
        return [item["email"] for item in items]

    def test_returns_a_page_with_the_documented_envelope(
        self, api_client: TestClient, admin_headers: dict[str, str], directory: list[User]
    ) -> None:
        body = self.get(api_client, admin_headers)

        assert set(body) == {"items", "total_records", "page", "page_size", "total_pages"}
        assert body["total_records"] == len(directory)
        assert body["page"] == 1
        assert body["total_pages"] == 1

    def test_never_includes_a_password_hash(
        self, api_client: TestClient, admin_headers: dict[str, str], directory: list[User]
    ) -> None:
        response = api_client.get(USERS_URL, headers=admin_headers)

        for user in directory:
            assert user.hashed_password not in response.text

    def test_searches_case_insensitively_across_name_and_email(
        self, api_client: TestClient, admin_headers: dict[str, str], directory: list[User]
    ) -> None:
        assert self.emails(self.get(api_client, admin_headers, search="ZAHRA")) == [
            "karim.zahra@example.com"
        ]
        assert self.emails(self.get(api_client, admin_headers, search="yasmine")) == [
            "yasmine.alami@example.com"
        ]
        assert self.emails(self.get(api_client, admin_headers, search="cherkaoui@")) == [
            "omar.cherkaoui@example.com"
        ]

    def test_a_search_with_no_results_is_an_empty_page_not_an_error(
        self, api_client: TestClient, admin_headers: dict[str, str], directory: list[User]
    ) -> None:
        body = self.get(api_client, admin_headers, search="nobody-here")

        assert body["items"] == []
        assert body["total_records"] == 0
        assert body["total_pages"] == 1

    def test_filters_by_role_and_status_together(
        self, api_client: TestClient, admin_headers: dict[str, str], directory: list[User]
    ) -> None:
        body = self.get(api_client, admin_headers, role="lawyer", status="active")

        assert self.emails(body) == ["karim.zahra@example.com"]

    def test_sorts_by_name_in_both_directions(
        self, api_client: TestClient, admin_headers: dict[str, str], directory: list[User]
    ) -> None:
        ascending = self.emails(self.get(api_client, admin_headers, sort_by="name", sort_order="asc"))
        descending = self.emails(
            self.get(api_client, admin_headers, sort_by="name", sort_order="desc")
        )

        # Ordered by family name: Alami, Benali, Cherkaoui, Zahra.
        assert ascending == [
            "yasmine.alami@example.com",
            "admin@example.com",
            "omar.cherkaoui@example.com",
            "karim.zahra@example.com",
        ]
        assert descending == list(reversed(ascending))

    def test_paginates_without_overlap(
        self, api_client: TestClient, admin_headers: dict[str, str], directory: list[User]
    ) -> None:
        first = self.get(api_client, admin_headers, page=1, page_size=2, sort_by="email")
        second = self.get(api_client, admin_headers, page=2, page_size=2, sort_by="email")

        assert first["total_records"] == len(directory)
        assert first["total_pages"] == 2
        assert len(first["items"]) == 2  # type: ignore[arg-type]
        assert set(self.emails(first)).isdisjoint(self.emails(second))

    @pytest.mark.parametrize(
        "params",
        [
            {"page": 0},
            {"page_size": 0},
            {"page_size": 1000},
            {"role": "judge"},
            {"status": "archived"},
            {"sort_by": "password"},
            {"sort_order": "sideways"},
        ],
    )
    def test_rejects_invalid_query_parameters(
        self, api_client: TestClient, admin_headers: dict[str, str], params: dict[str, object]
    ) -> None:
        response = api_client.get(USERS_URL, params=params, headers=admin_headers)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
