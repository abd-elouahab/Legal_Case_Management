"""Unit tests for the authentication workflow (``services.auth.AuthService``)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from core.exceptions import (
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidPasswordError,
    InvalidTokenError,
    TokenExpiredError,
)
from core.security import TokenType, create_access_token, decode_token, verify_password
from models.user import User, UserRole, UserStatus
from services.auth import AuthService, TokenPair
from tests.helpers import expired_access_token

PASSWORD = "correct-horse-battery"

MakeUser = Callable[..., User]


class TestAuthenticate:
    def test_returns_the_user_for_valid_credentials(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)

        assert auth_service.authenticate(user.email, PASSWORD).id == user.id

    def test_email_matching_is_case_insensitive(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(email="amina@example.com", password=PASSWORD)

        assert auth_service.authenticate("AMINA@EXAMPLE.COM", PASSWORD).id == user.id

    def test_rejects_a_wrong_password(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)

        with pytest.raises(InvalidCredentialsError):
            auth_service.authenticate(user.email, "wrong-password")

    def test_rejects_an_unknown_email(self, auth_service: AuthService) -> None:
        with pytest.raises(InvalidCredentialsError):
            auth_service.authenticate("nobody@example.com", PASSWORD)

    def test_unknown_email_and_wrong_password_are_indistinguishable(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # Identical error codes prevent account enumeration through the API.
        user = make_user(password=PASSWORD)

        with pytest.raises(InvalidCredentialsError) as unknown:
            auth_service.authenticate("nobody@example.com", PASSWORD)
        with pytest.raises(InvalidCredentialsError) as wrong:
            auth_service.authenticate(user.email, "wrong-password")

        assert unknown.value.error_code == wrong.value.error_code
        assert unknown.value.message == wrong.value.message

    def test_rejects_a_disabled_account(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD, is_active=False)

        with pytest.raises(InactiveAccountError):
            auth_service.authenticate(user.email, PASSWORD)

    def test_a_disabled_account_is_rejected_only_after_the_password_matches(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # A wrong password on a disabled account must not reveal that the account
        # exists but is disabled.
        user = make_user(password=PASSWORD, is_active=False)

        with pytest.raises(InvalidCredentialsError):
            auth_service.authenticate(user.email, "wrong-password")


class TestLogin:
    def test_issues_an_access_and_refresh_token_pair(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)

        returned_user, tokens = auth_service.login(user.email, PASSWORD)

        assert returned_user.id == user.id
        assert decode_token(tokens.access.token, expected_type=TokenType.ACCESS).subject == str(user.id)
        assert decode_token(tokens.refresh.token, expected_type=TokenType.REFRESH).subject == str(user.id)

    def test_records_the_last_login_timestamp(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        assert user.last_login_at is None

        auth_service.login(user.email, PASSWORD)

        assert user.last_login_at is not None

    def test_reports_the_access_token_lifetime_in_seconds(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        user = make_user(password=PASSWORD)

        _, tokens = auth_service.login(user.email, PASSWORD)

        assert tokens.access_expires_in == 15 * 60


class TestRefresh:
    def test_exchanges_a_refresh_token_for_a_new_pair(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, original = auth_service.login(user.email, PASSWORD)

        refreshed_user, renewed = auth_service.refresh(original.refresh.token)

        assert refreshed_user.id == user.id
        assert renewed.access.token != original.access.token
        assert renewed.refresh.token != original.refresh.token

    def test_rotates_the_refresh_token_so_it_cannot_be_replayed(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        user = make_user(password=PASSWORD)
        _, original = auth_service.login(user.email, PASSWORD)

        auth_service.refresh(original.refresh.token)

        with pytest.raises(InvalidTokenError):
            auth_service.refresh(original.refresh.token)

    def test_the_newly_issued_refresh_token_works(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, original = auth_service.login(user.email, PASSWORD)

        _, renewed = auth_service.refresh(original.refresh.token)

        assert auth_service.refresh(renewed.refresh.token) is not None

    def test_rejects_an_access_token_presented_for_refresh(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)

        with pytest.raises(InvalidTokenError):
            auth_service.refresh(tokens.access.token)

    def test_rejects_a_malformed_token(self, auth_service: AuthService) -> None:
        with pytest.raises(InvalidTokenError):
            auth_service.refresh("not-a-token")

    def test_rejects_a_refresh_token_whose_account_was_disabled(
        self, auth_service: AuthService, make_user: MakeUser, db_session: Session
    ) -> None:
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)

        user.status = UserStatus.INACTIVE
        db_session.commit()

        with pytest.raises(InactiveAccountError):
            auth_service.refresh(tokens.refresh.token)

    def test_rejects_a_token_for_a_user_that_no_longer_exists(
        self, auth_service: AuthService, make_user: MakeUser, db_session: Session
    ) -> None:
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)

        db_session.delete(user)
        db_session.commit()

        with pytest.raises(InvalidTokenError):
            auth_service.refresh(tokens.refresh.token)


class TestResolveAccessToken:
    def test_returns_the_user_and_payload(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)

        resolved, payload = auth_service.resolve_access_token(tokens.access.token)

        assert resolved.id == user.id
        assert payload.jti == tokens.access.jti

    def test_rejects_a_refresh_token(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)

        with pytest.raises(InvalidTokenError):
            auth_service.resolve_access_token(tokens.refresh.token)

    def test_reports_expiry_distinctly_from_invalidity(self, auth_service: AuthService, make_user: MakeUser) -> None:
        # Clients use this distinction to decide between refreshing and re-login.
        user = make_user(password=PASSWORD)

        with pytest.raises(TokenExpiredError):
            auth_service.resolve_access_token(expired_access_token(str(user.id)))

    def test_rejects_a_token_with_a_non_uuid_subject(self, auth_service: AuthService) -> None:
        issued = create_access_token("not-a-uuid")

        with pytest.raises(InvalidTokenError):
            auth_service.resolve_access_token(issued.token)


class TestLogout:
    def test_revokes_the_access_token(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)
        _, payload = auth_service.resolve_access_token(tokens.access.token)

        auth_service.logout(user, payload.jti, payload.expires_at, tokens.refresh.token)

        with pytest.raises(InvalidTokenError):
            auth_service.resolve_access_token(tokens.access.token)

    def test_revokes_the_refresh_token(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)
        _, payload = auth_service.resolve_access_token(tokens.access.token)

        auth_service.logout(user, payload.jti, payload.expires_at, tokens.refresh.token)

        with pytest.raises(InvalidTokenError):
            auth_service.refresh(tokens.refresh.token)

    def test_succeeds_without_a_refresh_token(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)
        _, payload = auth_service.resolve_access_token(tokens.access.token)

        auth_service.logout(user, payload.jti, payload.expires_at, None)

        with pytest.raises(InvalidTokenError):
            auth_service.resolve_access_token(tokens.access.token)

    def test_tolerates_an_unusable_refresh_token(self, auth_service: AuthService, make_user: MakeUser) -> None:
        # Logout must still clear the session even if the client sends garbage.
        user = make_user(password=PASSWORD)
        _, tokens = auth_service.login(user.email, PASSWORD)
        _, payload = auth_service.resolve_access_token(tokens.access.token)

        auth_service.logout(user, payload.jti, payload.expires_at, "not-a-token")

        with pytest.raises(InvalidTokenError):
            auth_service.resolve_access_token(tokens.access.token)

    def test_does_not_affect_a_different_session(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, first = auth_service.login(user.email, PASSWORD)
        _, second = auth_service.login(user.email, PASSWORD)
        _, first_payload = auth_service.resolve_access_token(first.access.token)

        auth_service.logout(user, first_payload.jti, first_payload.expires_at, first.refresh.token)

        assert auth_service.resolve_access_token(second.access.token)[0].id == user.id


NEW_PASSWORD = "a-brand-new-password"


def change_password(
    auth_service: AuthService,
    user: User,
    current: str = PASSWORD,
    new: str = NEW_PASSWORD,
    *,
    session: TokenPair | None = None,
) -> TokenPair:
    """Change a password on behalf of an active session.

    Establishes a session first when none is supplied, since the service needs the
    caller's current tokens in order to retire them. The session is always created
    with the real password (``PASSWORD``); ``current`` is only what gets submitted
    to the change itself, so tests can pass a wrong one.
    """
    if session is None:
        _, session = auth_service.login(user.email, PASSWORD)

    _, access_payload = auth_service.resolve_access_token(session.access.token)
    return auth_service.change_password(
        user,
        current,
        new,
        current_access=access_payload,
        current_refresh_token=session.refresh.token,
    )


class TestChangePassword:
    def test_replaces_the_stored_hash(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        original_hash = user.hashed_password

        change_password(auth_service, user)

        assert user.hashed_password != original_hash
        assert verify_password(NEW_PASSWORD, user.hashed_password)

    def test_the_old_password_stops_working(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)

        change_password(auth_service, user)

        with pytest.raises(InvalidCredentialsError):
            auth_service.authenticate(user.email, PASSWORD)

    def test_the_new_password_authenticates(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)

        change_password(auth_service, user)

        assert auth_service.authenticate(user.email, NEW_PASSWORD).id == user.id

    def test_rejects_a_wrong_current_password(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        original_hash = user.hashed_password

        with pytest.raises(InvalidPasswordError):
            change_password(auth_service, user, current="not-the-current-password")

        assert user.hashed_password == original_hash

    def test_a_failed_change_does_not_revoke_sessions(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        user = make_user(password=PASSWORD)
        _, session = auth_service.login(user.email, PASSWORD)

        with pytest.raises(InvalidPasswordError):
            change_password(auth_service, user, current="wrong", session=session)

        assert user.session_generation == 0
        assert auth_service.resolve_access_token(session.access.token)[0].id == user.id

    def test_the_password_is_never_stored_in_plain_text(
        self, auth_service: AuthService, make_user: MakeUser, db_session: Session
    ) -> None:
        user = make_user(password=PASSWORD)

        change_password(auth_service, user)
        db_session.refresh(user)

        assert NEW_PASSWORD not in user.hashed_password
        assert user.hashed_password.startswith("$2b$")


class TestChangePasswordRevokesSessions:
    """A password change must end every session, on every device."""

    def test_returns_a_replacement_token_pair(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        _, original = auth_service.login(user.email, PASSWORD)

        replacement = change_password(auth_service, user, session=original)

        assert replacement.access.token != original.access.token
        assert replacement.refresh.token != original.refresh.token

    def test_the_replacement_tokens_work(self, auth_service: AuthService, make_user: MakeUser) -> None:
        # The device performing the change stays signed in.
        user = make_user(password=PASSWORD)

        replacement = change_password(auth_service, user)

        assert auth_service.resolve_access_token(replacement.access.token)[0].id == user.id
        assert auth_service.refresh(replacement.refresh.token) is not None

    def test_advances_the_session_generation(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        assert user.session_generation == 0

        change_password(auth_service, user)

        assert user.session_generation == 1

    def test_each_change_advances_the_generation_again(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        user = make_user(password=PASSWORD)

        first = change_password(auth_service, user, PASSWORD, "second-password-here")
        change_password(auth_service, user, "second-password-here", "third-password-here", session=first)

        assert user.session_generation == 2
        # The pair issued by the first change is invalidated by the second.
        with pytest.raises(InvalidTokenError):
            auth_service.resolve_access_token(first.access.token)

    def test_another_devices_access_token_is_rejected(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        user = make_user(password=PASSWORD)
        _, other_device = auth_service.login(user.email, PASSWORD)
        _, this_device = auth_service.login(user.email, PASSWORD)

        change_password(auth_service, user, session=this_device)

        with pytest.raises(InvalidTokenError):
            auth_service.resolve_access_token(other_device.access.token)

    def test_another_devices_refresh_token_is_rejected(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # Without this, a stolen refresh token would outlive the password change.
        user = make_user(password=PASSWORD)
        _, other_device = auth_service.login(user.email, PASSWORD)
        _, this_device = auth_service.login(user.email, PASSWORD)

        change_password(auth_service, user, session=this_device)

        with pytest.raises(InvalidTokenError):
            auth_service.refresh(other_device.refresh.token)

    def test_every_other_device_is_signed_out(self, auth_service: AuthService, make_user: MakeUser) -> None:
        user = make_user(password=PASSWORD)
        others = [auth_service.login(user.email, PASSWORD)[1] for _ in range(3)]
        _, this_device = auth_service.login(user.email, PASSWORD)

        change_password(auth_service, user, session=this_device)

        for session in others:
            with pytest.raises(InvalidTokenError):
                auth_service.resolve_access_token(session.access.token)
            with pytest.raises(InvalidTokenError):
                auth_service.refresh(session.refresh.token)

    def test_the_callers_own_old_tokens_are_retired(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # The pair used to make the change is replaced, not left alive alongside
        # the new one.
        user = make_user(password=PASSWORD)
        _, original = auth_service.login(user.email, PASSWORD)

        change_password(auth_service, user, session=original)

        with pytest.raises(InvalidTokenError):
            auth_service.resolve_access_token(original.access.token)
        with pytest.raises(InvalidTokenError):
            auth_service.refresh(original.refresh.token)

    def test_a_fresh_login_with_the_new_password_works(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        # Affected devices must be able to authenticate again.
        user = make_user(password=PASSWORD)
        change_password(auth_service, user)

        _, tokens = auth_service.login(user.email, NEW_PASSWORD)

        assert auth_service.resolve_access_token(tokens.access.token)[0].id == user.id

    def test_another_users_sessions_are_untouched(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        user = make_user(email="first@example.com", password=PASSWORD)
        bystander = make_user(email="second@example.com", password=PASSWORD)
        _, bystander_session = auth_service.login(bystander.email, PASSWORD)

        change_password(auth_service, user)

        assert auth_service.resolve_access_token(bystander_session.access.token)[0].id == bystander.id

    def test_tolerates_a_missing_refresh_token(self, auth_service: AuthService, make_user: MakeUser) -> None:
        # A non-browser client may not send one; the cut-off still applies.
        user = make_user(password=PASSWORD)
        _, original = auth_service.login(user.email, PASSWORD)
        _, access_payload = auth_service.resolve_access_token(original.access.token)

        auth_service.change_password(
            user,
            PASSWORD,
            NEW_PASSWORD,
            current_access=access_payload,
            current_refresh_token=None,
        )

        with pytest.raises(InvalidTokenError):
            auth_service.refresh(original.refresh.token)

    def test_tolerates_an_unusable_refresh_token(
        self, auth_service: AuthService, make_user: MakeUser
    ) -> None:
        user = make_user(password=PASSWORD)
        _, original = auth_service.login(user.email, PASSWORD)
        _, access_payload = auth_service.resolve_access_token(original.access.token)

        replacement = auth_service.change_password(
            user,
            PASSWORD,
            NEW_PASSWORD,
            current_access=access_payload,
            current_refresh_token="not-a-token",
        )

        assert auth_service.resolve_access_token(replacement.access.token)[0].id == user.id


class TestRoleIsIdentityOnly:
    @pytest.mark.parametrize("role", list(UserRole))
    def test_every_role_can_authenticate(
        self, auth_service: AuthService, make_user: MakeUser, role: UserRole
    ) -> None:
        # Authentication establishes identity only: no role is privileged or
        # blocked at this layer. Authorization is a separate feature.
        user = make_user(email=f"{role.value}@example.com", password=PASSWORD, role=role)

        assert auth_service.authenticate(user.email, PASSWORD).role is role
