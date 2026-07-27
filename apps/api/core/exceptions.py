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


class InvalidPasswordError(AppException):
    """The supplied current password did not match (password change)."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "invalid_password"
    message = "Current password is incorrect."


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
    logger.warning(
        "application_error",
        error_code=exc.error_code,
        status_code=exc.status_code,
        message=exc.message,
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
