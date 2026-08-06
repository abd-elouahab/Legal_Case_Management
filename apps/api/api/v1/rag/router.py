"""RAG pipeline endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.rag.RagService`, and shape the HTTP response. No business
logic lives here.

**Two endpoints, and neither of them is a chat interface.**
``12-rag-pipeline.md`` is explicit that this feature *"is not the chat
interface"* and that no chat UI, conversation history, or persistent memory is
in scope. What is here is a **single stateless question** and an operational
view of the pipeline that answers it — nothing is created, nothing is
remembered, and asking the same question twice produces two independent runs.
Conversations, streaming, follow-up suggestions, and feedback belong to the AI
Legal Assistant, which is a separate feature against this same service.

The endpoints exist at all because a pipeline nothing can call is a pipeline
nothing can verify: the spec requires authorization to be enforced, failures to
be surfaced safely, and metrics to be *exposed*, and `ai-workflow-rules.md`
requires every completed unit to be independently testable end to end.

**Asking is a POST, and that is a privacy decision rather than a stylistic one.**
A query string is written to the reverse proxy's access log, to the browser's
history, and to the ``Referer`` header of anything the page loads next. A
question put to a legal assistant names what a lawyer is looking for and in whose
matter, and a ``GET`` would put every one of them into three logs the application
does not control.

Authorization is layered, and the two layers answer different questions:

* the ``ai:*`` **capability** is required by the reusable dependencies in
  :mod:`api.authorization`, declared next to the route so it appears in the
  OpenAPI schema. Because ``CurrentUser`` resolves first, an anonymous caller
  gets **401** and only an authenticated-but-unentitled one gets **403**;
* the **resource** — which cases this caller is party to — is applied by
  :class:`~services.search.SearchService` inside the vector query, because the
  pipeline retrieves through nothing else.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from api.authorization import require_permission
from api.deps import RagServiceDep
from core.permissions import Permission
from models.user import User
from schemas.errors import ErrorResponse
from schemas.rag import RagAnswerResponse, RagMetricsRead, RagRequest

#: Mounted under ``/rag``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

Asker = Annotated[User, Depends(require_permission(Permission.AI_ASK))]
AiMonitor = Annotated[User, Depends(require_permission(Permission.AI_MONITOR))]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": (
            "The account is disabled, lacks the required permission, or filtered by a case or "
            "document it is not assigned to."
        ),
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The case or document named by a filter does not exist.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "Request validation failed, or a metadata filter matches too many documents to "
            "retrieve from at once."
        ),
    }
}
_UNAVAILABLE: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "The pipeline is disabled on this deployment, or retrieval, the prompt library, or "
            "the language model could not answer. The `error` field names which."
        ),
    }
}


@router.post(
    "/answer",
    response_model=RagAnswerResponse,
    status_code=status.HTTP_200_OK,
    summary="Answer a question from indexed legal documents",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_VALIDATION, **_UNAVAILABLE},
)
def answer_question(actor: Asker, rag: RagServiceDep, request: RagRequest) -> RagAnswerResponse:
    """Answer a question using only the case documents this caller may read.

    The question is retrieved against with the **same model the documents were
    indexed with**, the most relevant passages are assembled into a versioned
    prompt, the configured language model is invoked once, and the answer comes
    back with a citation per source — document, version, page, and case.

    **The answer is grounded or it is not given.** The model is instructed to use
    only the retrieved passages, and when they do not support an answer the
    pipeline says so rather than improvising: `insufficient_evidence` is `true`,
    `grounded` is `false`, and the text is the platform's own message. When
    retrieval returns nothing at all, no model is called.

    **Authorization is inherited, not re-implemented.** Retrieval runs through
    the semantic search service, so a passage can only reach the model if the
    caller could already open the document it came from. Filters narrow that set
    and can never widen it, and filtering by a case or document the caller is not
    party to answers **403** rather than an unsourced answer.

    **This is one question, not a conversation.** Nothing is stored, nothing is
    remembered, and there is no history — those belong to the AI assistant.

    Arabic, French, and English share one retrieval space, so a question in one
    language finds passages written in the others; `language` chooses which one
    the answer is written in, and is detected from the question when omitted.
    """
    return rag.answer(request, actor=actor).to_response()


@router.get(
    "/metrics",
    response_model=RagMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="RAG pipeline metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_rag_metrics(actor: AiMonitor, rag: RagServiceDep) -> RagMetricsRead:
    """Return platform-wide pipeline health.

    The five figures the spec's Monitoring section names — **response latency,
    retrieval latency, token usage, successful requests, failed requests** — plus
    the rates, a breakdown of failures by cause, and the pipeline configuration.

    It also reports the **grounding rate**: the share of successful runs that
    answered from retrieved evidence rather than declining. That is the number to
    watch, and it is not an AI metric — a falling rate means the corpus no longer
    covers what people ask, which is a document problem.

    And it reports whether a **provider client can be built** and whether the
    **prompt template can be loaded**. Those are the three things the counters
    cannot tell apart: a platform answering nothing because no credential is
    configured, one answering nothing because its prompts are missing, and one
    nobody has asked yet all show the same zeros.

    `since` is the honest caveat, not decoration: these counters live in the API
    process rather than in a table, so they reset when it restarts and each
    instance counts only its own traffic.

    An operational view of the pipeline, so it is gated on `ai:monitor` and
    reports **counts, timings, and configuration only** — never a question, an
    answer, a document, a case, or a passage of text.
    """
    health = rag.health()
    metrics = health.metrics

    return RagMetricsRead(
        since=metrics.since,
        total_requests=metrics.total_requests,
        successful_requests=metrics.successful_requests,
        failed_requests=metrics.failed_requests,
        success_rate=metrics.success_rate,
        failure_rate=metrics.failure_rate,
        grounded_answers=metrics.grounded_answers,
        insufficient_evidence=metrics.insufficient_evidence,
        grounding_rate=metrics.grounding_rate,
        average_latency_ms=metrics.average_latency_ms,
        average_retrieval_ms=metrics.average_retrieval_ms,
        average_generation_ms=metrics.average_generation_ms,
        total_passages=metrics.total_passages,
        average_passages=metrics.average_passages,
        total_citations=metrics.total_citations,
        average_citations=metrics.average_citations,
        total_prompt_tokens=metrics.total_prompt_tokens,
        total_completion_tokens=metrics.total_completion_tokens,
        metered_requests=metrics.metered_requests,
        average_total_tokens=metrics.average_total_tokens,
        failures_by_code=metrics.failures_by_code,
        provider=health.provider,
        model=health.model,
        llm_available=health.llm_available,
        prompt_name=health.prompt_name,
        prompt_version=health.prompt_version,
        prompt_available=health.prompt_available,
        retrieval_top_k=health.retrieval_top_k,
        max_context_characters=health.max_context_characters,
        max_citations=health.max_citations,
        enabled=health.enabled,
    )
