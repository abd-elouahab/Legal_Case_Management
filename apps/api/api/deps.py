"""Shared FastAPI dependencies.

Wires the request-scoped database session into the repository and service layers
and turns a Bearer token into the authenticated :class:`~models.user.User`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from core.config import settings
from core.exceptions import MissingTokenError
from core.security import TokenPayload
from db.session import SessionLocal, get_db
from models.user import User
from repositories.case import CaseRepository
from repositories.conversation import ConversationRepository
from repositories.document import DocumentRepository
from repositories.indexing import IndexingRepository
from repositories.notification import NotificationRepository
from repositories.ocr import OcrRepository
from repositories.report import ReportRepository
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
from services.events import EventPublisher, get_event_dispatcher
from services.indexing import IndexingService, IndexJob
from services.indexing_worker import index_queue
from services.job_queue import JobQueue
from services.llm import LLMProvider, get_llm_provider
from services.login_throttle import LoginThrottle
from services.notification import NotificationService
from services.notification_events import (
    NotificationEventSubscriber,
    get_notification_subscriber,
)
from services.notification_metrics import (
    NotificationMetricsRecorder,
    get_notification_metrics,
)
from services.ocr import OcrService
from services.ocr_engine import OcrEngine, get_ocr_engine
from services.ocr_queue import OcrJobQueue
from services.ocr_worker import ocr_queue
from services.prompts import PromptLibrary, get_prompt_library
from services.rag import RagService
from services.rag_metrics import RagMetricsRecorder, get_rag_metrics
from services.report import ReportJob, ReportService
from services.report_worker import report_queue
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
    events: Annotated[EventPublisher, Depends(get_event_publisher)],
) -> UserService:
    """Provide the user management service.

    The event publisher is injected because account changes are announced:
    activation, deactivation, a role change, and a password reset each publish a
    domain event on the affected account's own topic. The service takes the
    narrow protocol, so it can announce and cannot discover that Notifications is
    listening — which is what keeps ``16-notifications.md``'s *"business modules
    should publish domain events without knowing that notifications exist"* a
    property of the type rather than a convention.
    """
    return UserService(users, events=events)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


def get_session_factory() -> Callable[[], AbstractContextManager[Session]]:
    """Provide the application's database session factory.

    Distinct from :data:`DbSession`, and needed for exactly one caller: the
    **WebSocket endpoint**. A connection is not a request — it lives for hours —
    so it cannot hold a request-scoped session: that would pin a pooled database
    connection for the length of somebody's afternoon and serve increasingly
    stale rows from its identity map. What the socket needs is the ability to
    open a short session, ask one question, and close it.

    A dependency rather than an import of ``SessionLocal`` at the call site, for
    the reason every other collaborator here is one: an integration test
    overrides it and exercises the real endpoint, the real protocol, and the real
    authorization policy against its own database.
    """
    return SessionLocal


def get_event_publisher() -> EventPublisher:
    """Provide the process-wide event dispatcher, as a **publisher**.

    Process-wide rather than per request for the reason the job queues are: the
    WebSocket manager subscribed to the instance that existed at startup, and a
    dispatcher rebuilt per request would have no consumers — every event would be
    published into an empty room.

    Typed as :class:`~services.events.EventPublisher` rather than as
    :class:`~services.events.EventDispatcher`, and that is the load-bearing line
    in this function. Every business service below takes the narrow protocol, so
    none of them can register a subscriber, enumerate who is listening, or reach a
    connection — which is the isolation ``15-real-time-synchronization.md``
    requires (*"business modules should publish events but should never know who
    consumes them"*) made a property of the type rather than a convention.

    A test overrides this with a recorder, so "did archiving a case announce it?"
    is an assertion about a list rather than about a socket.
    """
    return get_event_dispatcher()


def get_timeline_repository(session: DbSession) -> TimelineRepository:
    """Provide a request-scoped timeline repository."""
    return TimelineRepository(session)


def get_timeline_service(
    events: Annotated[TimelineRepository, Depends(get_timeline_repository)],
    cases: Annotated[CaseRepository, Depends(get_case_repository)],
    publisher: Annotated[EventPublisher, Depends(get_event_publisher)],
) -> TimelineService:
    """Provide the timeline service with its collaborators injected.

    The case repository is injected because a case's timeline may only be served
    to a caller party to that case — a rule the timeline repository has no
    business knowing about, and one that must not be re-implemented against a
    second copy of the case query.

    The event publisher is injected because appending to a case's history is
    itself something a connected client needs to know about: it is what makes an
    activity feed refresh without polling. See
    :class:`~services.timeline.TimelineService` for why that is one event rather
    than one per entry type.
    """
    return TimelineService(events, cases, publisher=publisher)


TimelineServiceDep = Annotated[TimelineService, Depends(get_timeline_service)]


def get_case_service(
    cases: Annotated[CaseRepository, Depends(get_case_repository)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
    events: Annotated[EventPublisher, Depends(get_event_publisher)],
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
    return CaseService(cases, users, timeline=timeline, events=events)


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
    events: Annotated[EventPublisher, Depends(get_event_publisher)],
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
        events=events,
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


def get_report_repository(session: DbSession) -> ReportRepository:
    """Provide a request-scoped report repository."""
    return ReportRepository(session)


def get_report_job_queue() -> JobQueue[ReportJob]:
    """Provide the application's background report queue.

    The process-wide pool from :mod:`services.report_worker`, not a fresh one per
    request: bounding concurrency is the whole point of it, and a pool per
    request would bound nothing. **Its own pool rather than the indexing one**,
    because a report is a burst of calls to a metered language model while an
    index is CPU-bound embedding work — sharing would make each one's backlog the
    other's latency. A test overrides this with an inline queue so the work
    happens synchronously and the assertion does not race a thread.
    """
    return report_queue


def get_report_service(
    reports: Annotated[ReportRepository, Depends(get_report_repository)],
    cases: Annotated[CaseRepository, Depends(get_case_repository)],
    rag: Annotated[RagService, Depends(get_rag_service)],
    queue: Annotated[JobQueue[ReportJob], Depends(get_report_job_queue)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
    events: Annotated[EventPublisher, Depends(get_event_publisher)],
) -> ReportService:
    """Provide the report agent with its collaborators injected.

    **The RAG service is the agent's only route to content, and that is the
    load-bearing line in this function.** ``14-ai-report-agent.md`` forbids
    duplicating retrieval, prompt construction, and LLM interaction, and requires
    that the agent *"must never query Qdrant directly"* — and this is where that
    would be undone: injecting a ``SearchService``, a ``VectorSearcher``, or a
    ``PromptLibrary`` here would give the agent a way to build a section without
    the pipeline that scopes, grounds, and cites it. It takes none of them, so
    the chain — report → pipeline → search → document → case — holds structurally
    rather than by discipline.

    The case repository is injected because every report is *about* a case: the
    request has to check that it exists and that the caller is party to it, and
    that rule must not be re-implemented against a second copy of the case query.

    The timeline service is injected because the report service *publishes* to
    it: requested, generated, failed, and exported are the report half of a
    case's history. Unlike the assistant — which deliberately publishes nothing,
    because a conversation is one lawyer's private research — a report is case
    work product, and the people on the matter are entitled to know one exists.
    """
    return ReportService(reports, cases, rag, queue, timeline=timeline, events=events)


ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]


def get_notification_repository(session: DbSession) -> NotificationRepository:
    """Provide a request-scoped notification repository."""
    return NotificationRepository(session)


def get_notification_metrics_recorder() -> NotificationMetricsRecorder:
    """Provide the process-wide notification metrics recorder.

    Process-wide rather than per request for the same reason the search, RAG,
    assistant, and real-time recorders are: a counter rebuilt on every request
    counts to one. It matters more here than for any of those, because most
    notifications are created on a **background thread** with no request to hang
    a per-request instance off at all — so the recorder the metrics endpoint
    reads must be the same object the worker writes to.
    """
    return get_notification_metrics()


def get_notification_subscriber_dependency() -> NotificationEventSubscriber:
    """Provide the process-wide notification event subscriber.

    Reached through a dependency for exactly one endpoint —
    ``GET /notifications/metrics``, which reports the worker's backlog — and for
    exactly one other reason: a test overrides it, so an assertion about the
    metrics view does not depend on a thread another test started.

    It is **not** how notifications are created. That path runs from the event
    dispatcher, on a worker thread with no request; this is the read side of the
    same object.
    """
    return get_notification_subscriber()


def get_notification_service(
    notifications: Annotated[NotificationRepository, Depends(get_notification_repository)],
    events: Annotated[EventPublisher, Depends(get_event_publisher)],
    metrics: Annotated[NotificationMetricsRecorder, Depends(get_notification_metrics_recorder)],
) -> NotificationService:
    """Provide the notification service with its collaborators injected.

    **What this function does not inject is the load-bearing part.** There is no
    case service, no document service, no report service, and no user service
    here — ``16-notifications.md`` requires that the Notification Service *"remain
    independent from business modules"*, and this is where that would be undone.
    It takes a repository, a publisher, and a recorder, so there is no path from
    it into a business module at all.

    The reverse direction is stronger and is not visible in this file: **no
    business module takes this service**, because notifications are created by a
    subscriber on the event dispatcher rather than by anybody calling in. That is
    what makes the spec's *"business logic should never create notifications
    directly"* a fact about the dependency graph rather than a rule to remember.

    The event publisher is injected because delivery *is* a publication: the
    service persists a notification and announces it on the recipient's own
    topic, and the connection manager routes it like any other event. Typed as
    :class:`~services.events.EventPublisher`, so this service can announce and
    cannot reach a subscriber, a connection, or a client — which is the spec's
    *"the Notification Service must never communicate directly with clients"*
    made a property of the type.

    There is deliberately **no timeline recorder**: the timeline is a case's
    permanent history, and "Amina was told about this" is not a fact about the
    case. The business change that produced the notification recorded itself
    there already.
    """
    return NotificationService(notifications, events=events, metrics=metrics)


NotificationServiceDep = Annotated[NotificationService, Depends(get_notification_service)]
NotificationSubscriberDep = Annotated[
    NotificationEventSubscriber, Depends(get_notification_subscriber_dependency)
]


def get_ocr_service(
    results: Annotated[OcrRepository, Depends(get_ocr_repository)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    storage: Annotated[DocumentStorageService, Depends(get_document_storage)],
    engine: Annotated[OcrEngine, Depends(get_ocr_engine_dependency)],
    queue: Annotated[OcrJobQueue, Depends(get_ocr_job_queue)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
    indexing: Annotated[IndexingService, Depends(get_indexing_service)],
    events: Annotated[EventPublisher, Depends(get_event_publisher)],
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
        results,
        documents,
        storage,
        engine,
        queue,
        timeline=timeline,
        indexing=indexing,
        events=events,
    )


OcrServiceDep = Annotated[OcrService, Depends(get_ocr_service)]


def get_document_service(
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    cases: Annotated[CaseRepository, Depends(get_case_repository)],
    storage: Annotated[DocumentStorageService, Depends(get_document_storage)],
    timeline: Annotated[TimelineService, Depends(get_timeline_service)],
    ocr: Annotated[OcrService, Depends(get_ocr_service)],
    events: Annotated[EventPublisher, Depends(get_event_publisher)],
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
    return DocumentService(documents, cases, storage, timeline=timeline, ocr=ocr, events=events)


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
