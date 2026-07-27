"""Authentication endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.auth.AuthService`, and shape the HTTP response (including
the refresh cookie). No business logic lives here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Response, status

from api.deps import AccessTokenPayload, AuthServiceDep, ClientIp, CurrentUser
from api.v1.auth.cookies import clear_refresh_cookie, set_refresh_cookie
from core.config import settings
from core.exceptions import MissingTokenError
from schemas.auth import (
    ChangePasswordRequest,
    ChangePasswordResponse,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    TokenResponse,
)
from schemas.errors import ErrorResponse
from schemas.user import UserRead
from services.auth import TokenPair

router = APIRouter()

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "Missing, invalid, or expired token."}
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {"model": ErrorResponse, "description": "The account is disabled."}
}
_RATE_LIMITED: dict[int | str, dict[str, object]] = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": ErrorResponse,
        "description": (
            "Too many consecutive failed sign-in attempts for this account or IP "
            "address. The `Retry-After` header gives the wait in seconds."
        ),
    }
}

#: Name of the httpOnly cookie carrying the refresh token.
RefreshCookie = Annotated[str | None, Cookie(alias=settings.REFRESH_COOKIE_NAME, include_in_schema=False)]


def _token_response(response: Response, user: UserRead, tokens: TokenPair) -> TokenResponse:
    """Build the token payload and attach the refresh cookie."""
    set_refresh_cookie(response, tokens.refresh.token)
    return TokenResponse(
        access_token=tokens.access.token,
        refresh_token=tokens.refresh.token,
        expires_in=tokens.access_expires_in,
        user=user,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign in with email and password",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "Incorrect email or password."},
        **_FORBIDDEN,
        **_RATE_LIMITED,
    },
)
def login(
    payload: LoginRequest,
    response: Response,
    auth: AuthServiceDep,
    client_ip: ClientIp,
) -> TokenResponse:
    """Verify credentials and start a session.

    Returns an access token (short-lived, sent as a Bearer credential) and a
    refresh token. The refresh token is additionally set as an httpOnly cookie;
    browser clients should rely on that cookie and hold only the access token in
    memory.

    Repeated failures are throttled per account and per client IP: after too many
    consecutive failures the endpoint responds 429 until the lockout expires.
    """
    user, tokens = auth.login(payload.email, payload.password, ip_address=client_ip)
    return _token_response(response, UserRead.model_validate(user), tokens)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange a refresh token for a new token pair",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def refresh_tokens(
    response: Response,
    auth: AuthServiceDep,
    refresh_cookie: RefreshCookie = None,
    payload: RefreshRequest | None = None,
) -> TokenResponse:
    """Issue a fresh token pair from a valid refresh token.

    The token is read from the httpOnly cookie, falling back to the request body
    for non-browser clients. Refresh tokens are **single-use**: the presented
    token is revoked as part of this call and a new one is issued in its place.
    """
    token = (payload.refresh_token if payload else None) or refresh_cookie
    if not token:
        raise MissingTokenError("No refresh token was provided.")

    user, tokens = auth.refresh(token)
    return _token_response(response, UserRead.model_validate(user), tokens)


@router.post(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign out and revoke the current session",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def logout(
    response: Response,
    current_user: CurrentUser,
    access_payload: AccessTokenPayload,
    auth: AuthServiceDep,
    refresh_cookie: RefreshCookie = None,
    payload: RefreshRequest | None = None,
) -> MessageResponse:
    """Revoke this session's tokens and clear the refresh cookie.

    Both tokens are added to the revocation denylist, so neither works again even
    though JWTs are otherwise valid until they expire.
    """
    token = (payload.refresh_token if payload else None) or refresh_cookie
    auth.logout(current_user, access_payload.jti, access_payload.expires_at, token)
    clear_refresh_cookie(response)
    return MessageResponse(message="Signed out successfully.")


@router.get(
    "/me",
    response_model=UserRead,
    status_code=status.HTTP_200_OK,
    summary="Get the authenticated user",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_current_user(current_user: CurrentUser) -> UserRead:
    """Return the profile of the user owning the presented access token."""
    return UserRead.model_validate(current_user)


@router.patch(
    "/change-password",
    response_model=ChangePasswordResponse,
    status_code=status.HTTP_200_OK,
    summary="Change the authenticated user's password",
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse, "description": "Current password is incorrect."},
        **_UNAUTHORIZED,
        **_FORBIDDEN,
    },
)
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    current_user: CurrentUser,
    access_payload: AccessTokenPayload,
    auth: AuthServiceDep,
    refresh_cookie: RefreshCookie = None,
) -> ChangePasswordResponse:
    """Replace the current user's password and end every other session.

    All previously-issued tokens for this user are invalidated, so every other
    device must sign in again. The response carries a **replacement token pair**
    (and a new refresh cookie) so the device making the change stays signed in —
    clients must swap in the returned access token.
    """
    tokens = auth.change_password(
        current_user,
        payload.current_password,
        payload.new_password,
        current_access=access_payload,
        current_refresh_token=refresh_cookie,
    )

    set_refresh_cookie(response, tokens.refresh.token)
    return ChangePasswordResponse(
        message="Password changed successfully. Other devices have been signed out.",
        access_token=tokens.access.token,
        refresh_token=tokens.refresh.token,
        expires_in=tokens.access_expires_in,
        user=UserRead.model_validate(current_user),
    )
