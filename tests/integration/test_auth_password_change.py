"""Integration tests for password change and the session revocation it triggers.

A password change must end every session for that user. The device making the
change is handed a replacement token pair so it stays signed in; every other
device has to authenticate again.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.config import settings
from models.user import User

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-brand-new-password"
EMAIL = "amina.benali@example.com"

AUTH_PREFIX = f"{settings.API_V1_PREFIX}/auth"
LOGIN_URL = f"{AUTH_PREFIX}/login"
REFRESH_URL = f"{AUTH_PREFIX}/refresh"
ME_URL = f"{AUTH_PREFIX}/me"
CHANGE_PASSWORD_URL = f"{AUTH_PREFIX}/change-password"

MakeUser = Callable[..., User]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user(make_user: MakeUser) -> User:
    return make_user(email=EMAIL, password=PASSWORD)


def sign_in(client: TestClient, password: str = PASSWORD) -> dict[str, Any]:
    """Sign in and return the token payload, without disturbing client cookies."""
    response = client.post(LOGIN_URL, json={"email": EMAIL, "password": password})
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def change_password(
    client: TestClient,
    access_token: str,
    current: str = PASSWORD,
    new: str = NEW_PASSWORD,
):  # type: ignore[no-untyped-def]
    return client.patch(
        CHANGE_PASSWORD_URL,
        headers=bearer(access_token),
        json={"current_password": current, "new_password": new},
    )


class TestChangePasswordResponse:
    def test_succeeds_and_reports_the_revocation(self, api_client: TestClient, user: User) -> None:
        tokens = sign_in(api_client)

        response = change_password(api_client, tokens["access_token"])

        assert response.status_code == 200
        body = response.json()
        assert body["message"]
        assert body["sessions_revoked"] is True

    def test_returns_a_replacement_token_pair(self, api_client: TestClient, user: User) -> None:
        tokens = sign_in(api_client)

        body = change_password(api_client, tokens["access_token"]).json()

        assert body["access_token"] != tokens["access_token"]
        assert body["refresh_token"] != tokens["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60

    def test_returns_the_user(self, api_client: TestClient, user: User) -> None:
        tokens = sign_in(api_client)

        body = change_password(api_client, tokens["access_token"]).json()

        assert body["user"]["id"] == str(user.id)
        assert "hashed_password" not in body["user"]

    def test_sets_a_new_refresh_cookie(self, api_client: TestClient, user: User) -> None:
        tokens = sign_in(api_client)
        cookie_before = api_client.cookies.get(settings.REFRESH_COOKIE_NAME)

        change_password(api_client, tokens["access_token"])

        assert api_client.cookies.get(settings.REFRESH_COOKIE_NAME) != cookie_before

    def test_the_replacement_access_token_works(self, api_client: TestClient, user: User) -> None:
        tokens = sign_in(api_client)

        replacement = change_password(api_client, tokens["access_token"]).json()

        assert api_client.get(ME_URL, headers=bearer(replacement["access_token"])).status_code == 200

    def test_the_replacement_refresh_cookie_works(self, api_client: TestClient, user: User) -> None:
        tokens = sign_in(api_client)

        change_password(api_client, tokens["access_token"])

        assert api_client.post(REFRESH_URL).status_code == 200


class TestCurrentSessionIsReplaced:
    def test_the_old_access_token_stops_working(self, api_client: TestClient, user: User) -> None:
        tokens = sign_in(api_client)

        change_password(api_client, tokens["access_token"])

        response = api_client.get(ME_URL, headers=bearer(tokens["access_token"]))
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_the_old_refresh_token_stops_working(self, api_client: TestClient, user: User) -> None:
        tokens = sign_in(api_client)

        change_password(api_client, tokens["access_token"])

        response = api_client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
        assert response.status_code == 401


class TestOtherSessionsAreRevoked:
    def test_another_devices_access_token_is_rejected(self, api_client: TestClient, user: User) -> None:
        other_device = sign_in(api_client)
        this_device = sign_in(api_client)

        change_password(api_client, this_device["access_token"])

        response = api_client.get(ME_URL, headers=bearer(other_device["access_token"]))
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_another_devices_refresh_token_is_rejected(self, api_client: TestClient, user: User) -> None:
        other_device = sign_in(api_client)
        this_device = sign_in(api_client)

        change_password(api_client, this_device["access_token"])

        response = api_client.post(REFRESH_URL, json={"refresh_token": other_device["refresh_token"]})
        assert response.status_code == 401

    def test_every_other_device_is_signed_out(self, api_client: TestClient, user: User) -> None:
        others = [sign_in(api_client) for _ in range(3)]
        this_device = sign_in(api_client)

        change_password(api_client, this_device["access_token"])

        for session in others:
            assert api_client.get(ME_URL, headers=bearer(session["access_token"])).status_code == 401
            assert (
                api_client.post(REFRESH_URL, json={"refresh_token": session["refresh_token"]}).status_code
                == 401
            )

    def test_affected_devices_can_authenticate_again(self, api_client: TestClient, user: User) -> None:
        # The whole point: revoked devices are not locked out, just re-challenged.
        this_device = sign_in(api_client)
        change_password(api_client, this_device["access_token"])

        response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": NEW_PASSWORD})

        assert response.status_code == 200
        assert api_client.get(ME_URL, headers=bearer(response.json()["access_token"])).status_code == 200

    def test_another_users_session_is_untouched(
        self, api_client: TestClient, user: User, make_user: MakeUser
    ) -> None:
        make_user(email="bystander@example.com", password=PASSWORD)
        bystander = api_client.post(
            LOGIN_URL, json={"email": "bystander@example.com", "password": PASSWORD}
        ).json()
        this_device = sign_in(api_client)

        change_password(api_client, this_device["access_token"])

        assert api_client.get(ME_URL, headers=bearer(bystander["access_token"])).status_code == 200

    def test_the_session_generation_is_advanced(
        self, api_client: TestClient, user: User, db_session: Session
    ) -> None:
        tokens = sign_in(api_client)
        assert user.session_generation == 0

        change_password(api_client, tokens["access_token"])
        db_session.refresh(user)

        assert user.session_generation == 1


class TestFailedChangeLeavesSessionsAlone:
    def test_a_wrong_current_password_does_not_revoke_anything(
        self, api_client: TestClient, user: User
    ) -> None:
        other_device = sign_in(api_client)
        this_device = sign_in(api_client)

        response = change_password(api_client, this_device["access_token"], current="wrong-password")

        assert response.status_code == 400
        assert api_client.get(ME_URL, headers=bearer(other_device["access_token"])).status_code == 200
        assert api_client.get(ME_URL, headers=bearer(this_device["access_token"])).status_code == 200

    def test_an_invalid_new_password_does_not_revoke_anything(
        self, api_client: TestClient, user: User
    ) -> None:
        other_device = sign_in(api_client)
        this_device = sign_in(api_client)

        response = change_password(api_client, this_device["access_token"], new="short")

        assert response.status_code == 422
        assert api_client.get(ME_URL, headers=bearer(other_device["access_token"])).status_code == 200

    def test_an_unauthenticated_request_changes_nothing(
        self, api_client: TestClient, user: User, db_session: Session
    ) -> None:
        session = sign_in(api_client)

        response = api_client.patch(
            CHANGE_PASSWORD_URL,
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )

        assert response.status_code == 401
        db_session.refresh(user)
        assert user.session_generation == 0
        assert api_client.get(ME_URL, headers=bearer(session["access_token"])).status_code == 200


class TestRepeatedChanges:
    def test_each_change_revokes_the_previous_replacement(
        self, api_client: TestClient, user: User
    ) -> None:
        first = sign_in(api_client)
        second = change_password(api_client, first["access_token"], PASSWORD, "second-password-x").json()

        third = change_password(
            api_client, second["access_token"], "second-password-x", "third-password-x"
        ).json()

        assert third["access_token"] != second["access_token"]
        assert api_client.get(ME_URL, headers=bearer(second["access_token"])).status_code == 401
        assert api_client.get(ME_URL, headers=bearer(third["access_token"])).status_code == 200
