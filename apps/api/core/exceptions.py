"""Application exceptions and global exception handlers.

All error responses share a single JSON envelope so clients can rely on a
consistent shape. Internal exception details and stack traces are never exposed
to clients; unexpected errors are logged and returned as a generic 500.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from schemas.errors import ErrorDetail, ErrorResponse

logger = structlog.get_logger(__name__)


class AppException(Exception):
    """Base class for expected, handled application errors.

    Subclass this for domain errors so they map onto consistent HTTP responses
    without leaking internal details.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "internal_error"
    message: str = "An unexpected error occurred."
    #: Extra response headers (e.g. ``WWW-Authenticate`` on 401 responses).
    headers: dict[str, str] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        error_code: str | None = None,
        status_code: int | None = None,
        details: list[ErrorDetail] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.error_code = error_code or self.error_code
        self.status_code = status_code or self.status_code
        self.details = details or []
        self.headers = headers or self.headers
        super().__init__(self.message)


class ServiceUnavailableError(AppException):
    """Raised when a required downstream dependency is unavailable."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "service_unavailable"
    message = "A required service is currently unavailable."


# --------------------------------------------------------------------------- #
# Authentication errors
#
# Every message below is deliberately generic: it must never reveal whether an
# email exists, whether a password was close, or any internal detail.
# --------------------------------------------------------------------------- #

#: Signals to the client that Bearer credentials are expected (RFC 6750).
_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


class AuthenticationError(AppException):
    """Base class for 401 responses (credentials missing, invalid, or expired)."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "unauthorized"
    message = "Authentication is required."
    headers = _BEARER_CHALLENGE


class InvalidCredentialsError(AuthenticationError):
    """Wrong email or password.

    The same error is returned for an unknown email and for a bad password so
    the endpoint cannot be used to enumerate accounts.
    """

    error_code = "invalid_credentials"
    message = "Incorrect email or password."


class MissingTokenError(AuthenticationError):
    """No credentials were presented on a protected endpoint."""

    error_code = "missing_token"
    message = "Authentication credentials were not provided."


class InvalidTokenError(AuthenticationError):
    """The presented token is malformed, mis-signed, revoked, or of the wrong type."""

    error_code = "invalid_token"
    message = "Authentication token is invalid."


class TokenExpiredError(AuthenticationError):
    """The presented token is past its expiry.

    Distinct from :class:`InvalidTokenError` so clients know a refresh is worth
    attempting rather than a full re-login.
    """

    error_code = "token_expired"
    message = "Authentication token has expired."


class InactiveAccountError(AppException):
    """The account exists and the credentials are correct, but it is disabled."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "account_disabled"
    message = "This account has been disabled."


# --------------------------------------------------------------------------- #
# Authorization errors
#
# A denial says only *that* access was refused — never which role or permission
# would have been required. Naming the missing permission would hand an attacker
# a map of the platform's capability model, so the specifics are logged
# server-side instead (see `services/authorization.py`).
# --------------------------------------------------------------------------- #


class AuthorizationError(AppException):
    """The caller is authenticated but not permitted to perform the action.

    Distinct from :class:`AuthenticationError` (401): the credentials are valid,
    so re-authenticating would not help.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "forbidden"
    message = "You do not have permission to perform this action."


class AuthorizationConfigurationError(AppException):
    """An authorization rule referenced an unknown role or permission.

    This is always a bug in the application (a hand-written identifier, a role
    with no policy entry), never something a client can provoke. It fails the
    request as a generic 500 — the caller learns nothing — while ``detail``
    carries the specifics into the log.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "internal_error"
    message = "An unexpected error occurred."

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__()


class InvalidPasswordError(AppException):
    """The supplied current password did not match (password change)."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "invalid_password"
    message = "Current password is incorrect."


# --------------------------------------------------------------------------- #
# User management errors
#
# Unlike the authentication errors above, these are *administrative* responses:
# the caller has already proved both who they are and that they may manage users,
# so naming the problem helps them fix it and reveals nothing they could not
# discover through the list endpoint they are entitled to use.
# --------------------------------------------------------------------------- #


class UserNotFoundError(AppException):
    """No user exists with the requested identifier."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "user_not_found"
    message = "User not found."


class DuplicateEmailError(AppException):
    """Another account already uses this email address.

    409 rather than 422: the request is well-formed, and whether it can succeed
    depends on the current state of the system rather than on the payload.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "email_already_exists"
    message = "A user with this email address already exists."


class SelfModificationError(AppException):
    """An administrator tried to disable or demote their own account.

    Not in the spec's error list, but the alternative is an administrator who can
    lock themselves — and, if they are the last one, the whole platform — out of
    user management, recoverable only by running ``scripts/create_user.py`` on the
    server. Every other account remains fully manageable.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "cannot_modify_own_account"
    message = "You cannot change your own role or account status."


class TooManyLoginAttemptsError(AppException):
    """Too many consecutive failed logins; the attempt is refused outright.

    Deliberately raised *before* credentials are checked, so a correct password
    cannot be used during a lockout and an attacker learns nothing from the
    response timing. The same lockout applies whether or not the email exists, so
    it does not become an account-enumeration oracle.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "too_many_login_attempts"
    message = "Too many failed sign-in attempts. Try again later."

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(retry_after_seconds, 1)
        minutes = max(1, round(self.retry_after_seconds / 60))
        super().__init__(
            f"Too many failed sign-in attempts. Try again in about {minutes} minute"
            f"{'s' if minutes != 1 else ''}.",
            # RFC 6585: Retry-After tells a well-behaved client when to come back.
            headers={"Retry-After": str(self.retry_after_seconds)},
        )


def _get_request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _build_response(
    *,
    status_code: int,
    error_code: str,
    message: str,
    request_id: str | None,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=error_code,
        message=message,
        request_id=request_id,
        details=details or [],
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    # Server-fault exceptions are logged at error level; client faults stay at
    # warning so an operator's error feed is not filled with ordinary 4xx traffic.
    log = logger.error if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR else logger.warning
    log(
        "application_error",
        error_code=exc.error_code,
        status_code=exc.status_code,
        message=exc.message,
        # Present on exceptions that keep internal specifics out of the response
        # body (e.g. an unknown permission identifier) but still need them logged.
        detail=getattr(exc, "detail", None),
    )
    return _build_response(
        status_code=exc.status_code,
        error_code=exc.error_code,
        message=exc.message,
        request_id=_get_request_id(request),
        details=exc.details,
        headers=exc.headers,
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail: Any = exc.detail
    message = detail if isinstance(detail, str) else "Request could not be completed."
    return _build_response(
        status_code=exc.status_code,
        error_code="http_error",
        message=message,
        request_id=_get_request_id(request),
    )


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        ErrorDetail(
            field=".".join(str(part) for part in error.get("loc", []) if part != "body"),
            message=error.get("msg", "Invalid value."),
        )
        for error in exc.errors()
    ]
    return _build_response(
        # HTTP_422_UNPROCESSABLE_ENTITY is deprecated in Starlette 1.x and emits a
        # DeprecationWarning on every validation failure; same status code (422).
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        error_code="validation_error",
        message="Request validation failed.",
        request_id=_get_request_id(request),
        details=details,
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log the full exception server-side; never expose internals to the client.
    logger.exception("unhandled_exception", path=request.url.path, method=request.method)
    return _build_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code="internal_error",
        message="An unexpected error occurred.",
        request_id=_get_request_id(request),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all global exception handlers on the application."""
    app.add_exception_handler(AppException, _app_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception_handler)
