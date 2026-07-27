"""Integration tests for failed-login throttling over HTTP.

Verifies the 429 contract: status code, error envelope, ``Retry-After`` header,
and that a lockout cannot be bypassed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from models.user import User

if TYPE_CHECKING:
    from tests.conftest import InMemoryLoginThrottle

PASSWORD = "correct-horse-battery"
WRONG = "not-the-password"
EMAIL = "amina.benali@example.com"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
MAX_ATTEMPTS = settings.MAX_FAILED_LOGIN_ATTEMPTS

MakeUser = Callable[..., User]


@pytest.fixture
def user(make_user: MakeUser) -> User:
    return make_user(email=EMAIL, password=PASSWORD)


def attempt(client: TestClient, password: str = WRONG, email: str = EMAIL):  # type: ignore[no-untyped-def]
    return client.post(LOGIN_URL, json={"email": email, "password": password})


def exhaust_attempts(client: TestClient, email: str = EMAIL) -> None:
    """Fail enough times to trigger the lockout."""
    for _ in range(MAX_ATTEMPTS):
        attempt(client, email=email)


class TestLockoutResponse:
    def test_failures_below_the_threshold_stay_401(self, api_client: TestClient, user: User) -> None:
        for _ in range(MAX_ATTEMPTS - 1):
            response = attempt(api_client)
            assert response.status_code == 401
            assert response.json()["error"] == "invalid_credentials"

    def test_the_threshold_attempt_returns_429(self, api_client: TestClient, user: User) -> None:
        for _ in range(MAX_ATTEMPTS - 1):
            attempt(api_client)

        response = attempt(api_client)

        assert response.status_code == 429
        assert response.json()["error"] == "too_many_login_attempts"

    def test_subsequent_attempts_stay_429(self, api_client: TestClient, user: User) -> None:
        exhaust_attempts(api_client)

        response = attempt(api_client)

        assert response.status_code == 429

    def test_includes_a_retry_after_header(self, api_client: TestClient, user: User) -> None:
        exhaust_attempts(api_client)

        response = attempt(api_client)

        retry_after = int(response.headers["retry-after"])
        assert 0 < retry_after <= settings.login_lockout_duration.total_seconds()

    def test_the_message_tells_the_user_when_to_retry(self, api_client: TestClient, user: User) -> None:
        exhaust_attempts(api_client)

        message = attempt(api_client).json()["message"]

        assert "minute" in message.lower()

    def test_uses_the_standard_error_envelope(self, api_client: TestClient, user: User) -> None:
        exhaust_attempts(api_client)

        body = attempt(api_client).json()

        assert set(body) >= {"error", "message", "request_id", "details"}
        assert body["request_id"]

    def test_the_correct_password_is_refused_during_a_lockout(
        self, api_client: TestClient, user: User
    ) -> None:
        # The lockout must not be bypassable by finally guessing correctly.
        exhaust_attempts(api_client)

        response = attempt(api_client, password=PASSWORD)

        assert response.status_code == 429

    def test_no_session_is_issued_during_a_lockout(self, api_client: TestClient, user: User) -> None:
        exhaust_attempts(api_client)

        response = attempt(api_client, password=PASSWORD)

        assert "access_token" not in response.json()
        assert not api_client.cookies.get(settings.REFRESH_COOKIE_NAME)


class TestLockoutDoesNotLeakAccountExistence:
    def test_an_unknown_email_is_also_throttled(self, api_client: TestClient, user: User) -> None:
        # Otherwise 429-vs-401 would reveal which addresses are registered.
        exhaust_attempts(api_client, email="nobody@example.com")

        response = attempt(api_client, email="nobody@example.com")

        assert response.status_code == 429

    def test_known_and_unknown_emails_lock_out_identically(
        self, api_client: TestClient, make_user: MakeUser
    ) -> None:
        make_user(email="known@example.com", password=PASSWORD)

        exhaust_attempts(api_client, email="known@example.com")
        known = attempt(api_client, email="known@example.com")

        exhaust_attempts(api_client, email="unknown@example.com")
        unknown = attempt(api_client, email="unknown@example.com")

        assert known.status_code == unknown.status_code == 429
        assert known.json()["error"] == unknown.json()["error"]
        assert known.json()["message"] == unknown.json()["message"]


class TestLockoutRecovery:
    def test_a_successful_login_resets_the_counter(self, api_client: TestClient, user: User) -> None:
        for _ in range(MAX_ATTEMPTS - 1):
            attempt(api_client)

        assert attempt(api_client, password=PASSWORD).status_code == 200

        # The counter restarted, so a fresh near-miss run is still allowed.
        for _ in range(MAX_ATTEMPTS - 1):
            assert attempt(api_client).status_code == 401

    def test_login_works_again_once_the_lockout_expires(
        self, api_client: TestClient, user: User, throttle: InMemoryLoginThrottle
    ) -> None:
        exhaust_attempts(api_client)
        assert attempt(api_client, password=PASSWORD).status_code == 429

        throttle.advance(settings.login_lockout_duration + timedelta(seconds=1))

        assert attempt(api_client, password=PASSWORD).status_code == 200

    def test_a_disabled_account_keeps_its_actionable_error(
        self, api_client: TestClient, make_user: MakeUser
    ) -> None:
        # Correct credentials on a disabled account are not guesses, so the user
        # keeps seeing "account disabled" rather than an opaque 429.
        make_user(email="disabled@example.com", password=PASSWORD, is_active=False)

        for _ in range(MAX_ATTEMPTS + 2):
            response = attempt(api_client, password=PASSWORD, email="disabled@example.com")
            assert response.status_code == 403
            assert response.json()["error"] == "account_disabled"


class TestThrottlingIsDocumented:
    def test_login_documents_the_429_response(self, api_client: TestClient) -> None:
        schema = api_client.get("/openapi.json").json()

        responses = schema["paths"][LOGIN_URL]["post"]["responses"]
        assert "429" in responses
        assert "Retry-After" in responses["429"]["description"]
