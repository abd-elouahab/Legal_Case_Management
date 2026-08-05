"""Shared FastAPI dependencies.

Wires the request-scoped database session into the repository and service layers
and turns a Bearer token into the authenticated :class:`~models.user.User`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from core.config import settings
from core.exceptions import MissingTokenError
from core.security import TokenPayload
from db.session import get_db
from models.user import User
from repositories.case import CaseRepository
from repositories.document import DocumentRepository
from repositories.ocr import OcrRepository
from repositories.timeline import TimelineRepository
from repositories.user import UserRepository
from services.auth import AuthService
from services.case import CaseService
from services.document import DocumentService
from services.document_storage import DocumentStorageService
from services.login_throttle import LoginThrottle
from services.ocr import OcrService
from services.ocr_engine import OcrEngine, get_ocr_engine
from services.ocr_queue import OcrJobQueue
from services.ocr_worker import ocr_queue
from services.timeline import TimelineService
from services.token_revocation import TokenRevocationStore
from services.user import UserService

# auto_error=False so a missing header raises our own MissingTokenError (with a
# consistent error envelope) instead of FastAPI's bare 403 "Not authenticated".
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

DbSession = Annotated[Session, Depends(get_db)]


def get_user_repository(session: DbSession) -> UserRepository:
    """Provide a request-scoped user repository."""
    return UserRepository(session)


def get_case_repository(session: DbSession) -> CaseRepository:
    """Provide a request-scoped case repository."""
    return CaseRepository(session)


def get_token_revocation_store() -> TokenRevocationStore:
    """Provide the Redis-backed token denylist."""
    return TokenRevocationStore()


def get_login_throttle() -> LoginThrottle:
    """Provide the Redis-backed failed-login throttle."""
    return LoginThrottle()


def get_auth_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    revocations: Annotated[TokenRevocationStore, Depends(get_token_revocation_store)],
    throttle: Annotated[LoginThrottle, Depends(get_login_throttle)],
) -> AuthService:
    """Provide the authentication service with its collaborators injected."""
    return AuthService(users, revocations, throttle)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_user_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> UserService:
    """Provide the user management service."""
    return UserService(users)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


def get_timeline_repository(session: DbSession) -> TimelineRepository:
    """Provide a request-scoped timeline repository."""
    return TimelineRepository(session)


def get_timeline_service(
    events: Annotated[TimelineRepository, Depends(get_timeline_repository)],
    cases: Annotated[CaseRepository, Depends(get_case_repository)],
) -> TimelineService:
    """Provide the timeline service with its collaborators injected.

    The case repository is injected because a case's timeline may only be served
    to a caller party to that case — a rule the timeline repository has no
    business knowing about, and one that must not be re-implemented against a
    second copy of the case query.
    """
    return TimelineService(events, cases)


TimelineServiceDep = Annotated[TimelineService, Depends(get_timeline_service)]


def get_case_service(
    cases: Annotated[CaseRepository, Depends(get_case_repository)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
) -> CaseService:
    """Provide the case management service.

    The user repository is injected because assignment has to check that the
    assignee exists and holds the right role — a rule the case repository has no
    business knowing about, and one that must not be re-implemented against a
    second copy of the user query.

    The timeline service is injected because the case service *publishes* to it:
    creating, updating, archiving, restoring, and re-assigning a case are the
    events the case timeline is made of. This is the only place the real recorder
    is wired in — the service's own default records nothing — so a test asserts
    that this function supplies it.
    """
    return CaseService(cases, users, timeline=timeline)


CaseServiceDep = Annotated[CaseService, Depends(get_case_service)]


def get_document_repository(session: DbSession) -> DocumentRepository:
    """Provide a request-scoped document repository."""
    return DocumentRepository(session)


def get_document_storage() -> DocumentStorageService:
    """Provide the MinIO-backed document storage service.

    A dependency rather than a module-level singleton so an integration test can
    override it with a fake and exercise the endpoints without a running MinIO.
    """
    return DocumentStorageService()


def get_ocr_repository(session: DbSession) -> OcrRepository:
    """Provide a request-scoped OCR repository."""
    return OcrRepository(session)


def get_ocr_engine_dependency() -> OcrEngine:
    """Provide the configured OCR engine.

    A dependency rather than a module-level singleton so an integration test can
    override it with a fake and exercise the endpoints without an installed
    Tesseract — the same reason ``get_document_storage`` is one.
    """
    return get_ocr_engine()


def get_ocr_job_queue() -> OcrJobQueue:
    """Provide the application's background OCR queue.

    The process-wide pool from :mod:`services.ocr_worker`, not a fresh one per
    request: bounding concurrency is the whole point of it, and a pool per
    request would bound nothing. A test overrides this with an inline queue so
    the work happens synchronously and the assertion does not race a thread.
    """
    return ocr_queue


def get_ocr_service(
    results: Annotated[OcrRepository, Depends(get_ocr_repository)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    storage: Annotated[DocumentStorageService, Depends(get_document_storage)],
    engine: Annotated[OcrEngine, Depends(get_ocr_engine_dependency)],
    queue: Annotated[OcrJobQueue, Depends(get_ocr_job_queue)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
) -> OcrService:
    """Provide the OCR service with its collaborators injected.

    The document repository is injected because every OCR path starts from a
    document — a status read, a text read, and a retry all name one — and because
    OCR access follows document access, which follows case access. None of those
    rules may be re-implemented against a second copy of the document query.

    The timeline service is injected because the OCR service *publishes* to it:
    started, completed, failed, and retried are the OCR half of a document's
    history. As with the case and document services, this is one of only two
    places the real recorder is wired in — the other being the background worker,
    which has no request to take one from.
    """
    return OcrService(results, documents, storage, engine, queue, timeline=timeline)


OcrServiceDep = Annotated[OcrService, Depends(get_ocr_service)]


def get_document_service(
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    cases: Annotated[CaseRepository, Depends(get_case_repository)],
    storage: Annotated[DocumentStorageService, Depends(get_document_storage)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
    ocr: Annotated[OcrService, Depends(get_ocr_service)],
) -> DocumentService:
    """Provide the document management service with its collaborators injected.

    The case repository is injected because a document must belong to a case the
    caller may reach — a rule the document repository has no business knowing
    about, and one that must not be re-implemented against a second copy of the
    case query.

    The timeline service is injected because the document service *publishes* to
    it: uploads, metadata edits, replacements, deletions, and downloads are the
    document half of a case's history. As with the case service, this is the only
    place the real recorder is wired in.

    The OCR service is injected for the same shape of reason: storing a file is
    what schedules its text extraction. The document service depends on the
    narrow :class:`~services.ocr.OcrScheduler` protocol rather than on this
    class, so it cannot reach the read, retry, or monitoring side.
    """
    return DocumentService(documents, cases, storage, timeline=timeline, ocr=ocr)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


def get_client_ip(request: Request) -> str | None:
    """Best-effort client IP for per-address login throttling.

    ``X-Forwarded-For`` is only consulted when ``TRUST_PROXY_HEADERS`` is enabled.
    The header is client-supplied and trivially spoofed, so trusting it without a
    reverse proxy that overwrites it would let an attacker rotate the value to
    evade throttling — or set a victim's address to get *them* locked out. When
    trusted, the left-most entry is the original client per convention.

    Returns ``None`` when no address is available (e.g. ASGI transports with no
    client info), in which case only the per-account counter applies.
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client = forwarded.split(",")[0].strip()
            if client:
                return client

    return request.client.host if request.client else None


ClientIp = Annotated[str | None, Depends(get_client_ip)]


def get_access_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> str:
    """Extract the Bearer access token, or fail with a 401.

    Raises:
        MissingTokenError: no ``Authorization: Bearer <token>`` header present.
    """
    if credentials is None or not credentials.credentials:
        raise MissingTokenError
    return credentials.credentials


AccessToken = Annotated[str, Depends(get_access_token)]


def get_current_user(request: Request, token: AccessToken, auth: AuthServiceDep) -> User:
    """Resolve the authenticated user from the request's access token.

    Rejects missing, malformed, expired, and revoked tokens, and disabled
    accounts. The decoded payload is stashed on ``request.state`` so endpoints
    such as logout can revoke exactly this token without decoding it again.
    """
    user, payload = auth.resolve_access_token(token)
    request.state.access_token_payload = payload
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_access_token_payload(request: Request) -> TokenPayload:
    """Return the access-token payload attached by :func:`get_current_user`.

    Depends on ``get_current_user`` running first, so endpoints using this must
    also depend on :data:`CurrentUser`.
    """
    payload = getattr(request.state, "access_token_payload", None)
    if not isinstance(payload, TokenPayload):  # pragma: no cover - programming error
        raise MissingTokenError
    return payload


AccessTokenPayload = Annotated[TokenPayload, Depends(get_access_token_payload)]
