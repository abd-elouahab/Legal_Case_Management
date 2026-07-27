"""Unit tests for failed-login throttling.

Covers the throttle contract through :class:`AuthService.login` (the only
network-facing entry point) plus the counter semantics directly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from core.config import settings
from core.exceptions import (
    InactiveAccountError,
    InvalidCredentialsError,
    TooManyLoginAttemptsError,
)
from models.user import User
from services.auth import AuthService

if TYPE_CHECKING:
    # Imported for typing only. At runtime pytest loads `conftest` under its own
    # module name, so importing the class here as well would create a *second*
    # distinct class object and break `isinstance` checks.
    from tests.conftest import InMemoryLoginThrottle

PASSWORD = "correct-horse-battery"
WRONG = "not-the-password"
EMAIL = "amina.benali@example.com"
IP = "203.0.113.10"

MakeUser = Callable[..., User]

MAX_ATTEMPTS = settings.MAX_FAILED_LOGIN_ATTEMPTS


def fail_login(auth_service: AuthService, email: str = EMAIL, ip: str | None = IP) -> None:
    """One failed attempt, swallowing the expected credential error."""
    with pytest.raises(InvalidCredentialsError):
        auth_service.login(email, WRONG, ip_address=ip)


class TestLockoutThreshold:
    def test_allows_attempts_below_the_threshold(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        make_user(email=EMAIL, password=PASSWORD)

        for _ in range(MAX_ATTEMPTS - 1):
            fail_login(auth_service)

        # Still reachable: the correct password works.
        user, _ = auth_service.login(EMAIL, PASSWORD, ip_address=IP)
        assert user.email == EMAIL

    def test_blocks_on_the_attempt_that_reaches_the_threshold(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # The threshold attempt itself is reported as 429, not the one after it.
        make_user(email=EMAIL, password=PASSWORD)

        for _ in range(MAX_ATTEMPTS - 1):
            fail_login(auth_service)

        with pytest.raises(TooManyLoginAttemptsError):
            auth_service.login(EMAIL, WRONG, ip_address=IP)

    def test_blocks_subsequent_attempts(self, auth_service: AuthService, make_user: MakeUser) -> None:
        make_user(email=EMAIL, password=PASSWORD)

        for _ in range(MAX_ATTEMPTS):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(EMAIL, WRONG, ip_address=IP)

        with pytest.raises(TooManyLoginAttemptsError):
            auth_service.login(EMAIL, WRONG, ip_address=IP)

    def test_the_correct_password_is_refused_during_a_lockout(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # The lockout is checked before credentials, so guessing right mid-lockout
        # does not grant access.
        make_user(email=EMAIL, password=PASSWORD)
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(EMAIL, WRONG, ip_address=IP)

        with pytest.raises(TooManyLoginAttemptsError):
            auth_service.login(EMAIL, PASSWORD, ip_address=IP)

    def test_reports_how_long_to_wait(self, auth_service: AuthService, make_user: MakeUser) -> None:
        make_user(email=EMAIL, password=PASSWORD)
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(EMAIL, WRONG, ip_address=IP)

        with pytest.raises(TooManyLoginAttemptsError) as error:
            auth_service.login(EMAIL, WRONG, ip_address=IP)

        expected = settings.login_lockout_duration.total_seconds()
        assert 0 < error.value.retry_after_seconds <= expected
        assert error.value.headers is not None
        assert error.value.headers["Retry-After"] == str(error.value.retry_after_seconds)

    def test_the_error_does_not_reveal_whether_the_account_exists(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # Unknown emails are counted too, so the lockout is not an enumeration oracle.
        make_user(email=EMAIL, password=PASSWORD)

        for _ in range(MAX_ATTEMPTS):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login("nobody@example.com", WRONG, ip_address=IP)

        with pytest.raises(TooManyLoginAttemptsError) as unknown:
            auth_service.login("nobody@example.com", WRONG, ip_address=IP)

        assert unknown.value.error_code == "too_many_login_attempts"


class TestConsecutiveSemantics:
    def test_a_successful_login_clears_the_counter(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # "Consecutive" failures: a user who mistypes then succeeds starts fresh.
        make_user(email=EMAIL, password=PASSWORD)

        for _ in range(MAX_ATTEMPTS - 1):
            fail_login(auth_service)
        auth_service.login(EMAIL, PASSWORD, ip_address=IP)

        # A full fresh run of failures is needed again before any lockout.
        for _ in range(MAX_ATTEMPTS - 1):
            fail_login(auth_service)
        assert auth_service.login(EMAIL, PASSWORD, ip_address=IP) is not None

    def test_the_lockout_expires(
        self, auth_service: AuthService, make_user: MakeUser, throttle: InMemoryLoginThrottle
    ) -> None:
        # `throttle` is the same instance injected into `auth_service`, so advancing
        # its clock is what the service sees.
        make_user(email=EMAIL, password=PASSWORD)
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(EMAIL, WRONG, ip_address=IP)

        with pytest.raises(TooManyLoginAttemptsError):
            auth_service.login(EMAIL, PASSWORD, ip_address=IP)

        throttle.advance(settings.login_lockout_duration + timedelta(seconds=1))

        user, _ = auth_service.login(EMAIL, PASSWORD, ip_address=IP)
        assert user.email == EMAIL

    def test_failures_outside_the_window_do_not_accumulate(
        self, auth_service: AuthService, make_user: MakeUser, throttle: InMemoryLoginThrottle
    ) -> None:
        make_user(email=EMAIL, password=PASSWORD)

        for _ in range(MAX_ATTEMPTS - 1):
            fail_login(auth_service)

        # Let the failure window lapse; the earlier attempts should be forgotten.
        throttle.advance(settings.login_failure_window + timedelta(seconds=1))

        for _ in range(MAX_ATTEMPTS - 1):
            fail_login(auth_service)

        assert auth_service.login(EMAIL, PASSWORD, ip_address=IP) is not None


class TestScopeIsolation:
    def test_locking_one_account_does_not_lock_another(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        make_user(email=EMAIL, password=PASSWORD)
        make_user(email="other@example.com", password=PASSWORD)

        # Fail against one account from a different address each time, so only the
        # per-account counter trips.
        for index in range(MAX_ATTEMPTS + 1):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(EMAIL, WRONG, ip_address=f"198.51.100.{index}")

        user, _ = auth_service.login("other@example.com", PASSWORD, ip_address="198.51.100.200")
        assert user.email == "other@example.com"

    def test_one_address_spraying_many_accounts_is_blocked(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # A per-account counter alone would never notice this; the per-IP counter does.
        make_user(email=EMAIL, password=PASSWORD)

        for index in range(MAX_ATTEMPTS):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(f"victim{index}@example.com", WRONG, ip_address=IP)

        with pytest.raises(TooManyLoginAttemptsError):
            auth_service.login(EMAIL, PASSWORD, ip_address=IP)

    def test_a_locked_account_is_locked_from_every_address(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        make_user(email=EMAIL, password=PASSWORD)

        for _ in range(MAX_ATTEMPTS):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(EMAIL, WRONG, ip_address=IP)

        with pytest.raises(TooManyLoginAttemptsError):
            auth_service.login(EMAIL, PASSWORD, ip_address="198.51.100.77")

    def test_works_without_a_client_address(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # When no IP is available only the per-account counter applies; it must
        # still lock out.
        make_user(email=EMAIL, password=PASSWORD)

        for _ in range(MAX_ATTEMPTS):
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(EMAIL, WRONG, ip_address=None)

        with pytest.raises(TooManyLoginAttemptsError):
            auth_service.login(EMAIL, PASSWORD, ip_address=None)


class TestWhatCountsAsAFailure:
    def test_a_disabled_account_does_not_count_toward_the_lockout(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # Presenting correct credentials for a disabled account is not a guess, and
        # the user should keep seeing the actionable message rather than a 429.
        make_user(email="disabled@example.com", password=PASSWORD, is_active=False)

        for _ in range(MAX_ATTEMPTS + 2):
            with pytest.raises(InactiveAccountError):
                auth_service.login("disabled@example.com", PASSWORD, ip_address=IP)

    def test_email_matching_is_case_insensitive_for_the_counter(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # Varying the case must not reset the counter.
        make_user(email=EMAIL, password=PASSWORD)

        for index in range(MAX_ATTEMPTS):
            variant = EMAIL.upper() if index % 2 else EMAIL
            with pytest.raises((InvalidCredentialsError, TooManyLoginAttemptsError)):
                auth_service.login(variant, WRONG, ip_address=IP)

        with pytest.raises(TooManyLoginAttemptsError):
            auth_service.login(EMAIL, PASSWORD, ip_address=IP)


class TestRetryAfterMessage:
    @pytest.mark.parametrize(
        ("seconds", "expected_fragment"),
        [(900, "15 minutes"), (60, "1 minute"), (30, "1 minute")],
    )
    def test_message_states_the_wait_in_minutes(self, seconds: int, expected_fragment: str) -> None:
        assert expected_fragment in TooManyLoginAttemptsError(seconds).message

    def test_retry_after_is_never_zero(self) -> None:
        # A zero Retry-After would tell clients to retry instantly.
        assert TooManyLoginAttemptsError(0).retry_after_seconds >= 1


class TestThrottleCounterSemantics:
    """Direct tests of the counter/lockout bookkeeping."""

    def test_check_is_clear_before_any_failure(self, throttle: InMemoryLoginThrottle) -> None:
        assert throttle.check(email=EMAIL, ip_address=IP).blocked is False

    def test_failures_below_the_threshold_do_not_block(self, throttle: InMemoryLoginThrottle) -> None:
        for _ in range(MAX_ATTEMPTS - 1):
            assert throttle.register_failure(email=EMAIL, ip_address=IP).blocked is False

        assert throttle.check(email=EMAIL, ip_address=IP).blocked is False

    def test_the_threshold_failure_blocks(self, throttle: InMemoryLoginThrottle) -> None:
        for _ in range(MAX_ATTEMPTS - 1):
            throttle.register_failure(email=EMAIL, ip_address=IP)

        assert throttle.register_failure(email=EMAIL, ip_address=IP).blocked is True
        assert throttle.check(email=EMAIL, ip_address=IP).blocked is True

    def test_reset_clears_an_active_lockout(self, throttle: InMemoryLoginThrottle) -> None:
        for _ in range(MAX_ATTEMPTS):
            throttle.register_failure(email=EMAIL, ip_address=IP)
        assert throttle.check(email=EMAIL, ip_address=IP).blocked is True

        throttle.reset(email=EMAIL, ip_address=IP)

        assert throttle.check(email=EMAIL, ip_address=IP).blocked is False

    def test_reported_wait_matches_the_configured_lockout(
        self, throttle: InMemoryLoginThrottle
    ) -> None:
        for _ in range(MAX_ATTEMPTS):
            throttle.register_failure(email=EMAIL, ip_address=IP)

        status = throttle.check(email=EMAIL, ip_address=IP)

        assert status.retry_after_seconds <= settings.login_lockout_duration.total_seconds()
        assert status.retry_after_seconds > 0


class TestRealThrottleAgainstRedis:
    """The production :class:`LoginThrottle`, exercised against live Redis.

    Skipped automatically when Redis is unavailable so the suite still runs
    without Docker.
    """

    @pytest.fixture
    def redis_throttle(self):  # type: ignore[no-untyped-def]
        from core.cache import redis_client
        from services.login_throttle import LoginThrottle

        try:
            redis_client.ping()
        except Exception:
            pytest.skip("Redis is not available")

        # Namespace this run so it cannot collide with real data or a rerun.
        unique_email = f"throttle-test-{datetime.now(UTC).timestamp()}@example.com"
        throttle = LoginThrottle()
        yield throttle, unique_email
        throttle.reset(email=unique_email, ip_address=IP)

    def test_counts_failures_and_locks_out(self, redis_throttle) -> None:  # type: ignore[no-untyped-def]
        throttle, email = redis_throttle

        for _ in range(MAX_ATTEMPTS - 1):
            assert throttle.register_failure(email=email, ip_address=IP).blocked is False

        assert throttle.register_failure(email=email, ip_address=IP).blocked is True
        assert throttle.check(email=email, ip_address=IP).blocked is True

    def test_reset_clears_the_lockout(self, redis_throttle) -> None:  # type: ignore[no-untyped-def]
        throttle, email = redis_throttle
        for _ in range(MAX_ATTEMPTS):
            throttle.register_failure(email=email, ip_address=IP)

        throttle.reset(email=email, ip_address=IP)

        assert throttle.check(email=email, ip_address=IP).blocked is False

    def test_lockout_carries_a_bounded_ttl(self, redis_throttle) -> None:  # type: ignore[no-untyped-def]
        # Keys must expire on their own, or the denylist grows without bound.
        throttle, email = redis_throttle
        for _ in range(MAX_ATTEMPTS):
            throttle.register_failure(email=email, ip_address=IP)

        status = throttle.check(email=email, ip_address=IP)

        assert 0 < status.retry_after_seconds <= settings.login_lockout_duration.total_seconds()
