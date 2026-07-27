"""Integration tests for the authentication endpoints.

Exercises the full HTTP surface — validation, status codes, error envelopes,
cookies, and the login → refresh → logout lifecycle — through FastAPI's
TestClient against an in-memory database and token denylist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.config import settings
from models.user import User, UserRole
from tests.helpers import expired_access_token, expired_refresh_token, forge_token

PASSWORD = "correct-horse-battery"
EMAIL = "amina.benali@example.com"

AUTH_PREFIX = f"{settings.API_V1_PREFIX}/auth"
LOGIN_URL = f"{AUTH_PREFIX}/login"
LOGOUT_URL = f"{AUTH_PREFIX}/logout"
REFRESH_URL = f"{AUTH_PREFIX}/refresh"
ME_URL = f"{AUTH_PREFIX}/me"
CHANGE_PASSWORD_URL = f"{AUTH_PREFIX}/change-password"

MakeUser = Callable[..., User]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> dict[str, Any]:
    """Sign in and return the token payload (raising if login failed)."""
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


@pytest.fixture
def user(make_user: MakeUser) -> User:
    return make_user(email=EMAIL, password=PASSWORD)


class TestLogin:
    def test_returns_a_token_pair_and_the_user(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})

        assert response.status_code == 200
        body = response.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        assert body["user"]["email"] == EMAIL
        assert body["user"]["id"] == str(user.id)

    def test_never_returns_the_password_hash(self, api_client: TestClient, user: User) -> None:
        body = login(api_client)

        assert "hashed_password" not in body["user"]
        assert "password" not in body["user"]
        assert PASSWORD not in response_text(body)

    def test_sets_an_httponly_refresh_cookie(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})

        set_cookie = response.headers["set-cookie"]
        assert settings.REFRESH_COOKIE_NAME in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie.replace("samesite", "SameSite")
        assert "Path=/" in set_cookie
        assert api_client.cookies.get(settings.REFRESH_COOKIE_NAME)

    def test_the_access_token_is_not_placed_in_a_cookie(self, api_client: TestClient, user: User) -> None:
        body = login(api_client)

        cookie_values = list(api_client.cookies.values())
        assert body["access_token"] not in cookie_values

    def test_email_matching_is_case_insensitive(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(LOGIN_URL, json={"email": EMAIL.upper(), "password": PASSWORD})

        assert response.status_code == 200

    def test_records_the_last_login_timestamp(
        self, api_client: TestClient, user: User, db_session: Session
    ) -> None:
        assert user.last_login_at is None

        login(api_client)
        db_session.refresh(user)

        assert user.last_login_at is not None

    def test_rejects_a_wrong_password(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": "wrong-password"})

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_credentials"

    def test_rejects_an_unknown_email(self, api_client: TestClient) -> None:
        response = api_client.post(LOGIN_URL, json={"email": "nobody@example.com", "password": PASSWORD})

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_credentials"

    def test_wrong_password_and_unknown_email_look_identical(self, api_client: TestClient, user: User) -> None:
        unknown = api_client.post(LOGIN_URL, json={"email": "nobody@example.com", "password": PASSWORD})
        wrong = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": "wrong-password"})

        assert unknown.status_code == wrong.status_code
        assert unknown.json()["error"] == wrong.json()["error"]
        assert unknown.json()["message"] == wrong.json()["message"]

    def test_a_failed_login_sets_no_cookie(self, api_client: TestClient, user: User) -> None:
        api_client.post(LOGIN_URL, json={"email": EMAIL, "password": "wrong-password"})

        assert api_client.cookies.get(settings.REFRESH_COOKIE_NAME) is None

    def test_rejects_a_disabled_account(self, api_client: TestClient, make_user: MakeUser) -> None:
        make_user(email="disabled@example.com", password=PASSWORD, is_active=False)

        response = api_client.post(LOGIN_URL, json={"email": "disabled@example.com", "password": PASSWORD})

        assert response.status_code == 403
        assert response.json()["error"] == "account_disabled"

    def test_challenges_with_the_bearer_scheme_on_401(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": "wrong-password"})

        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize(
        "body",
        [
            {"email": "not-an-email", "password": PASSWORD},
            {"email": "", "password": PASSWORD},
            {"email": EMAIL},
            {"password": PASSWORD},
            {},
            {"email": EMAIL, "password": ""},
        ],
    )
    def test_rejects_invalid_request_bodies(self, api_client: TestClient, body: dict[str, str]) -> None:
        response = api_client.post(LOGIN_URL, json=body)

        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"

    def test_validation_errors_name_the_offending_field(self, api_client: TestClient) -> None:
        response = api_client.post(LOGIN_URL, json={"email": "not-an-email", "password": PASSWORD})

        details = response.json()["details"]
        assert any(detail["field"] == "email" for detail in details)

    def test_errors_carry_a_correlation_id(self, api_client: TestClient) -> None:
        response = api_client.post(LOGIN_URL, json={"email": "not-an-email", "password": PASSWORD})

        assert response.json()["request_id"]
        assert response.headers["x-request-id"]


class TestCurrentUser:
    def test_returns_the_authenticated_user(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        response = api_client.get(ME_URL, headers=bearer(tokens["access_token"]))

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(user.id)
        assert body["email"] == EMAIL
        assert body["full_name"] == user.full_name
        assert body["role"] == UserRole.ADMINISTRATOR.value
        assert body["is_active"] is True

    def test_never_exposes_the_password_hash(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        response = api_client.get(ME_URL, headers=bearer(tokens["access_token"]))

        assert "hashed_password" not in response.json()
        assert user.hashed_password not in response.text

    def test_rejects_a_request_with_no_token(self, api_client: TestClient, user: User) -> None:
        response = api_client.get(ME_URL)

        assert response.status_code == 401
        assert response.json()["error"] == "missing_token"
        assert response.headers["www-authenticate"] == "Bearer"

    def test_rejects_a_malformed_token(self, api_client: TestClient, user: User) -> None:
        response = api_client.get(ME_URL, headers=bearer("not-a-real-token"))

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_rejects_an_expired_token_with_a_distinct_code(self, api_client: TestClient, user: User) -> None:
        # The distinct code tells the client to refresh rather than re-login.
        response = api_client.get(ME_URL, headers=bearer(expired_access_token(str(user.id))))

        assert response.status_code == 401
        assert response.json()["error"] == "token_expired"

    def test_rejects_a_token_signed_with_another_secret(self, api_client: TestClient, user: User) -> None:
        forged = forge_token(str(user.id), secret="a-completely-different-secret")

        response = api_client.get(ME_URL, headers=bearer(forged))

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_rejects_a_refresh_token_used_as_a_bearer_credential(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        response = api_client.get(ME_URL, headers=bearer(tokens["refresh_token"]))

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_rejects_a_non_bearer_authorization_scheme(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        response = api_client.get(ME_URL, headers={"Authorization": f"Basic {tokens['access_token']}"})

        assert response.status_code == 401

    def test_rejects_a_token_whose_account_was_disabled(
        self, api_client: TestClient, user: User, db_session: Session
    ) -> None:
        tokens = login(api_client)
        user.is_active = False
        db_session.commit()

        response = api_client.get(ME_URL, headers=bearer(tokens["access_token"]))

        assert response.status_code == 403
        assert response.json()["error"] == "account_disabled"


class TestRefresh:
    def test_issues_a_new_pair_from_the_cookie(self, api_client: TestClient, user: User) -> None:
        original = login(api_client)

        response = api_client.post(REFRESH_URL)

        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] != original["access_token"]
        assert body["refresh_token"] != original["refresh_token"]
        assert body["user"]["id"] == str(user.id)

    def test_the_new_access_token_works(self, api_client: TestClient, user: User) -> None:
        login(api_client)

        refreshed = api_client.post(REFRESH_URL).json()

        assert api_client.get(ME_URL, headers=bearer(refreshed["access_token"])).status_code == 200

    def test_issues_a_new_pair_from_the_request_body(self, api_client: TestClient, user: User) -> None:
        original = login(api_client)
        api_client.cookies.clear()

        response = api_client.post(REFRESH_URL, json={"refresh_token": original["refresh_token"]})

        assert response.status_code == 200
        assert response.json()["access_token"] != original["access_token"]

    def test_rotates_the_refresh_token_so_replay_fails(self, api_client: TestClient, user: User) -> None:
        original = login(api_client)
        assert api_client.post(REFRESH_URL).status_code == 200

        replay = api_client.post(REFRESH_URL, json={"refresh_token": original["refresh_token"]})

        assert replay.status_code == 401
        assert replay.json()["error"] == "invalid_token"

    def test_replaces_the_refresh_cookie(self, api_client: TestClient, user: User) -> None:
        login(api_client)
        first_cookie = api_client.cookies.get(settings.REFRESH_COOKIE_NAME)

        api_client.post(REFRESH_URL)

        assert api_client.cookies.get(settings.REFRESH_COOKIE_NAME) != first_cookie

    def test_rejects_a_request_with_no_token_at_all(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(REFRESH_URL)

        assert response.status_code == 401
        assert response.json()["error"] == "missing_token"

    def test_rejects_an_access_token_presented_for_refresh(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)
        api_client.cookies.clear()

        response = api_client.post(REFRESH_URL, json={"refresh_token": tokens["access_token"]})

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_rejects_an_expired_refresh_token(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(REFRESH_URL, json={"refresh_token": expired_refresh_token(str(user.id))})

        assert response.status_code == 401
        assert response.json()["error"] == "token_expired"

    def test_rejects_a_malformed_refresh_token(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(REFRESH_URL, json={"refresh_token": "not-a-token"})

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_rejects_refresh_after_the_account_is_disabled(
        self, api_client: TestClient, user: User, db_session: Session
    ) -> None:
        login(api_client)
        user.is_active = False
        db_session.commit()

        response = api_client.post(REFRESH_URL)

        assert response.status_code == 403
        assert response.json()["error"] == "account_disabled"


class TestLogout:
    def test_succeeds_and_reports_a_message(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        response = api_client.post(LOGOUT_URL, headers=bearer(tokens["access_token"]))

        assert response.status_code == 200
        assert response.json()["message"]

    def test_clears_the_refresh_cookie(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        api_client.post(LOGOUT_URL, headers=bearer(tokens["access_token"]))

        assert not api_client.cookies.get(settings.REFRESH_COOKIE_NAME)

    def test_the_access_token_stops_working(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        api_client.post(LOGOUT_URL, headers=bearer(tokens["access_token"]))

        response = api_client.get(ME_URL, headers=bearer(tokens["access_token"]))
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_the_refresh_token_stops_working(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        api_client.post(LOGOUT_URL, headers=bearer(tokens["access_token"]))

        response = api_client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
        assert response.status_code == 401

    def test_requires_authentication(self, api_client: TestClient, user: User) -> None:
        response = api_client.post(LOGOUT_URL)

        assert response.status_code == 401
        assert response.json()["error"] == "missing_token"

    def test_does_not_end_other_sessions(self, api_client: TestClient, user: User) -> None:
        # Signing out of one device must not sign the user out everywhere.
        first = login(api_client)
        second = login(api_client)

        api_client.post(LOGOUT_URL, headers=bearer(first["access_token"]))

        assert api_client.get(ME_URL, headers=bearer(second["access_token"])).status_code == 200

    def test_a_fresh_login_works_after_logout(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)
        api_client.post(LOGOUT_URL, headers=bearer(tokens["access_token"]))

        assert api_client.get(ME_URL, headers=bearer(login(api_client)["access_token"])).status_code == 200


class TestChangePassword:
    def test_changes_the_password(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        response = api_client.patch(
            CHANGE_PASSWORD_URL,
            headers=bearer(tokens["access_token"]),
            json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
        )

        assert response.status_code == 200
        assert response.json()["message"]

    def test_the_new_password_can_sign_in(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)
        api_client.patch(
            CHANGE_PASSWORD_URL,
            headers=bearer(tokens["access_token"]),
            json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
        )

        response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": "a-brand-new-password"})

        assert response.status_code == 200

    def test_the_old_password_is_rejected_afterwards(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)
        api_client.patch(
            CHANGE_PASSWORD_URL,
            headers=bearer(tokens["access_token"]),
            json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
        )

        response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})

        assert response.status_code == 401

    def test_stores_a_hash_not_the_plaintext(
        self, api_client: TestClient, user: User, db_session: Session
    ) -> None:
        tokens = login(api_client)

        api_client.patch(
            CHANGE_PASSWORD_URL,
            headers=bearer(tokens["access_token"]),
            json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
        )
        db_session.refresh(user)

        assert user.hashed_password.startswith("$2b$")
        assert "a-brand-new-password" not in user.hashed_password

    def test_rejects_a_wrong_current_password(self, api_client: TestClient, user: User) -> None:
        tokens = login(api_client)

        response = api_client.patch(
            CHANGE_PASSWORD_URL,
            headers=bearer(tokens["access_token"]),
            json={"current_password": "not-my-password", "new_password": "a-brand-new-password"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_password"

    def test_requires_authentication(self, api_client: TestClient, user: User) -> None:
        response = api_client.patch(
            CHANGE_PASSWORD_URL,
            json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
        )

        assert response.status_code == 401
        assert response.json()["error"] == "missing_token"

    @pytest.mark.parametrize(
        "body",
        [
            {"current_password": PASSWORD, "new_password": "short"},
            {"current_password": PASSWORD, "new_password": PASSWORD},
            {"current_password": PASSWORD},
            {"new_password": "a-brand-new-password"},
            {"current_password": "", "new_password": "a-brand-new-password"},
        ],
    )
    def test_rejects_invalid_request_bodies(
        self, api_client: TestClient, user: User, body: dict[str, str]
    ) -> None:
        tokens = login(api_client)

        response = api_client.patch(CHANGE_PASSWORD_URL, headers=bearer(tokens["access_token"]), json=body)

        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"


class TestFullSessionLifecycle:
    def test_login_use_refresh_use_logout(self, api_client: TestClient, user: User) -> None:
        # 1. Sign in.
        tokens = login(api_client)
        assert api_client.get(ME_URL, headers=bearer(tokens["access_token"])).status_code == 200

        # 2. Refresh (as the client would when the access token expires).
        refreshed = api_client.post(REFRESH_URL).json()
        assert api_client.get(ME_URL, headers=bearer(refreshed["access_token"])).status_code == 200

        # 3. The rotated-out token is dead; the current one still works.
        assert api_client.get(ME_URL, headers=bearer(tokens["access_token"])).status_code == 200

        # 4. Sign out, and confirm the session is fully closed.
        assert api_client.post(LOGOUT_URL, headers=bearer(refreshed["access_token"])).status_code == 200
        assert api_client.get(ME_URL, headers=bearer(refreshed["access_token"])).status_code == 401
        assert api_client.post(REFRESH_URL).status_code == 401


class TestRolesAreNotGated:
    @pytest.mark.parametrize("role", list(UserRole))
    def test_every_role_can_sign_in_and_read_itself(
        self, api_client: TestClient, make_user: MakeUser, role: UserRole
    ) -> None:
        # Authentication is identity-only: no endpoint here is role-restricted.
        email = f"{role.value}@example.com"
        make_user(email=email, password=PASSWORD, role=role)

        tokens = login(api_client, email=email)
        response = api_client.get(ME_URL, headers=bearer(tokens["access_token"]))

        assert response.status_code == 200
        assert response.json()["role"] == role.value


def response_text(body: dict[str, Any]) -> str:
    """Serialize a response body for substring assertions."""
    import json

    return json.dumps(body)
