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
from repositories.conversation import ConversationRepository
from repositories.document import DocumentRepository
from repositories.indexing import IndexingRepository
from repositories.ocr import OcrRepository
from repositories.search import SearchRepository
from repositories.timeline import TimelineRepository
from repositories.user import UserRepository
from services.assistant import AssistantService
from services.assistant_metrics import AssistantMetricsRecorder, get_assistant_metrics
from services.auth import AuthService
from services.case import CaseService
from services.chunking import Chunker, get_chunker
from services.document import DocumentService
from services.document_storage import DocumentStorageService
from services.embedding import Embedder, get_embedder
from services.indexing import IndexingService, IndexJob
from services.indexing_worker import index_queue
from services.job_queue import JobQueue
from services.llm import LLMProvider, get_llm_provider
from services.login_throttle import LoginThrottle
from services.ocr import OcrService
from services.ocr_engine import OcrEngine, get_ocr_engine
from services.ocr_queue import OcrJobQueue
from services.ocr_worker import ocr_queue
from services.prompts import PromptLibrary, get_prompt_library
from services.rag import RagService
from services.rag_metrics import RagMetricsRecorder, get_rag_metrics
from services.search import SearchService
from services.search_metrics import SearchMetricsRecorder, get_search_metrics
from services.search_ranking import Ranker, get_ranker
from services.suggestions import FollowUpSuggester, get_follow_up_suggester
from services.timeline import TimelineService
from services.token_revocation import TokenRevocationStore
from services.user import UserService
from services.vector_search import VectorSearcher, get_vector_searcher
from services.vector_store import VectorStore, get_vector_store

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


def get_indexing_repository(session: DbSession) -> IndexingRepository:
    """Provide a request-scoped document-index repository."""
    return IndexingRepository(session)


def get_chunker_dependency() -> Chunker:
    """Provide the configured text chunker.

    A dependency rather than a module-level singleton so an integration test can
    override it with a fake — the same reason ``get_document_storage`` and
    ``get_ocr_engine_dependency`` are ones.
    """
    return get_chunker()


def get_embedder_dependency() -> Embedder:
    """Provide the configured embedding model.

    The instance is process-wide (see :func:`~services.embedding.get_embedder`) —
    a model measured in gigabytes must not be built per request — but it is still
    reached through a dependency so a test can override it and exercise the
    endpoints without a downloaded model.
    """
    return get_embedder()


def get_vector_store_dependency() -> VectorStore:
    """Provide the configured vector store.

    A dependency for the same reason as the two above: the monitoring endpoint
    asks it whether Qdrant is reachable, and a test must be able to answer that
    without a running Qdrant.
    """
    return get_vector_store()


def get_index_job_queue() -> JobQueue[IndexJob]:
    """Provide the application's background indexing queue.

    The process-wide pool from :mod:`services.indexing_worker`, not a fresh one
    per request: bounding concurrency is the whole point of it, and a pool per
    request would bound nothing. A test overrides this with an inline queue so
    the work happens synchronously and the assertion does not race a thread.
    """
    return index_queue


def get_indexing_service(
    indexes: Annotated[IndexingRepository, Depends(get_indexing_repository)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    results: Annotated[OcrRepository, Depends(get_ocr_repository)],
    chunker: Annotated[Chunker, Depends(get_chunker_dependency)],
    embedder: Annotated[Embedder, Depends(get_embedder_dependency)],
    vectors: Annotated[VectorStore, Depends(get_vector_store_dependency)],
    queue: Annotated[JobQueue[IndexJob], Depends(get_index_job_queue)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
) -> IndexingService:
    """Provide the indexing service with its collaborators injected.

    The document repository is injected because every indexing path starts from a
    document, and because index access follows document access, which follows
    case access. The OCR repository is injected because the *input* to indexing
    is the text OCR persisted: a re-index reads it back, and the precondition
    "this version has completed extraction" is a question only that repository
    can answer. Neither rule may be re-implemented against a second copy of its
    query.

    The timeline service is injected because the indexing service *publishes* to
    it: started, completed, failed, and retried are the indexing half of a
    document's history.
    """
    return IndexingService(
        indexes,
        documents,
        results,
        chunker,
        embedder,
        vectors,
        queue,
        timeline=timeline,
    )


IndexingServiceDep = Annotated[IndexingService, Depends(get_indexing_service)]


def get_search_repository(session: DbSession) -> SearchRepository:
    """Provide a request-scoped semantic-search repository."""
    return SearchRepository(session)


def get_vector_searcher_dependency() -> VectorSearcher:
    """Provide the configured vector searcher.

    Separate from ``get_vector_store_dependency`` on purpose, and not merely
    because they are different classes: writing and reading the vector index are
    two capabilities the platform keeps apart (see
    :mod:`services.vector_search`), and one dependency yielding both would be the
    join that undoes it.
    """
    return get_vector_searcher()


def get_ranker_dependency() -> Ranker:
    """Provide the configured search ranker.

    A dependency rather than a module-level singleton so an integration test can
    override it with a fake — and so a future cross-encoder reranker, which does
    hold a model, can be made process-wide here without touching the service.
    """
    return get_ranker()


def get_search_metrics_recorder() -> SearchMetricsRecorder:
    """Provide the process-wide search metrics recorder.

    Process-wide rather than per request for the same reason the job queues are:
    a counter rebuilt on every request counts to one. A test overrides this so
    assertions about the metrics endpoint do not depend on traffic another test
    produced.
    """
    return get_search_metrics()


def get_search_service(
    searches: Annotated[SearchRepository, Depends(get_search_repository)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    cases: Annotated[CaseRepository, Depends(get_case_repository)],
    embedder: Annotated[Embedder, Depends(get_embedder_dependency)],
    searcher: Annotated[VectorSearcher, Depends(get_vector_searcher_dependency)],
    ranker: Annotated[Ranker, Depends(get_ranker_dependency)],
    metrics: Annotated[SearchMetricsRecorder, Depends(get_search_metrics_recorder)],
) -> SearchService:
    """Provide the semantic-search service with its collaborators injected.

    The **embedder is the same dependency the indexing service takes**, and that
    is the point rather than a convenience: `ai-architecture.md` requires one
    model for documents and for queries, and sharing the provider makes the two
    impossible to configure apart.

    The case and document repositories are injected because a filter naming
    either must be checked against the caller's assignments before it is trusted;
    the search repository because the authorization scope has to cross into
    Qdrant as a set of case identifiers, which only SQL can produce.

    There is deliberately **no timeline recorder and no job queue**: a search
    changes nothing, so it has nothing to announce and nothing to schedule.
    """
    return SearchService(
        searches,
        documents,
        cases,
        embedder,
        searcher,
        ranker,
        metrics=metrics,
    )


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]


def get_prompt_library_dependency() -> PromptLibrary:
    """Provide the configured prompt library.

    The instance is process-wide (see
    :func:`~services.prompts.get_prompt_library`) because it owns Jinja's
    compiled-template cache, but it is still reached through a dependency so a
    test can override it and exercise the pipeline against a template of its own
    — the same reason the embedder is a dependency despite being a singleton.
    """
    return get_prompt_library()


def get_llm_provider_dependency() -> LLMProvider:
    """Provide the configured language-model provider.

    Process-wide for the same reason: it owns an HTTP client and its connection
    pool. Reached through a dependency so an integration test can substitute a
    scripted provider and exercise the endpoints **without an API key, without a
    network call, and without paying a vendor per assertion** — which is the only
    way the failure paths (a timeout, a refusal, an empty completion) are
    testable at all.
    """
    return get_llm_provider()


def get_rag_metrics_recorder() -> RagMetricsRecorder:
    """Provide the process-wide RAG metrics recorder.

    Process-wide rather than per request for the same reason the search recorder
    is: a counter rebuilt on every request counts to one. A test overrides this
    so assertions about the metrics endpoint do not depend on traffic another
    test produced.
    """
    return get_rag_metrics()


def get_rag_service(
    search: Annotated[SearchService, Depends(get_search_service)],
    prompts: Annotated[PromptLibrary, Depends(get_prompt_library_dependency)],
    provider: Annotated[LLMProvider, Depends(get_llm_provider_dependency)],
    metrics: Annotated[RagMetricsRecorder, Depends(get_rag_metrics_recorder)],
) -> RagService:
    """Provide the RAG pipeline with its collaborators injected.

    **The search service is the pipeline's only retrieval collaborator, and that
    is the load-bearing line in this function.** ``12-rag-pipeline.md`` forbids
    querying the vector database directly when a retrieval abstraction exists,
    and this is where that would be undone: injecting a ``VectorSearcher`` or a
    ``SearchRepository`` here would give the pipeline a way to reach a passage
    without the case scope the search service applies. It takes neither, so the
    authorization chain — passage → document → case — holds structurally rather
    than by discipline.

    There is deliberately **no timeline recorder, no job queue, and no database
    session**: answering a question changes nothing, so it has nothing to
    announce, nothing to schedule, and nothing to write.
    """
    return RagService(search, prompts, provider, metrics=metrics)


RagServiceDep = Annotated[RagService, Depends(get_rag_service)]


def get_conversation_repository(session: DbSession) -> ConversationRepository:
    """Provide a request-scoped conversation repository."""
    return ConversationRepository(session)


def get_follow_up_suggester_dependency() -> FollowUpSuggester:
    """Provide the configured follow-up suggester.

    A dependency rather than a module-level singleton so an integration test can
    override it with a scripted one and exercise the endpoints **without an API
    key and without paying a vendor per assertion** — the same reason the LLM
    provider is one.
    """
    return get_follow_up_suggester()


def get_assistant_metrics_recorder() -> AssistantMetricsRecorder:
    """Provide the process-wide assistant metrics recorder.

    Process-wide rather than per request for the same reason the search and RAG
    recorders are: a counter rebuilt on every request counts to one. A test
    overrides this so assertions about the metrics endpoint do not depend on
    traffic another test produced.
    """
    return get_assistant_metrics()


def get_assistant_service(
    conversations: Annotated[ConversationRepository, Depends(get_conversation_repository)],
    rag: Annotated[RagService, Depends(get_rag_service)],
    suggester: Annotated[FollowUpSuggester, Depends(get_follow_up_suggester_dependency)],
    metrics: Annotated[AssistantMetricsRecorder, Depends(get_assistant_metrics_recorder)],
) -> AssistantService:
    """Provide the AI assistant with its collaborators injected.

    **The RAG service is the assistant's only route to an answer, and that is the
    load-bearing line in this function.** ``13-ai-legal-assistant.md`` forbids
    duplicating retrieval, prompt construction, and orchestration, and this is
    where that would be undone: injecting a ``SearchService``, a
    ``VectorSearcher``, or a ``PromptLibrary`` here would give the assistant a way
    to build an answer without the pipeline that scopes, grounds, and cites it. It
    takes none of them, so the chain — conversation → pipeline → search → document
    → case — holds structurally rather than by discipline.

    The suggester is the one exception, and a narrow one: it holds a provider and
    a prompt library because proposing a *next question* is not something the
    pipeline does, but it is handed only an answer that has already been produced
    and cited, so it can reach no passage the caller could not already read.

    There is deliberately **no timeline recorder**: the timeline is a *case's*
    history, published to by the services that change a case, and a conversation
    belongs to a user rather than to a matter. Recording "asked the assistant a
    question" on a case's audit trail would also put one lawyer's private research
    in front of everyone else assigned to it.
    """
    return AssistantService(conversations, rag, suggester=suggester, metrics=metrics)


AssistantServiceDep = Annotated[AssistantService, Depends(get_assistant_service)]


def get_ocr_service(
    results: Annotated[OcrRepository, Depends(get_ocr_repository)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    storage: Annotated[DocumentStorageService, Depends(get_document_storage)],
    engine: Annotated[OcrEngine, Depends(get_ocr_engine_dependency)],
    queue: Annotated[OcrJobQueue, Depends(get_ocr_job_queue)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
    indexing: Annotated[IndexingService, Depends(get_indexing_service)],
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

    The indexing service is injected for the same shape of reason the OCR service
    is injected into the document service: a completed extraction is what
    schedules an index. OCR depends on the narrow
    :class:`~services.indexing.IndexScheduler` protocol rather than on this
    class, so it cannot reach the read, re-index, or monitoring side.
    """
    return OcrService(
        results, documents, storage, engine, queue, timeline=timeline, indexing=indexing
    )


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
