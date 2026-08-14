"""Application exceptions and global exception handlers.

All error responses share a single JSON envelope so clients can rely on a
consistent shape. Internal exception details and stack traces are never exposed
to clients; unexpected errors are logged and returned as a generic 500.
"""

from __future__ import annotations

import contextlib
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.config import settings
from core.observability import (
    ErrorCategory,
    LogEvent,
    MonitoringComponent,
    SecurityEventType,
)
from schemas.errors import ErrorDetail, ErrorResponse
from services.error_tracker import get_error_tracker
from services.security_monitor import get_security_monitor

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Monitoring — attached here, and nowhere in the business modules
# --------------------------------------------------------------------------- #
#
# **This is where security monitoring happens, and the reason is worth stating.**
# ``22-monitoring.md`` asks the platform to watch failed logins, repeated
# authorization failures, invalid tokens, and excessive requests, while requiring
# that *"business modules should not contain monitoring-specific logic"*. Every
# one of those four is already an exception this module handles:
# :class:`InvalidCredentialsError`, :class:`AuthorizationError`,
# :class:`InvalidTokenError`, :class:`TooManyLoginAttemptsError`. So the
# classification happens **once, here**, over exceptions that were being raised
# anyway — and :class:`~services.auth.AuthService`,
# :class:`~services.authorization.AuthorizationService`, and every access policy
# stay exactly as they were, with no idea that monitoring exists.
#
# The mapping below is the whole of it: an error code, and what it means to
# somebody watching the platform for trouble.

#: Error codes that are security events, and the event each one is.
_SECURITY_EVENTS: dict[str, SecurityEventType] = {
    "invalid_credentials": SecurityEventType.LOGIN_FAILED,
    "too_many_login_attempts": SecurityEventType.LOGIN_LOCKED_OUT,
    "invalid_token": SecurityEventType.TOKEN_INVALID,
    "token_expired": SecurityEventType.TOKEN_EXPIRED,
    "missing_token": SecurityEventType.TOKEN_INVALID,
    "unauthorized": SecurityEventType.TOKEN_INVALID,
    "forbidden": SecurityEventType.PERMISSION_DENIED,
    "account_disabled": SecurityEventType.ACCOUNT_DISABLED,
}

#: Suffix identifying the platform's *per-resource* denial classes.
#:
#: Every one of them — ``CaseAccessDeniedError``, ``DocumentAccessDeniedError``,
#: ``OcrAccessDeniedError``, ``IndexAccessDeniedError``,
#: ``SearchAccessDeniedError``, ``TimelineAccessDeniedError`` — subclasses
#: :class:`AuthorizationError` and deliberately keeps its generic ``forbidden``
#: code, because a 403 body must never say which rule refused it. So the *class*
#: is what tells them apart, and telling them apart is worth doing: a caller
#: without the capability is a configuration question, while a caller who holds
#: it and asked for somebody else's case is the pattern that matters.
_RESOURCE_DENIAL_SUFFIX: str = "AccessDeniedError"


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


# --------------------------------------------------------------------------- #
# Case management errors
#
# Like the user-management errors above, these are informative: the caller has
# already proved both who they are and that they hold the matching ``cases:*``
# capability, so naming the problem helps them fix it. The one exception is
# :class:`CaseAccessDeniedError`, which is a *per-resource* denial and therefore
# says as little as the 403s in the authorization section.
# --------------------------------------------------------------------------- #


class CaseNotFoundError(AppException):
    """No case exists with the requested identifier."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "case_not_found"
    message = "Case not found."


class DuplicateCaseNumberError(AppException):
    """Another case already uses this case number.

    409 rather than 422, for the same reason as :class:`DuplicateEmailError`: the
    request is well-formed, and whether it can succeed depends on the current
    state of the system rather than on the payload.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "case_number_already_exists"
    message = "A case with this case number already exists."


class CaseNumberGenerationError(AppException):
    """A unique case number could not be generated after repeated attempts.

    Only reachable when concurrent creations keep colliding, which means the
    series is contended far beyond anything expected — a systems problem, not a
    client one, so it fails as a generic 500 with the specifics in the log.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "internal_error"
    message = "An unexpected error occurred."

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__()


class InvalidCaseTransitionError(AppException):
    """The requested status change is not a legal move from the current status.

    Names both statuses: the caller chose them, they are visible in the case they
    just read, and being told *which* move was refused is the difference between
    a fixable error and a mysterious one.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "invalid_case_transition"
    message = "This case cannot move to that status."

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"A case that is {current!r} cannot move to {target!r}.")


class InvalidAssignmentError(AppException):
    """The user being assigned does not exist, is disabled, or holds the wrong role.

    422 rather than 404: the identifier is a *field of the request body*, so this
    is a validation failure about the payload, not a statement that the URL's
    resource is missing.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "invalid_assignment"
    message = "The selected user cannot be assigned to this case."


class InvalidCaseDatesError(AppException):
    """The requested dates are not coherent with one another.

    Raised by the service rather than by the schema when a PATCH moves only one
    of the two dates: the other one is only knowable from the stored case, so the
    request is valid in isolation and invalid against reality.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "invalid_case_dates"
    message = "The next hearing date cannot be before the filing date."


class CaseAccessDeniedError(AuthorizationError):
    """The caller holds the capability but is not party to *this* case.

    A subclass of :class:`AuthorizationError`, so it answers **403** with the
    same generic body as every other denial — a lawyer who is refused learns
    only that they are refused, never which permission or assignment would have
    admitted them.

    403 rather than a concealing 404 is deliberate. Case ids are random UUIDs, so
    answering honestly does not enable enumeration, and a lawyer who follows a
    colleague's link needs to know the case exists and that they should ask to be
    assigned — a 404 would read as a broken link.
    """


# --------------------------------------------------------------------------- #
# Document management errors
#
# Same posture as the case errors above: a caller who has proved both who they
# are and that they hold the matching ``documents:*`` capability is told what is
# wrong, because it is information they could obtain from the endpoints they are
# entitled to use. :class:`DocumentAccessDeniedError` is the exception, being a
# per-resource denial.
# --------------------------------------------------------------------------- #


class DocumentNotFoundError(AppException):
    """No document exists with the requested identifier, or it has been deleted.

    A soft-deleted document answers the same 404 as one that never existed: it
    has been withdrawn from the case, and the row survives only so the history
    and the stored file remain recoverable — not so it can still be read through
    the API.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "document_not_found"
    message = "Document not found."


class DocumentVersionNotFoundError(AppException):
    """The document exists but has no such version."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "document_version_not_found"
    message = "This document has no such version."


class InvalidDocumentFileError(AppException):
    """The uploaded file is missing, empty, too large, or of an unsupported type.

    422 rather than 400: the file is a *field of the request*, so this is a
    validation failure about the payload, and it travels in the same envelope —
    with ``details`` naming the ``file`` field — as every other rejected field.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "invalid_document_file"
    message = "The uploaded file could not be accepted."


class DocumentPreviewUnavailableError(AppException):
    """The document's file type cannot be rendered in a browser.

    415 rather than 404 or 422: the document is there and the request is
    well-formed — the *representation* the endpoint would produce is the thing
    that does not exist. Clients are expected to fall back to the download
    endpoint, which the message says explicitly.
    """

    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    error_code = "preview_unavailable"
    message = "This file type cannot be previewed. Download it instead."


class DocumentStorageError(AppException):
    """MinIO could not be reached, or refused the operation.

    503 with a generic body: object storage being unavailable is an
    infrastructure fault the caller can neither fix nor learn anything useful
    from, and the underlying S3 error can name buckets, keys, and endpoints. The
    specifics go to the log through ``detail``, exactly as
    :class:`AuthorizationConfigurationError` does.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "document_storage_unavailable"
    message = "Document storage is currently unavailable. Please try again."

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__()


class DocumentAccessDeniedError(AuthorizationError):
    """The caller holds the capability but is not party to the document's case.

    A subclass of :class:`AuthorizationError`, so it answers **403** with the
    same generic body as every other denial. Document access follows case access
    exactly — see :mod:`services.document_access` — so the reasoning behind
    :class:`CaseAccessDeniedError` (403 rather than a concealing 404) applies
    unchanged.
    """


# --------------------------------------------------------------------------- #
# OCR processing errors
#
# Same posture as the document errors above: a caller who has proved both who
# they are and that they hold the matching ``ocr:*`` capability is told what is
# wrong. :class:`OcrAccessDeniedError` is the exception, being a per-resource
# denial.
#
# There is deliberately **no error for a failed extraction**. A failure is a
# recorded *state* of the run, not a failed request: the caller asking for the
# status gets a 200 describing a run whose status is ``failed``, with the reason
# on it. Answering 5xx would say the platform is broken when what happened is
# that a scan was unreadable.
# --------------------------------------------------------------------------- #


class OcrResultNotFoundError(AppException):
    """No OCR run exists for the requested document, or for that version.

    Distinct from "the document does not exist": a document of a type OCR does
    not apply to — a Word file, a plain-text note — has no run and never will,
    and the message says so rather than implying something went missing.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "ocr_result_not_found"
    message = "No text extraction record exists for this document."


class OcrUnsupportedFormatError(AppException):
    """OCR was requested for a file type it does not apply to.

    422 rather than 415: the request is well-formed and the endpoint's response
    type is not in question — what cannot be satisfied is the *operation*, for a
    reason that is a property of the resource the URL names.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "ocr_unsupported_format"
    message = "Text extraction is not available for this file type."

    def __init__(self, extension: str, supported: list[str]) -> None:
        super().__init__(
            f"Text extraction is not available for {extension!r} files. "
            f"Supported types: {', '.join(supported)}."
        )


class OcrAlreadyRunningError(AppException):
    """A retry was requested while extraction is queued or already running.

    409 rather than 202-and-ignore: the caller asked for a *new* run, and
    silently answering "done" for one already in flight would make the button
    they pressed indistinguishable from one that worked. Whether the run is
    queued or mid-extraction is stated, because it is the difference between
    "wait a moment" and "wait a while".
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "ocr_already_running"
    message = "Text extraction is already in progress for this document."

    def __init__(self, current: str) -> None:
        super().__init__(f"Text extraction for this document is already {current!r}.")


class InvalidOcrTransitionError(AppException):
    """A run was asked to move to a state that is not reachable from its own.

    Only provokable through the service's own API, not by a client: every HTTP
    path checks first. It exists so that a future caller — a scheduled
    re-processing job, a bulk import — cannot corrupt a run's lifecycle by
    writing a status directly, and so that the attempt is visible rather than
    silent.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "invalid_ocr_transition"
    message = "This text extraction cannot move to that state."

    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            f"A text extraction that is {current!r} cannot move to {target!r}."
        )


class OcrDisabledError(AppException):
    """OCR is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — the
    deployment has turned the pipeline off, which is a temporary condition an
    operator controls. Existing results stay readable; only new work is refused.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "ocr_disabled"
    message = "Text extraction is currently disabled on this platform."


class OcrAccessDeniedError(AuthorizationError):
    """The caller holds the capability but is not party to the document's case.

    A subclass of :class:`AuthorizationError`, so it answers **403** with the
    same generic body as every other denial. OCR access follows document access,
    which follows case access — see :mod:`services.ocr_access` — so the reasoning
    behind :class:`CaseAccessDeniedError` (403 rather than a concealing 404)
    applies unchanged.
    """


# --------------------------------------------------------------------------- #
# Document indexing errors
#
# Same posture as the OCR errors above, for the same reason: a caller who has
# proved both who they are and that they hold the matching ``indexing:*``
# capability is told what is wrong. :class:`IndexAccessDeniedError` is the
# exception, being a per-resource denial.
#
# There is deliberately **no error for a failed indexing run**. A failure is a
# recorded *state* of the run, not a failed request: the caller asking for the
# status gets a 200 describing a run whose status is ``failed``, with the reason
# on it. Answering 5xx would say the platform is broken when what happened is
# that one document had no extractable text.
# --------------------------------------------------------------------------- #


class DocumentIndexNotFoundError(AppException):
    """No index exists for the requested document, or for that version.

    Distinct from "the document does not exist": a document whose text has never
    been extracted has no index and will not have one until it does, and the
    message says so rather than implying something went missing.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "document_index_not_found"
    message = "No search index record exists for this document."


class IndexingNotReadyError(AppException):
    """Indexing was requested for a document whose text has not been extracted.

    409 rather than 422: nothing about the request is malformed, and nothing
    about the document is permanently unsuitable — it is a *sequencing* conflict.
    ``10-document-indexing.md`` begins its flow at "OCR Completed", so text is
    the precondition, and the right response tells the caller which state the
    document is actually in so they can wait for it or retry the extraction.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "indexing_not_ready"
    message = "This document has no extracted text to index yet."

    def __init__(self, ocr_status: str | None = None) -> None:
        if ocr_status is None:
            super().__init__(
                "This document version has not been processed for text extraction, so there is "
                "nothing to index yet."
            )
        else:
            super().__init__(
                f"Text extraction for this document version is {ocr_status!r}. Indexing can "
                f"begin once it has completed."
            )


class IndexingAlreadyRunningError(AppException):
    """A re-index was requested while one is queued or already running.

    409 rather than 202-and-ignore: the caller asked for a *new* run, and
    silently answering "done" for one already in flight would make the button
    they pressed indistinguishable from one that worked. Whether the run is
    queued or mid-index is stated, because it is the difference between "wait a
    moment" and "wait a while".
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "indexing_already_running"
    message = "This document is already being indexed."

    def __init__(self, current: str) -> None:
        super().__init__(f"Indexing for this document is already {current!r}.")


class InvalidIndexTransitionError(AppException):
    """An index was asked to move to a state that is not reachable from its own.

    Only provokable through the service's own API, not by a client: every HTTP
    path checks first. It exists so that a future caller — a scheduled
    re-indexing job, a bulk migration after a model change — cannot corrupt a
    run's lifecycle by writing a status directly, and so that the attempt is
    visible rather than silent.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "invalid_index_transition"
    message = "This indexing run cannot move to that state."

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"An indexing run that is {current!r} cannot move to {target!r}.")


class IndexingDisabledError(AppException):
    """Document indexing is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — the
    deployment has turned the pipeline off, which is a temporary condition an
    operator controls. Existing indexes stay readable; only new work is refused.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "indexing_disabled"
    message = "Document indexing is currently disabled on this platform."


class IndexAccessDeniedError(AuthorizationError):
    """The caller holds the capability but is not party to the document's case.

    A subclass of :class:`AuthorizationError`, so it answers **403** with the
    same generic body as every other denial. Index access follows document
    access, which follows case access — see :mod:`services.indexing_access` — so
    the reasoning behind :class:`CaseAccessDeniedError` (403 rather than a
    concealing 404) applies unchanged.
    """


# --------------------------------------------------------------------------- #
# Semantic search errors
#
# There is deliberately **no error for "nothing matched"**. An empty result set
# is a successful search: the corpus contains nothing near the query, which is an
# answer rather than a fault, and a 404 would make "no matches" indistinguishable
# from "the endpoint does not exist".
# --------------------------------------------------------------------------- #


class InvalidSearchQueryError(AppException):
    """The query carries nothing to search for.

    422 because the request *is* malformed: an empty query, one shorter than
    :data:`~core.search.MIN_QUERY_LENGTH`, or one made entirely of punctuation.
    Refused rather than answered, because embedding it would return a page of
    arbitrary passages that look like results.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "invalid_search_query"
    message = "Enter a search query of at least two characters."


class SearchFilterTooBroadError(AppException):
    """A metadata filter resolved to more documents than can be pushed down.

    422 rather than a silent truncation, and that is the whole point: narrowing
    the set to fit would drop matching documents with no way for anyone to
    notice, and a search that quietly ignores part of its own filter is worse
    than one that refuses. The message says how to narrow it.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "search_filter_too_broad"
    message = "This filter matches too many documents to search at once."

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"This filter matches more than {limit} documents. Narrow it — for example by "
            f"choosing a case — and search again."
        )


class SearchUnavailableError(AppException):
    """A dependency the search needs could not be reached.

    503 rather than 500: the request is valid and the platform is not broken —
    the embedding model or the vector database is unavailable, which is an
    operational condition that resolves without any change to the request. The
    caller may retry the identical search.

    The cause is carried as ``error_code`` so a client can distinguish "the model
    is not installed here" from "Qdrant is down" without parsing a sentence, and
    so the monitoring view can group failures the same way OCR and indexing do.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "search_unavailable"
    message = "Search is temporarily unavailable."

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, error_code=code)


class SearchDisabledError(AppException):
    """Semantic search is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — the
    deployment has turned retrieval off, which is a temporary condition an
    operator controls. Indexed documents are unaffected; only searching them is
    refused.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "search_disabled"
    message = "Semantic search is currently disabled on this platform."


class SearchAccessDeniedError(AuthorizationError):
    """The caller holds ``search:query`` but filtered to a case or document they
    are not party to.

    A subclass of :class:`AuthorizationError`, so it answers **403** with the
    same generic body as every other denial — and it is raised rather than
    silently returning an empty page, for exactly the reason
    :class:`TimelineAccessDeniedError` is: an inaccessible case and a case with no
    matching passages must not be indistinguishable.

    Note that this covers only an *explicit* filter. The implicit scope — the
    cases a caller is party to — is applied inside the vector query and never
    produces a denial, because a search the caller did not narrow has nothing to
    refuse: it simply searches what they can reach.
    """


# --------------------------------------------------------------------------- #
# RAG pipeline errors
#
# There is deliberately **no error for "no supporting evidence"**. An answer that
# says the documents do not support one is a *successful* run of the pipeline —
# `12-rag-pipeline.md`'s "No Evidence Handling" section requires exactly that
# response — and a 404 would make "the corpus does not cover this" look like "the
# endpoint does not exist".
#
# There is also **no per-resource denial of its own**. The pipeline retrieves
# only through :class:`~services.search.SearchService`, which already refuses a
# case or document the caller is not party to with
# :class:`SearchAccessDeniedError`; a second, parallel denial here would be a
# second authorization rule to keep in step with the first.
# --------------------------------------------------------------------------- #


class InvalidQuestionError(AppException):
    """The question carries nothing to answer.

    422 because the request *is* malformed: an empty question, one shorter than
    :data:`~core.rag.MIN_QUESTION_LENGTH`, or one made entirely of punctuation.
    Refused rather than answered, because retrieving on it returns arbitrary
    passages and the model then writes a confident paragraph out of them — which
    is precisely the fabrication this pipeline exists to prevent.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "invalid_question"
    message = "Enter a question of at least two characters."


class RagUnavailableError(AppException):
    """A dependency the pipeline needs could not be reached, or failed.

    503 rather than 500: the request is valid and the platform is not broken —
    retrieval or the language model is unavailable, which is an operational
    condition that resolves without any change to the request. The caller may
    retry the identical question.

    The cause is carried as ``error_code`` so a client can distinguish "no model
    is configured here" from "retrieval is down" from "the model timed out"
    without parsing a sentence, and so the monitoring view can group failures the
    same way OCR, indexing, and search do.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "rag_unavailable"
    message = "The AI assistant is temporarily unavailable."

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, error_code=code)


class RagDisabledError(AppException):
    """The RAG pipeline is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — the
    deployment has turned answer generation off, which is a temporary condition
    an operator controls. Documents, indexes, and semantic search are unaffected;
    only grounded answers are refused.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "rag_disabled"
    message = "The AI assistant is currently disabled on this platform."


# --------------------------------------------------------------------------- #
# AI Legal Assistant errors
#
# **There is deliberately no per-resource denial here**, and it is the one place
# on this platform where a 404 rather than a 403 is the right answer. Every other
# module refuses a resource the caller cannot reach with
# :class:`CaseAccessDeniedError` and its siblings, because a lawyer who follows a
# colleague's link to a case needs to know the case exists and that they should
# ask to be assigned. A conversation is the opposite: it is one user's private
# working material, nobody is ever meant to share a link to one, and confirming
# that another user's conversation *exists* is itself the disclosure the spec
# forbids ("The AI Assistant must never expose conversations belonging to another
# user"). So a conversation the caller does not own is simply not found — which is
# also what the repository returns, since every read there is keyed by owner.
#
# **And there is no error for an answer that found no supporting evidence.** That
# is a successful message, exactly as it is a successful run of the pipeline; it
# is persisted, shown, and rateable like any other.
# --------------------------------------------------------------------------- #


class ConversationNotFoundError(AppException):
    """No conversation with that identifier belongs to this caller.

    Covers three situations on purpose — it does not exist, it was deleted, or it
    belongs to somebody else — because telling them apart is precisely the
    disclosure this feature must not make. See the section note above.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "conversation_not_found"
    message = "Conversation not found."


class ConversationMessageNotFoundError(AppException):
    """No message with that identifier exists in this caller's conversation."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "conversation_message_not_found"
    message = "Message not found."


class ConversationArchivedError(AppException):
    """A message was sent to a conversation the user has archived.

    409 rather than 422: nothing about the request is malformed, and the
    conversation is not permanently unsuitable — it is a *state* conflict the
    caller resolves by restoring the thread or starting a new one, which the
    message says.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "conversation_archived"
    message = "This conversation is archived. Restore it, or start a new one, to continue."


class ConversationFullError(AppException):
    """The conversation has reached the platform's message ceiling.

    409 for the same reason: a state conflict rather than a bad request. The
    ceiling exists because a conversation is a working thread rather than an
    archive — and because an unbounded thread eventually cannot be loaded in one
    request.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "conversation_full"
    message = "This conversation has reached its message limit. Start a new one to continue."

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"This conversation has reached its limit of {limit} messages. Start a new one to "
            f"continue."
        )


class InvalidFeedbackTargetError(AppException):
    """Feedback was left on a message the user wrote themselves.

    422 because the identifier is a *path segment naming the wrong kind of
    resource*: the message exists and the caller may read it, but rating one's own
    question is not a judgement about the assistant, and silently accepting it
    would put noise into the evaluation data the spec says to persist.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "invalid_feedback_target"
    message = "Only the assistant's answers can be rated."


class AssistantDisabledError(AppException):
    """The AI assistant is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — the
    deployment has turned the conversational surface off, which is a temporary
    condition an operator controls. Existing conversations stay readable; only
    new ones and new messages are refused.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "assistant_disabled"
    message = "The AI assistant is currently disabled on this platform."


# --------------------------------------------------------------------------- #
# AI report generation errors
#
# Same posture as the OCR and indexing errors above, and for the same reason: a
# report is a *run*, so **there is deliberately no error for a report that failed
# to generate**. A failure is a recorded state of the run — the caller polling it
# gets a 200 describing a report whose status is ``failed``, with the reason on
# it. Answering 5xx would say the platform is broken when what happened is that
# one case file had nothing to build a report from.
#
# Export is the exception and is genuinely a *request* that can fail: the caller
# asked for a file in a format this deployment cannot produce, and there is no
# row to record that on.
#
# **There is no per-resource denial of its own for reading a report**, and this
# is the second place on the platform that conceals rather than refuses. A
# report is one user's private work product — ``14-ai-report-agent.md``:
# *"History must remain user-specific"*, *"generated reports remain private"* —
# so a report belonging to somebody else is simply not found, exactly as a
# conversation is. Generating one *is* refused with a 403, because that is a
# question about the **case**, and a lawyer who is refused a case needs to know
# it exists and that they should ask to be assigned.
# --------------------------------------------------------------------------- #


class ReportNotFoundError(AppException):
    """No report with that identifier belongs to this caller.

    Covers three situations on purpose — it does not exist, it was deleted, or it
    belongs to somebody else — because telling them apart is precisely the
    disclosure this feature must not make. See the section note above.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "report_not_found"
    message = "Report not found."


class ReportNotReadyError(AppException):
    """An export was requested for a report that has not finished generating.

    409 rather than 404 or 422: the report exists and the request is well-formed
    — it is a *sequencing* conflict the caller resolves by waiting, and the
    message says which state the run is actually in so they know whether that
    means seconds or a retry. The same shape :class:`IndexingNotReadyError` has.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "report_not_ready"
    message = "This report has not finished generating yet."

    def __init__(self, current: str) -> None:
        super().__init__(
            f"This report is {current!r}. It can be exported once generation has completed."
        )


class ReportAlreadyRunningError(AppException):
    """Regeneration was requested while a run is queued or already generating.

    409 rather than 202-and-ignore: the caller asked for a *fresh* report, and
    silently answering "done" for one already being written would make the button
    they pressed indistinguishable from one that worked — the same reasoning
    :class:`OcrAlreadyRunningError` and :class:`IndexingAlreadyRunningError`
    record.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "report_already_running"
    message = "This report is already being generated."

    def __init__(self, current: str) -> None:
        super().__init__(f"Generation for this report is already {current!r}.")


class TooManyActiveReportsError(AppException):
    """The caller already has as many reports in flight as the platform allows.

    429 rather than 409, and it is the only 429 on the platform besides the login
    lockout: this is a *rate* limit rather than a state conflict — nothing about
    the request is wrong, and the identical request will succeed once one of the
    caller's own runs finishes. A report costs one model call per section, so an
    unbounded queue is a way to spend a deployment's whole token budget from one
    browser tab.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "too_many_active_reports"
    message = "You already have several reports being generated. Wait for one to finish."

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"You already have {limit} report{'s' if limit != 1 else ''} being generated. Wait for "
            f"one to finish, then try again."
        )


class InvalidReportTransitionError(AppException):
    """A run was asked to move to a state that is not reachable from its own.

    Only provokable through the service's own API, not by a client: every HTTP
    path checks first. It exists so that a future caller — a scheduled report
    job, a bulk regeneration after a template change — cannot corrupt a run's
    lifecycle by writing a status directly, and so that the attempt is visible
    rather than silent.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = "invalid_report_transition"
    message = "This report cannot move to that state."

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"A report that is {current!r} cannot move to {target!r}.")


class ReportExportUnavailableError(AppException):
    """The requested export format cannot be produced on this deployment.

    503 rather than 415: the format is one the platform supports and the request
    is well-formed — the *library* that renders it is not installed here, which
    is an operational condition an operator fixes. The message names Markdown as
    the alternative, because it needs nothing beyond the platform itself, and
    that is the difference between a dead end and a workaround.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "report_export_unavailable"
    message = (
        "This report could not be exported in that format. Try Markdown, which needs no "
        "additional software."
    )

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message)


class ReportsDisabledError(AppException):
    """AI report generation is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — the
    deployment has turned generation off, which is a temporary condition an
    operator controls. Existing reports stay readable and exportable; only new
    runs are refused.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "reports_disabled"
    message = "AI report generation is currently disabled on this platform."


# --------------------------------------------------------------------------- #
# Real-time synchronization errors
#
# Only two, and the asymmetry with every other module above is the point: a
# WebSocket failure is not an HTTP failure. A malformed frame, an unauthorized
# topic, and a slow consumer are all answered **on the socket** — as an `error`
# frame or a close code from :mod:`core.realtime` — because by then the HTTP
# handshake has already succeeded and there is no status line left to set. The
# two below are the ones that happen on the *REST* surface this feature also
# exposes: its monitoring and presence views.
#
# There is deliberately **no per-resource denial class**. A topic the caller may
# not follow is refused by :mod:`services.realtime_access`, which owns no policy
# of its own — it delegates to the case and document policies, whose
# :class:`CaseAccessDeniedError` and :class:`DocumentAccessDeniedError` already
# say what a refusal means. Adding a third would be a third rule to keep in step
# with the first two.
# --------------------------------------------------------------------------- #


class RealtimeDisabledError(AppException):
    """Real-time synchronization is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — the
    deployment has turned the event channel off, which is a temporary condition
    an operator controls. **Every other feature is unaffected**, because none of
    them depends on it: the platform degrades to the REST API it was already
    built on, which is exactly what the spec's "graceful degradation" requires.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "realtime_disabled"
    message = "Live updates are currently disabled on this platform."


class EventPublicationError(AppException):
    """An event could not be built or dispatched.

    Never reaches a client, and that is its whole purpose. Publishing is a
    side effect of a business change that has **already committed**, so the
    dispatcher catches this and logs it rather than failing a request that
    succeeded — the same contract :meth:`~services.timeline.TimelineService.record`
    keeps, and for the same reason: a 500 on a completed operation invites a retry
    that duplicates the work.

    It exists as a typed exception rather than a bare ``ValueError`` so the
    dispatcher can tell "this publisher built a bad event" (a bug to log loudly)
    from an unexpected fault, and so ``detail`` carries the specifics into the log
    without any of them reaching a response body.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "internal_error"
    message = "An unexpected error occurred."

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__()


# --------------------------------------------------------------------------- #
# Notification errors
#
# Only two, and the small number is the point rather than an omission.
# ``16-notifications.md`` requires that *"failures should never affect business
# operations"* and that *"notification failures should be isolated"* — so
# everything that can go wrong while *creating* a notification is caught inside
# the subscriber, logged, and counted. None of it becomes an exception, because
# there is no request to fail: the business change that produced the event has
# already committed, which is the same contract
# :meth:`~services.timeline.TimelineService.record` keeps.
#
# What remains are the two failures on the *client-facing* surface: asking for
# somebody else's notification, and asking a deployment that has the feature
# switched off to create one.
#
# There is deliberately **no per-resource denial class**. A notification belongs
# to exactly one recipient, and every read in :mod:`repositories.notification` is
# keyed by them — so "somebody else's notification" and "no such notification"
# are the same answer, exactly as they are for a conversation and a report.
# --------------------------------------------------------------------------- #


class NotificationNotFoundError(AppException):
    """No notification with this identifier belongs to the caller.

    **404 rather than 403**, and the three cases it covers — it does not exist,
    it was archived, or it is somebody else's — are deliberately
    indistinguishable. The reasoning is the one
    :class:`ConversationNotFoundError` and :class:`ReportNotFoundError` record:
    confirming that another user's notification exists is itself the disclosure,
    and a notification names a case, an actor, and a moment.

    This is the opposite of :class:`CaseAccessDeniedError`'s 403, and the
    asymmetry is intentional: a lawyer needs to know a case exists so they can
    ask to be assigned to it, while nobody has any business learning what
    somebody else was told.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "notification_not_found"
    message = "Notification not found."


class NotificationsDisabledError(AppException):
    """Notification creation is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — an
    operator has turned creation off. Reading, filtering, and marking existing
    notifications are **unaffected**, because none of those creates anything, and
    a deployment that stopped new notifications must not also hide the ones
    people already have.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "notifications_disabled"
    message = "Notifications are currently disabled on this platform."


# --------------------------------------------------------------------------- #
# Dashboard errors
#
# Two, and the absence of a third is the point: the dashboard writes nothing, so
# there is no create, update, or delete path that could fail. Every other way a
# widget can go wrong is handled *inside* the response — a widget that could not
# be computed comes back marked unavailable, because the spec requires that one
# failing widget must not prevent the dashboard from loading.
# --------------------------------------------------------------------------- #


class DashboardWidgetNotFoundError(AppException):
    """No such widget is available to this caller.

    **404, and it deliberately conflates two situations**: a widget key this
    version does not define, and one the caller lacks the capabilities for. The
    reasoning is the one :class:`NotificationNotFoundError` records, applied to a
    catalog rather than to a row — answering 403 for the second would turn the
    per-widget endpoint into an oracle for "which analytics does this deployment
    have that I am not trusted with", which is precisely what the spec's *"users
    must never see widgets they are not authorized to access"* is protecting.

    A client never has to guess: ``GET /dashboard/widgets`` returns exactly the
    widgets this caller may refresh.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "dashboard_widget_not_found"
    message = "Dashboard widget not found."


class DashboardDisabledError(AppException):
    """The dashboard is switched off for this deployment.

    503 rather than 404: the capability exists and the request is valid — an
    operator has turned it off. Nothing else is affected, because the dashboard
    owns no data: every case, document, report, and notification stays exactly as
    readable through its own module's API as it was.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "dashboard_disabled"
    message = "The dashboard is currently disabled on this platform."


# --------------------------------------------------------------------------- #
# Settings errors
#
# Only two, and both are about the *request* rather than about a resource. The
# Settings module has no "not found": a setting the platform does not define is a
# request naming something that never existed, and a caller's own settings always
# exist — an account with no stored rows has the platform defaults, which is an
# answer rather than an absence. There is likewise no "settings disabled": every
# other feature switch on this platform turns off an outward-facing side effect
# or an expensive pipeline, and a deployment where nobody can change their
# password is not a configuration anybody wants.
# --------------------------------------------------------------------------- #


class UnknownSettingError(AppException):
    """A request named a setting this version of the platform does not define.

    **422 rather than 404**, because the identifier arrived in a request body
    rather than in a path: nothing was looked up and nothing was missing — the
    payload is simply not one the API accepts. It names the offending key in
    ``details`` so a client can say *which* row of a settings form was rejected,
    and so an older client sending a key a newer server dropped gets an
    actionable answer rather than a blanket refusal.

    Registries are read **tolerantly on the way out** (see
    :func:`~core.settings.user_setting_from_value`) and strictly on the way in,
    and the asymmetry is deliberate: a stored key an instance does not recognise
    must not stop somebody's settings page from loading, while an unrecognised
    key somebody is trying to *write* is a bug worth reporting.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "unknown_setting"
    message = "One or more settings are not recognised."


class InvalidSettingValueError(AppException):
    """A supplied value is not acceptable for the setting it was sent for.

    422, and — the part that matters — **nothing has been written when this is
    raised**. ``20-settings.md``: *"Invalid configuration should never corrupt
    stored preferences."* The service validates the whole batch before issuing a
    single statement, so a form with one bad field leaves every other field
    exactly as it was rather than half-applied. That is the same all-or-nothing
    rule Case Management applies to a request touching a field the caller cannot
    reach.

    ``details`` carries one entry per rejected key, each naming the key and why
    — never the value, which is the user's own text.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "invalid_setting_value"
    message = "One or more settings have an invalid value."


# --------------------------------------------------------------------------- #
# Timeline errors
#
# Only two, because the timeline is read-only over HTTP: events are published by
# the services that cause them, never by a client, so there is no create or
# update path that could fail.
# --------------------------------------------------------------------------- #


class TimelineEventNotFoundError(AppException):
    """No timeline event exists with the requested identifier.

    There is no soft-deleted variant to conceal, unlike a document: the timeline
    is append-only, so a 404 here means the event was never recorded.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "timeline_event_not_found"
    message = "Timeline event not found."


class TimelineAccessDeniedError(AuthorizationError):
    """The caller holds the capability but is not party to the event's case.

    A subclass of :class:`AuthorizationError`, so it answers **403** with the
    same generic body as every other denial. Timeline access follows case access
    exactly — see :mod:`services.timeline_access` — so the reasoning behind
    :class:`CaseAccessDeniedError` (403 rather than a concealing 404) applies
    unchanged.
    """


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


def _client_source(request: Request) -> str | None:
    """Return an opaque client identifier for security counting, or ``None``.

    Derived from ``X-Forwarded-For`` **only** when the deployment says it sits
    behind a trusted proxy, which is the same rule
    :mod:`services.login_throttle` applies and for the stronger of the two
    reasons: there, a spoofed header lets somebody get another client locked out;
    here it would only distort a count — but two rules for one question is how
    the two answers start to disagree.

    The value is handed to :class:`~services.security_monitor.SecurityMonitor`,
    which folds it into a salted digest and discards it. It is never stored,
    logged, or returned.
    """
    try:
        if settings.TRUST_PROXY_HEADERS:
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip() or None
        client = request.client
        return client.host if client else None
    except Exception:  # pragma: no cover - defensive
        return None


def _observe_application_error(request: Request, exc: AppException) -> None:
    """Record one handled failure as a metric, and as a security event if it is one.

    Called from the handler rather than from anything that raises, which is the
    property that keeps every business module free of monitoring code. Wrapped
    whole in a ``try``: this runs while the platform is already answering an
    error, and a monitoring failure here would replace a clean 403 with a 500.
    """
    try:
        event = _SECURITY_EVENTS.get(exc.error_code)
        if type(exc).__name__.endswith(_RESOURCE_DENIAL_SUFFIX):
            event = SecurityEventType.RESOURCE_ACCESS_DENIED
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS and event is None:
            event = SecurityEventType.RATE_LIMITED

        if event is not None:
            user = getattr(request.state, "user", None)
            role = getattr(getattr(user, "role", None), "value", None)
            get_security_monitor().record(
                event,
                role=role,
                reason=exc.error_code,
                source=_client_source(request),
            )

        # Only server faults are tracked as errors. A 404, a 403, and a 422 are
        # the platform working: recording them here would bury the failures an
        # operator can act on under the ordinary traffic of a public API, and the
        # metrics registry already counts every 4xx by route and status class.
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            get_error_tracker().record(
                category=ErrorCategory.HANDLED,
                component=MonitoringComponent.API,
                exception_type=type(exc).__name__,
                message=getattr(exc, "detail", None) or exc.message,
                location=_raise_location(exc),
                operation=_route_template(request),
                status_code=exc.status_code,
            )
    except Exception:  # pragma: no cover - defensive
        return


def _route_template(request: Request) -> str | None:
    """The matched route template, for grouping. Never the resolved path.

    Delegates to :func:`~core.middleware._route_of` rather than re-deriving it,
    so an error group's ``operation`` and the metric label for the same request
    are always the same string — two independent derivations is how a monitoring
    page ends up unable to join its own two halves.
    """
    try:
        from core.middleware import UNMATCHED_ROUTE, _route_of

        route = _route_of(request)
        return None if route == UNMATCHED_ROUTE else route
    except Exception:  # pragma: no cover - defensive
        return None


def _raise_location(exc: BaseException) -> str | None:
    """``file.py:line`` for the deepest frame of ``exc``, if it has a traceback."""
    try:
        traceback = exc.__traceback__
        if traceback is None:
            return None
        while traceback.tb_next is not None:
            traceback = traceback.tb_next
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        return f"{filename}:{traceback.tb_lineno}"
    except Exception:  # pragma: no cover - defensive
        return None


async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    # Server-fault exceptions are logged at error level; client faults stay at
    # warning so an operator's error feed is not filled with ordinary 4xx traffic.
    log = logger.error if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR else logger.warning
    log(
        LogEvent.APPLICATION_ERROR,
        error_code=exc.error_code,
        status_code=exc.status_code,
        message=exc.message,
        # Present on exceptions that keep internal specifics out of the response
        # body (e.g. an unknown permission identifier) but still need them logged.
        detail=getattr(exc, "detail", None),
    )
    _observe_application_error(request, exc)
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
    logger.exception(LogEvent.UNHANDLED_EXCEPTION, path=request.url.path, method=request.method)
    # Tracked as well as logged, and the difference is what each is for: the log
    # entry is one occurrence with its full traceback, and the tracked group is
    # *"this has now happened 240 times since 09:14"* — which is the sentence that
    # decides whether anybody is woken up. Wrapped, because a monitoring failure
    # inside the last-resort handler has nowhere left to be caught.
    with contextlib.suppress(Exception):
        get_error_tracker().record(
            category=ErrorCategory.UNHANDLED,
            component=MonitoringComponent.API,
            exception_type=type(exc).__name__,
            message=str(exc),
            location=_raise_location(exc),
            operation=_route_template(request),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
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
