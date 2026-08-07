"""Retrieval-Augmented Generation business logic.

Owns the workflow ``12-rag-pipeline.md`` defines, in exactly the order its flow
diagram gives:

.. code-block:: text

    User Question → Validate Request → Semantic Search → Relevant Chunks
                  → Build Prompt → Invoke LLM → Grounded Response
                  → Attach Citations → Return Result

The *order* is declared once, in :mod:`services.rag_graph`, as a LangGraph state
graph; this module implements each of its nodes. That split is deliberate: the
graph reads as a statement of what happens when, and this file reads as a
statement of what each step does, and neither has to be read to understand the
other.

Scope boundaries, kept deliberately sharp:

* **Retrieval is not done here, and cannot be.**
  :class:`~services.search.SearchService` owns it. The spec is explicit — *"The
  RAG pipeline must never query the vector database directly if an existing
  retrieval abstraction exists"* — and the boundary is **structural** rather than
  a matter of discipline: this service holds no vector searcher, no embedder, no
  repository, and no database session, so there is no path from here to a vector
  that does not pass through the service that scopes it to the caller's cases.
  That single fact is also the whole of this feature's authorization story.
* **Prompt text is not written here.** :mod:`services.prompts` renders versioned
  templates from ``apps/api/prompts``; the spec's *"do not hardcode prompts
  throughout the codebase"* holds because there is no prompt string in this file.
* **The model is not called directly.** :mod:`services.llm` owns the provider
  abstraction, so replacing Gemini with LiteLLM, Ollama, or anything else changes
  one setting and touches nothing here — the spec's *"provider replacement
  possible without changing the orchestration logic"*.
* **Capability authorization is not decided here.** Whether the caller may ask at
  all is settled by the dependencies in :mod:`api.authorization` before a request
  reaches this service.
* **No conversation is managed here.** ``ai-architecture.md``: *"The RAG Pipeline
  must never manage conversations."* This service has no conversation, no
  message, no session, and no history — the AI Assistant owns all four and calls
  this.

What it does own are the rules nothing else can express: that an answer is built
only from passages that were retrieved, that a question with no supporting
evidence is answered by saying so rather than by a model improvising, that every
grounded answer carries its sources, that a context budget is respected before a
provider is paid to discover it was not, and that no question, passage, or answer
reaches a log.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NoReturn

import structlog

from core.config import settings
from core.exceptions import (
    AppException,
    InvalidQuestionError,
    RagDisabledError,
    RagUnavailableError,
    SearchDisabledError,
    SearchUnavailableError,
)
from core.rag import (
    INSUFFICIENT_EVIDENCE_MARKER,
    RagFailureCode,
    cited_markers,
    clip_passage,
    failure_message,
    fit_to_budget,
    is_answerable_question,
    is_insufficient_evidence,
    is_usable_answer,
    language_name,
    loggable_question,
    no_evidence_message,
    question_fingerprint,
    resolve_answer_language,
    sentinel_prefix_pending,
    unknown_markers,
)
from models.user import User
from schemas.rag import RagAnswerResponse, RagCitationRead, RagRequest
from schemas.search import SearchRequest, SearchResultRead
from services.llm import LLMCompletion, LLMError, LLMProvider, get_llm_provider
from services.prompts import (
    PromptError,
    PromptLibrary,
    PromptNotFoundError,
    RenderedPrompt,
    get_prompt_library,
)
from services.rag_graph import RagState, build_rag_graph
from services.rag_metrics import NullRagMetrics, RagMetricsRecorder, RagMetricsSnapshot
from services.search import SearchService

logger = structlog.get_logger(__name__)

#: Collapses the whitespace left behind when an invented citation marker is
#: removed from an answer. Compiled once; see :meth:`RagService.format_output`.
_SPACE_RUN = re.compile(r"[ \t]{2,}")

#: Tidies the space a removed marker leaves in front of punctuation.
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")


@dataclass(frozen=True, slots=True)
class RagOutcome:
    """One answered question: the answer, its sources, and how it was produced.

    Returned instead of the schema so the router owns serialization and the
    service owns the workflow — the same split every other service here makes.
    """

    question: str
    answer: str
    language: str
    grounded: bool
    insufficient_evidence: bool
    truncated: bool
    citations: list[RagCitationRead]

    retrieved_count: int
    context_count: int
    context_characters: int
    context_truncated: bool

    duration_ms: int
    retrieval_ms: int
    generation_ms: int | None

    prompt_name: str
    prompt_version: int
    provider: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def to_response(self) -> RagAnswerResponse:
        """Project onto the wire shape.

        Here rather than in the router because the mapping is field-for-field:
        a router that restated twenty assignments would be twenty chances for one
        of them to drift.
        """
        return RagAnswerResponse(
            question=self.question,
            answer=self.answer,
            language=self.language,
            grounded=self.grounded,
            insufficient_evidence=self.insufficient_evidence,
            truncated=self.truncated,
            citations=self.citations,
            retrieved_count=self.retrieved_count,
            context_count=self.context_count,
            context_characters=self.context_characters,
            context_truncated=self.context_truncated,
            duration_ms=self.duration_ms,
            retrieval_ms=self.retrieval_ms,
            generation_ms=self.generation_ms,
            prompt_name=self.prompt_name,
            prompt_version=self.prompt_version,
            provider=self.provider,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
        )


class RagStreamEventKind(StrEnum):
    """What one event of a streamed run reports."""

    #: Retrieval has finished. Carries how many passages were found, which is
    #: what lets a client show "searching 6 documents" before the first word of
    #: the answer arrives — the part of a slow request that is otherwise silent.
    RETRIEVAL = "retrieval"
    #: A fragment of the answer.
    DELTA = "delta"
    #: The run is complete. Carries the whole :class:`RagOutcome`, which is the
    #: authoritative answer and the only place citations appear.
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class RagStreamEvent:
    """One thing that happened during a streamed run.

    A single event type with a ``kind`` rather than three classes, because the
    consumer is a transport that serializes whatever it is handed — and a
    hierarchy would make that consumer a dispatch table over types.
    """

    kind: RagStreamEventKind
    #: Set on :attr:`RagStreamEventKind.DELTA`.
    text: str = ""
    #: Set on :attr:`RagStreamEventKind.RETRIEVAL`.
    retrieved_count: int = 0
    #: Set on :attr:`RagStreamEventKind.FINAL`.
    outcome: RagOutcome | None = None


@dataclass(frozen=True, slots=True)
class RagHealth:
    """Platform-wide pipeline health, as the monitoring endpoint reports it."""

    metrics: RagMetricsSnapshot
    provider: str
    model: str
    llm_available: bool
    prompt_name: str
    prompt_version: int
    prompt_available: bool
    retrieval_top_k: int
    max_context_characters: int
    max_citations: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class _ContextFit:
    """The result of fitting retrieved passages to the context budget."""

    passages: list[SearchResultRead] = field(default_factory=list)
    characters: int = 0
    truncated: bool = False


class RagService:
    """Answers a question from indexed legal documents, with citations.

    Implements :class:`~services.rag_graph.RagNodes`; the graph it compiles in
    :meth:`__init__` is what calls them, in the order the spec's flow diagram
    gives.
    """

    def __init__(
        self,
        search: SearchService,
        prompts: PromptLibrary | None = None,
        provider: LLMProvider | None = None,
        *,
        metrics: RagMetricsRecorder | None = None,
    ) -> None:
        self._search = search
        self._prompts = prompts or get_prompt_library()
        self._provider = provider or get_llm_provider()
        # See `TimelineService`'s null recorder for why the default records
        # nothing; the application wires the process-wide one in
        # `api.deps.get_rag_service`.
        self._metrics: RagMetricsRecorder = metrics or NullRagMetrics()
        # Compiled once per instance rather than per question: compilation
        # validates every edge and builds the executor, and neither depends on
        # what is being asked.
        self._graph = build_rag_graph(self)

    # ------------------------------------------------------------- answering #

    def answer(self, request: RagRequest, *, actor: User) -> RagOutcome:
        """Answer one question from the documents this caller may read.

        The whole pipeline, run as a graph. This method owns only what the graph
        cannot: the enabled check, the clock, the single place a failure is
        recorded, and the projection of the final state onto an outcome.

        **Metrics are recorded here and nowhere else**, which is what guarantees
        exactly one record per request. A node that recorded its own failure and
        then raised through a node that recorded another would double-count the
        outages the metric exists to surface.

        Raises:
            RagDisabledError: answer generation is switched off for this
                deployment.
            InvalidQuestionError: the question carries nothing to answer.
            SearchAccessDeniedError: the caller filtered by a case or document
                they are not party to. Raised by the search service, and passed
                through unchanged — a second authorization rule here would be a
                second one to keep in step with the first.
            CaseNotFoundError: the case filter names no case.
            DocumentNotFoundError: the document filter names no live document.
            SearchFilterTooBroadError: a metadata filter resolved to more
                documents than can be retrieved from at once.
            RagUnavailableError: retrieval, the prompt library, or the language
                model could not answer.
        """
        if not settings.RAG_ENABLED:
            logger.info(
                "rag_rejected",
                reason="disabled",
                actor_id=str(actor.id),
                question_fingerprint=question_fingerprint(request.question),
            )
            raise RagDisabledError

        started = time.monotonic()
        logger.info("rag_requested", **self._event(request, actor=actor))

        try:
            final: RagState = self._graph.invoke(
                RagState(
                    request=request,
                    actor=actor,
                    started=started,
                    deadline=started + settings.RAG_TIMEOUT_SECONDS,
                )
            )
        except RagUnavailableError as exc:
            self._record_failure(request, actor=actor, started=started, error_code=exc.error_code)
            raise
        except AppException:
            # Already a precise status: an unanswerable question (422), a filter
            # naming a case the caller is not party to (403, raised by the search
            # service and passed through *unchanged* — a second authorization
            # rule here would be a second one to keep in step with the first), a
            # filter naming nothing (404), a filter too broad to push down (422).
            #
            # Deliberately **not** counted as a failed request: the spec's
            # failure rate is a health signal for the pipeline, and a rejected
            # request says nothing about whether the pipeline is healthy. The
            # same reasoning `SearchService` applies to its own rejections.
            raise
        except Exception as exc:
            # An unexpected fault. Logged with its traceback and answered as a
            # generic unavailability, because an unhandled exception escaping
            # into the global handler would be a 500 that tells the caller the
            # platform is broken when one dependency misbehaved.
            logger.exception(
                "rag_failed",
                error_code=RagFailureCode.UNKNOWN.value,
                error_type=type(exc).__name__,
                **self._event(request, actor=actor),
            )
            self._record_failure(
                request,
                actor=actor,
                started=started,
                error_code=RagFailureCode.UNKNOWN.value,
                already_logged=True,
            )
            raise RagUnavailableError(
                RagFailureCode.UNKNOWN.value, failure_message(RagFailureCode.UNKNOWN)
            ) from exc

        return self._complete(final, request=request, actor=actor, started=started)

    def stream(self, request: RagRequest, *, actor: User) -> Iterator[RagStreamEvent]:
        """Answer one question, emitting the answer as it is produced.

        The same pipeline as :meth:`answer`, in the same order, calling the same
        nodes — with exactly one difference: generation uses the provider's
        incremental interface instead of its blocking one.

        **Why this lives here rather than in the AI Assistant.**
        ``13-ai-legal-assistant.md`` requires streaming *and* forbids the
        assistant from duplicating *"retrieval, prompt construction, or
        orchestration logic already implemented by the RAG Pipeline"*. An
        assistant that streamed on its own would have to retrieve, build a
        prompt, call a provider, verify the reply, and attach citations — which
        is this whole module, written twice. So streaming is an entry point on
        the pipeline, and the assistant relays what comes out of it.

        **Why it is not a graph.** LangGraph's ``invoke`` returns a *final
        state*; emitting fragments out of the middle of a node needs a generator.
        The traversal below is the graph's edges written out by hand, and the
        honest description is that the two must be kept in step — which is why a
        test asserts they visit the same nodes, in the same order, with the same
        branch after retrieval.

        **The refusal sentinel never reaches the client as text.** Fragments are
        withheld while the accumulated answer could still turn out to be
        :data:`~core.rag.INSUFFICIENT_EVIDENCE_MARKER` alone (see
        :func:`~core.rag.sentinel_prefix_pending`), so a reader is never shown an
        internal token that is about to be replaced by a sentence.

        **The final event is authoritative.** Citations, the grounding flag, and
        the cleaned answer are all on it, and a client must render *that* rather
        than what it accumulated — because an answer that cites a source it was
        never given has its dangling marker removed, and because a model that
        declined has its sentinel replaced. The deltas are a progress indicator
        that happens to be readable, not the answer.

        Raises:
            The same exceptions as :meth:`answer`, at the point in the iteration
            where the corresponding stage runs. A failure after the first
            fragment has been emitted is still raised: the consumer has already
            shown text, so the transport is what decides how to tell the user.
        """
        if not settings.RAG_ENABLED:
            logger.info(
                "rag_rejected",
                reason="disabled",
                actor_id=str(actor.id),
                question_fingerprint=question_fingerprint(request.question),
            )
            raise RagDisabledError

        started = time.monotonic()
        logger.info("rag_requested", streamed=True, **self._event(request, actor=actor))

        state = RagState(
            request=request,
            actor=actor,
            started=started,
            deadline=started + settings.RAG_TIMEOUT_SECONDS,
        )

        try:
            state.update(self.validate_request(state))
            state.update(self.retrieve_context(state))

            yield RagStreamEvent(
                kind=RagStreamEventKind.RETRIEVAL,
                retrieved_count=len(state.get("passages", [])),
            )

            if state.get("passages"):
                state.update(self.assemble_prompt(state))
                # The one branch that differs from `answer`, and it is delegated
                # rather than inlined so that "what a streamed generation is" has
                # a single definition.
                yield from self._generate_streaming(state)
                state.update(self.verify_response(state))
            else:
                # The same branch `route_after_retrieval` takes, and for the same
                # reason: with nothing retrieved there is nothing to ground an
                # answer in, and the model is not called at all.
                state.update(self.report_no_evidence(state))

            state.update(self.format_output(state))
        except RagUnavailableError as exc:
            self._record_failure(request, actor=actor, started=started, error_code=exc.error_code)
            raise
        except AppException:
            # Already a precise status — an unanswerable question, an
            # inaccessible filter, a filter naming nothing. Deliberately not
            # counted as a pipeline failure, exactly as in `answer`.
            raise
        except Exception as exc:
            logger.exception(
                "rag_failed",
                streamed=True,
                error_code=RagFailureCode.UNKNOWN.value,
                error_type=type(exc).__name__,
                **self._event(request, actor=actor),
            )
            self._record_failure(
                request,
                actor=actor,
                started=started,
                error_code=RagFailureCode.UNKNOWN.value,
                already_logged=True,
            )
            raise RagUnavailableError(
                RagFailureCode.UNKNOWN.value, failure_message(RagFailureCode.UNKNOWN)
            ) from exc

        yield RagStreamEvent(
            kind=RagStreamEventKind.FINAL,
            outcome=self._complete(state, request=request, actor=actor, started=started),
        )

    def _generate_streaming(self, state: RagState) -> Iterator[RagStreamEvent]:
        """Call the provider incrementally, and fall back when it cannot stream.

        ``13-ai-legal-assistant.md``: *"The implementation should gracefully fall
        back to non-streaming responses if streaming is unavailable."* The
        fallback is taken when the provider fails **before producing a single
        fragment** — which covers a backend that does not implement streaming, a
        gateway that refuses it, and a connection that never opened.

        A failure **after** the first fragment is not retried and not fallen back
        on, deliberately: text has already been delivered to the reader, and
        restarting would either duplicate it or replace it with a differently
        worded answer mid-paragraph. The same reasoning
        :meth:`~services.llm.GeminiProvider.stream` gives for not retrying.

        A streamed completion carries **no token usage**, because the provider
        reports usage on a finished response and there is not one. That is stated
        rather than papered over: the monitoring view counts metered runs
        separately, so a deployment that streams everything reports honest
        ``None`` totals instead of a figure that silently omits its real traffic.
        """
        self._check_deadline(state, stage="generate")

        prompt = state["prompt"]
        remaining = max(1.0, state["deadline"] - time.monotonic())
        timeout = min(float(settings.LLM_TIMEOUT_SECONDS), remaining)
        generation_started = time.monotonic()

        accumulated = ""
        released = 0
        streamed = False

        try:
            for fragment in self._provider.stream(
                system=prompt.system, prompt=prompt.user, timeout_seconds=timeout
            ):
                streamed = True
                accumulated += fragment

                # Withheld while the reply could still be the refusal sentinel
                # alone; released in one piece the moment it cannot be.
                if sentinel_prefix_pending(accumulated):
                    continue

                pending = accumulated[released:]
                released = len(accumulated)
                if pending:
                    yield RagStreamEvent(kind=RagStreamEventKind.DELTA, text=pending)
        except LLMError as exc:
            if streamed:
                # Mid-answer failure: the provider has already translated and
                # logged the SDK's failure without quoting it, so all that is
                # left is to carry the cause up.
                self._unavailable(exc.code)

            logger.info(
                "rag_stream_unavailable",
                provider=self._provider.name,
                model=self._provider.model,
                error_code=exc.code.value,
            )
            state.update(self.invoke_model(state))
            completion = state["completion"]
            text = completion.text
            if not sentinel_prefix_pending(text) and text:
                yield RagStreamEvent(kind=RagStreamEventKind.DELTA, text=text)
            return

        generation_ms = self._elapsed_ms(generation_started)

        logger.info(
            "rag_llm_streamed",
            provider=self._provider.name,
            model=self._provider.model,
            generation_ms=generation_ms,
            answer_characters=len(accumulated),
        )

        state.update(
            RagState(
                completion=LLMCompletion(
                    text=accumulated,
                    provider=self._provider.name,
                    model=self._provider.model,
                ),
                generation_ms=generation_ms,
            )
        )

    # ----------------------------------------------------------- graph nodes #

    def validate_request(self, state: RagState) -> RagState:
        """Validate the question, and settle the language and breadth of the answer.

        The spec's first step. It does three things a later step would otherwise
        have to guess at:

        * refuses a question that carries nothing to answer — checked here as
          well as in the schema, because this service is a *reusable backend*
          that the assistant and the report agent will call with requests they
          built themselves, and a validation that lives only in a route is a
          validation those callers do not have;
        * settles the **answer language** once, so retrieval, the prompt, and the
          no-evidence message can never disagree about it;
        * settles how many passages to retrieve, clamped to what the search
          service will actually return — a configured 80 against a ceiling of 50
          must not silently become an answer built on fewer sources than the
          operator believes.
        """
        request = state["request"]
        question = request.question

        if not is_answerable_question(question):
            logger.info(
                "rag_rejected",
                reason="invalid_question",
                actor_id=str(state["actor"].id),
                question_fingerprint=question_fingerprint(question),
            )
            raise InvalidQuestionError

        language = resolve_answer_language(question, request.language)
        top_k = min(
            request.top_k or settings.RAG_RETRIEVAL_TOP_K,
            settings.SEARCH_MAX_LIMIT,
        )

        return RagState(question=question, language=language, top_k=top_k)

    def retrieve_context(self, state: RagState) -> RagState:
        """Retrieve the passages an answer may be built from.

        **Through :class:`~services.search.SearchService`, which is the only way
        this service can reach a vector at all.** Everything the spec's
        "Authorization" section requires follows from that one call: the caller's
        case scope is applied inside the vector query, a filter naming a case they
        are not party to is refused rather than emptied, and a caller assigned to
        nothing retrieves nothing. None of it is re-implemented here, and none of
        it can be bypassed from here.

        A retrieved passage whose document could not be resolved is dropped, so
        the sources placed in the prompt and the citations attached to the answer
        stay in one-to-one correspondence. A source the model can read but the
        reader cannot check is worse than one fewer source.

        Raises:
            RagUnavailableError: retrieval could not run.
        """
        self._check_deadline(state, stage="retrieve")

        request = state["request"]
        retrieval_started = time.monotonic()

        try:
            outcome = self._search.search(
                SearchRequest(
                    query=state["question"],
                    limit=state["top_k"],
                    offset=0,
                    min_score=(
                        request.min_score
                        if request.min_score is not None
                        else settings.RAG_MIN_SCORE
                    ),
                    filters=request.filters,
                ),
                actor=state["actor"],
            )
        except (SearchUnavailableError, SearchDisabledError) as exc:
            # Translated at this boundary rather than propagated, because a
            # caller of *this* endpoint should not be told that "search is
            # disabled" — they asked a question, and what failed was the
            # pipeline's retrieval step. The specific cause survives in the log.
            logger.error(
                "rag_retrieval_failed",
                cause=exc.error_code,
                actor_id=str(state["actor"].id),
                question_fingerprint=question_fingerprint(state["question"]),
            )
            self._unavailable(RagFailureCode.RETRIEVAL_UNAVAILABLE)

        passages = [passage for passage in outcome.results if passage.document is not None]
        dropped = len(outcome.results) - len(passages)
        retrieval_ms = self._elapsed_ms(retrieval_started)

        logger.info(
            "rag_retrieval_completed",
            retrieved_count=len(passages),
            unresolved_count=dropped,
            top_score=outcome.top_score,
            retrieval_ms=retrieval_ms,
            actor_id=str(state["actor"].id),
            question_fingerprint=question_fingerprint(state["question"]),
        )

        return RagState(passages=passages, retrieval_ms=retrieval_ms)

    def assemble_prompt(self, state: RagState) -> RagState:
        """Fit the retrieved passages to the context budget and render the prompt.

        The spec's "Context Assembly" and "Prompt Construction", and the two
        requirements that shape this node are *"respect model context limits"*
        and *"clearly separate retrieved context from user input"*.

        The budget is enforced **here, before the provider is called**, rather
        than by catching the provider's complaint: an over-long prompt is
        rejected after the request has been sent, billed, and waited for, and on
        some providers it is silently truncated instead — which would drop the
        least-relevant passages, or the most, with nothing to say which.

        The separation of context from user input is the template's
        (``CONTEXT``/``QUESTION`` fences plus a system rule that everything inside
        them is data), not this node's. It is a prompt-level control because
        there is no escaping scheme that makes a sentence stop being a sentence —
        see :mod:`services.prompts`.

        Raises:
            RagUnavailableError: the prompt could not be built, or no passage
                fits the configured budget.
        """
        self._check_deadline(state, stage="assemble")

        fit = self._fit_context(state["passages"])

        if not fit.passages:
            # Only reachable when the deployment's budget is smaller than one
            # readable passage — a configuration fault, not a fault of the
            # question, which is why it names the budget rather than the text.
            logger.error(
                "rag_context_overflow",
                budget=settings.RAG_MAX_CONTEXT_CHARACTERS,
                retrieved_count=len(state["passages"]),
            )
            self._unavailable(RagFailureCode.CONTEXT_OVERFLOW)

        prompt = self._render(state, fit.passages)

        if fit.truncated:
            logger.info(
                "rag_context_truncated",
                retrieved_count=len(state["passages"]),
                context_count=len(fit.passages),
                context_characters=fit.characters,
                budget=settings.RAG_MAX_CONTEXT_CHARACTERS,
            )

        logger.info(
            "rag_prompt_built",
            prompt_name=prompt.name,
            prompt_version=prompt.version,
            prompt_characters=prompt.character_count,
            context_count=len(fit.passages),
            context_characters=fit.characters,
        )

        return RagState(
            prompt=prompt,
            context=fit.passages,
            context_characters=fit.characters,
            context_truncated=fit.truncated,
        )

    def invoke_model(self, state: RagState) -> RagState:
        """Call the configured provider, once.

        Once is the spec's *"avoid duplicate LLM calls"*: there is exactly one
        ``generate`` in this pipeline, retries live inside the provider where
        they are bounded and backed off, and the no-evidence branch never reaches
        this node at all.

        The provider's deadline is the **smaller** of its own configured timeout
        and whatever is left of the run's — otherwise a run could pass its
        deadline check, enter a 45-second generation, and return a minute after
        the budget the operator set.

        Raises:
            RagUnavailableError: the model was unavailable, timed out, or failed.
        """
        self._check_deadline(state, stage="generate")

        prompt = state["prompt"]
        remaining = max(1.0, state["deadline"] - time.monotonic())
        generation_started = time.monotonic()

        try:
            completion = self._provider.generate(
                system=prompt.system,
                prompt=prompt.user,
                timeout_seconds=min(float(settings.LLM_TIMEOUT_SECONDS), remaining),
            )
        except LLMError as exc:
            # The provider has already translated and logged the SDK's failure
            # without quoting it; all that is left is to carry the cause up.
            self._unavailable(exc.code)

        generation_ms = self._elapsed_ms(generation_started)

        logger.info(
            "rag_llm_invoked",
            provider=completion.provider,
            model=completion.model,
            generation_ms=generation_ms,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            finish_reason=completion.finish_reason,
            truncated=completion.truncated,
            answer_characters=len(completion.text),
        )

        return RagState(completion=completion, generation_ms=generation_ms)

    def verify_response(self, state: RagState) -> RagState:
        """Decide whether what came back is an answer, and whether it is grounded.

        The spec's "Validate response" step, and it makes exactly two decisions:

        * **an unusable reply is a failure.** Empty, or whitespace — there is
          nothing to show a user, and showing a blank answer beside a list of
          citations would read as "the documents say nothing", which is a
          statement the platform has no basis for;
        * **the insufficiency sentinel is not an answer.** The prompt instructs
          the model to reply with :data:`~core.rag.INSUFFICIENT_EVIDENCE_MARKER`
          alone when the passages do not support one. Recognising it here is what
          turns the spec's *"acknowledge insufficient evidence"* into a typed
          outcome the caller can branch on, rather than a sentence a client would
          have to pattern-match in three languages.

        Everything else is left alone. Judging an answer's *quality* would be the
        platform second-guessing the model, and grading legal reasoning is not
        something this node can do honestly.

        Raises:
            RagUnavailableError: the reply was unusable.
        """
        completion = state["completion"]
        text = completion.text.strip()

        if not is_usable_answer(text):
            logger.error(
                "rag_response_invalid",
                reason="empty",
                finish_reason=completion.finish_reason,
                provider=completion.provider,
                model=completion.model,
            )
            self._unavailable(RagFailureCode.MALFORMED_RESPONSE)

        if is_insufficient_evidence(text):
            logger.info(
                "rag_insufficient_evidence",
                reason="model_declined",
                context_count=len(state.get("context", [])),
            )
            return RagState(
                answer=no_evidence_message(state["language"]),
                grounded=False,
                insufficient=True,
                cited=set(),
            )

        sources = len(state.get("context", []))
        invented = unknown_markers(text, source_count=sources)
        if invented:
            # Counted and logged rather than fatal: the prose may be perfectly
            # grounded and only the numbering wrong. A rising count is the
            # earliest signal that a prompt or model change has degraded
            # grounding, which is why it is logged even though the answer stands.
            logger.warning(
                "rag_citation_unknown",
                invented_count=len(invented),
                source_count=sources,
                provider=completion.provider,
                model=completion.model,
            )

        return RagState(
            answer=text,
            grounded=True,
            insufficient=False,
            cited={marker for marker in cited_markers(text) if 1 <= marker <= sources},
        )

    def report_no_evidence(self, state: RagState) -> RagState:
        """Answer a question retrieval found nothing for, without calling a model.

        The spec's "No Evidence Handling", and every clause of it follows from
        not making the call:

        * *"do not fabricate answers"* — there is no model output to fabricate
          from;
        * *"explain that supporting information could not be found"* — the
          platform's own sentence, in the answer language, written once in
          :data:`~core.rag.NO_EVIDENCE_MESSAGES`;
        * *"avoid confident speculation"* — a fixed sentence cannot speculate.

        Asking a model to explain that it found nothing is the tempting
        alternative and the wrong one: a model handed an empty context and a
        legal question will sometimes explain the emptiness *and then answer
        anyway* from its training data, which is indistinguishable from a
        grounded answer to everyone downstream.
        """
        logger.info(
            "rag_insufficient_evidence",
            reason="no_passages",
            actor_id=str(state["actor"].id),
            question_fingerprint=question_fingerprint(state["question"]),
        )
        return RagState(
            answer=no_evidence_message(state["language"]),
            grounded=False,
            insufficient=True,
            cited=set(),
            context=[],
            context_characters=0,
            context_truncated=False,
        )

    def format_output(self, state: RagState) -> RagState:
        """Shape the answer text. The one node both branches end at.

        Two things happen, and only two:

        * **invented citation markers are removed from the prose.** A ``[9]``
          when six sources were supplied resolves to nothing, and leaving a
          dangling reference in a legal answer invites a reader to look for a
          source that does not exist. The surrounding text is left untouched —
          this removes a broken reference, it does not rewrite an answer;
        * the result is stripped, and an answer that turns out to be empty after
          the removal falls back to the no-evidence message rather than to
          nothing at all.
        """
        answer = state.get("answer", "").strip()

        if state.get("grounded"):
            answer = self._strip_invented_markers(answer, sources=len(state.get("context", [])))

        if not answer:
            return RagState(
                answer=no_evidence_message(state["language"]),
                grounded=False,
                insufficient=True,
                cited=set(),
            )

        return RagState(answer=answer)

    # --------------------------------------------------------------- health #

    def health(self) -> RagHealth:
        """Aggregate pipeline health for the monitoring endpoint.

        No per-resource scope is applied, deliberately: this is an operational
        view of the *pipeline*, gated on the administrative ``ai:monitor``
        permission, and it reports counts, timings, and configuration only —
        never a question, an answer, a document, a case, or a passage.

        Availability is probed rather than inferred from the counters, for the
        same reason the search and indexing metrics probe it: a platform
        answering no questions because no credential is configured, one
        answering none because its prompts are missing, and one nobody has asked
        yet all show the same zeros.
        """
        return RagHealth(
            metrics=self._metrics.snapshot(),
            provider=self._provider.name,
            model=self._provider.model,
            llm_available=self._provider.is_available(),
            prompt_name=settings.RAG_PROMPT_TEMPLATE,
            prompt_version=settings.RAG_PROMPT_VERSION,
            prompt_available=self._prompt_available(),
            retrieval_top_k=settings.RAG_RETRIEVAL_TOP_K,
            max_context_characters=settings.RAG_MAX_CONTEXT_CHARACTERS,
            max_citations=settings.RAG_MAX_CITATIONS,
            enabled=settings.RAG_ENABLED,
        )

    # ------------------------------------------------------------- assembly #

    def _fit_context(self, passages: Sequence[SearchResultRead]) -> _ContextFit:
        """Choose which passages, and how much of each, goes into the prompt.

        The arithmetic is :func:`~core.rag.fit_to_budget`'s, so the rule can be
        tested without inventing legal prose; what happens here is applying its
        verdict to the text and recording whether anything was lost.

        Capped at ``RAG_MAX_CITATIONS`` **sources**, not merely citations: a
        passage the model is shown but cannot cite is a source the reader can
        never check, so the cap is applied where the sources are chosen rather
        than where the citations are counted.
        """
        candidates = list(passages)[: max(1, settings.RAG_MAX_CITATIONS)]
        allowances = fit_to_budget(
            [len(passage.text) for passage in candidates],
            budget=settings.RAG_MAX_CONTEXT_CHARACTERS,
            max_passage=settings.RAG_MAX_PASSAGE_CHARACTERS,
        )

        kept: list[SearchResultRead] = []
        characters = 0
        truncated = len(candidates) < len(passages)

        for passage, allowance in zip(candidates, allowances, strict=True):
            if allowance <= 0:
                truncated = True
                continue
            text = passage.text if allowance >= len(passage.text) else clip_passage(passage.text, allowance)
            if text != passage.text:
                truncated = True
            kept.append(passage.model_copy(update={"text": text}))
            characters += len(text)

        return _ContextFit(passages=kept, characters=characters, truncated=truncated)

    def _render(self, state: RagState, context: Sequence[SearchResultRead]) -> RenderedPrompt:
        """Render the configured template with the sources and the question.

        The markers are assigned **here**, before generation, and in relevance
        order — so the ``[2]`` in the prose and the second entry in the citation
        list are the same source without the client re-sorting anything, and so
        the model is never asked to invent a numbering scheme.

        Raises:
            RagUnavailableError: the template is missing or could not render.
        """
        sources = [
            {
                "marker": marker,
                "document_name": self._document_name(passage),
                "document_version": passage.document_version,
                "page_number": passage.page_number,
                "text": passage.text,
            }
            for marker, passage in enumerate(context, start=1)
        ]

        try:
            return self._prompts.render(
                settings.RAG_PROMPT_TEMPLATE,
                version=settings.RAG_PROMPT_VERSION,
                context={
                    "sources": sources,
                    "question": state["question"],
                    "language_name": language_name(state["language"]),
                    "insufficient_marker": INSUFFICIENT_EVIDENCE_MARKER,
                    "max_citations": settings.RAG_MAX_CITATIONS,
                },
            )
        except PromptError as exc:
            # A missing or broken template is a *deployment* fault, so it is
            # reported as an unavailability rather than as a bad request — the
            # caller can change nothing about their question that would fix it.
            logger.error(
                "rag_prompt_failed",
                template=settings.RAG_PROMPT_TEMPLATE,
                version=settings.RAG_PROMPT_VERSION,
                missing=isinstance(exc, PromptNotFoundError),
            )
            self._unavailable(RagFailureCode.LLM_UNAVAILABLE)

    # ------------------------------------------------------------ citations #

    def _citations(self, state: RagState) -> list[RagCitationRead]:
        """Attach a citation per source the answer was built on.

        The spec's "Citations" section, and its four required references —
        document, document version, page, case — come straight off the passage's
        provenance, which Document Indexing put on every vector and Semantic
        Search carried through unchanged. Nothing here re-derives them, so a
        citation cannot point somewhere the retrieved passage does not.

        **Every source is returned, whether the model cited it or not.** The
        spec requires citations *"whenever supporting context exists"*, and a
        model that forgot a marker has not made the evidence disappear — so the
        sources are all listed and each says whether the prose actually leaned on
        it. That keeps the list complete and honest at the same time.

        **An answer that is not grounded carries no citations at all**, and both
        halves of that matter. The spec attaches citations to a *grounded* answer,
        and an answer reading "I could not find any supporting document" beside a
        list of two sources contradicts itself in front of the reader. It also
        makes the two no-evidence paths agree: retrieval finding nothing and the
        model judging what it found insufficient are the same outcome to a caller,
        and returning citations for one and not the other would be a distinction
        they cannot act on. Nothing is concealed by it — ``retrieved_count`` and
        ``context_count`` still report what was considered.
        """
        if not state.get("grounded"):
            return []

        cited = state.get("cited", set())

        return [
            RagCitationRead(
                marker=marker,
                document_id=passage.document_id,
                document_name=self._document_name(passage),
                document_version=passage.document_version,
                page_number=passage.page_number,
                case_id=passage.case_id,
                score=passage.score,
                excerpt=passage.text,
                # Compared against the retrieved passage rather than recomputed
                # from the budget: what a reader needs to know is whether the
                # excerpt in front of them is the whole passage.
                excerpt_truncated=passage.text.endswith("…"),
                referenced=marker in cited,
            )
            for marker, passage in enumerate(state.get("context", []), start=1)
        ]

    @staticmethod
    def _document_name(passage: SearchResultRead) -> str:
        """The name a citation refers to the document by.

        A filename rather than an identifier, because ``12-rag-pipeline.md`` asks
        for citations that reference the document and adds *"do not expose
        internal identifiers unnecessarily"* — and because "Contrat de bail.pdf,
        page 7" is a citation a lawyer can act on while a UUID is not.
        """
        document = passage.document
        return document.original_filename if document is not None else str(passage.document_id)

    @staticmethod
    def _strip_invented_markers(answer: str, *, sources: int) -> str:
        """Remove citation markers that resolve to no source, and tidy the gap.

        Deliberately conservative: only the bracketed number is removed, the
        sentence around it is untouched, and the two regexes afterwards do
        nothing but repair the whitespace the removal left. An answer is the
        model's; a broken reference is not part of it.
        """
        invented = unknown_markers(answer, source_count=sources)
        if not invented:
            return answer

        cleaned = answer
        for marker in invented:
            cleaned = cleaned.replace(f"[{marker}]", "")

        cleaned = _SPACE_RUN.sub(" ", cleaned)
        cleaned = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", cleaned)
        return cleaned.strip()

    # ------------------------------------------------------------- finishing #

    def _complete(
        self, state: RagState, *, request: RagRequest, actor: User, started: float
    ) -> RagOutcome:
        """Record the run and project the final state onto an outcome.

        A run that found no evidence is recorded as a **success**, because it is
        one: the pipeline retrieved, found nothing that supports an answer, and
        said so. Counting it as a failure would make the failure rate a measure
        of the corpus rather than of the platform, and would hide a genuine
        outage behind it — the same argument :mod:`services.search` makes about
        a search that matches nothing.
        """
        duration_ms = self._elapsed_ms(started)
        citations = self._citations(state)
        completion: LLMCompletion | None = state.get("completion")
        grounded = bool(state.get("grounded"))
        generation_ms = state.get("generation_ms")

        self._metrics.record_success(
            duration_ms=duration_ms,
            retrieval_ms=state.get("retrieval_ms", 0),
            generation_ms=generation_ms,
            passage_count=len(state.get("passages", [])),
            citation_count=len(citations),
            grounded=grounded,
            prompt_tokens=completion.prompt_tokens if completion else None,
            completion_tokens=completion.completion_tokens if completion else None,
        )

        prompt = state.get("prompt")

        logger.info(
            "rag_completed",
            grounded=grounded,
            insufficient_evidence=bool(state.get("insufficient")),
            retrieved_count=len(state.get("passages", [])),
            context_count=len(state.get("context", [])),
            citation_count=len(citations),
            referenced_count=sum(1 for citation in citations if citation.referenced),
            duration_ms=duration_ms,
            retrieval_ms=state.get("retrieval_ms", 0),
            generation_ms=generation_ms,
            answer_characters=len(state.get("answer", "")),
            provider=completion.provider if completion else None,
            model=completion.model if completion else None,
            total_tokens=completion.total_tokens if completion else None,
            **self._event(request, actor=actor),
        )

        return RagOutcome(
            question=state["question"],
            answer=state.get("answer", ""),
            language=state["language"],
            grounded=grounded,
            insufficient_evidence=bool(state.get("insufficient")),
            truncated=bool(completion.truncated) if completion else False,
            citations=citations,
            retrieved_count=len(state.get("passages", [])),
            context_count=len(state.get("context", [])),
            context_characters=state.get("context_characters", 0),
            context_truncated=bool(state.get("context_truncated")),
            duration_ms=duration_ms,
            retrieval_ms=state.get("retrieval_ms", 0),
            generation_ms=generation_ms,
            # Reported even on the no-evidence branch, where no prompt was
            # rendered: which template *would* have answered is what an
            # evaluation run needs to group by, and configuration is the honest
            # source for it when nothing was rendered.
            prompt_name=prompt.name if prompt else settings.RAG_PROMPT_TEMPLATE,
            prompt_version=prompt.version if prompt else settings.RAG_PROMPT_VERSION,
            provider=completion.provider if completion else None,
            model=completion.model if completion else None,
            prompt_tokens=completion.prompt_tokens if completion else None,
            completion_tokens=completion.completion_tokens if completion else None,
            total_tokens=completion.total_tokens if completion else None,
        )

    # -------------------------------------------------------------- helpers #

    def _check_deadline(self, state: RagState, *, stage: str) -> None:
        """Refuse to begin a stage after the run's deadline has passed.

        Checked **between** stages rather than inside them, and the reason is the
        same one :mod:`services.indexing` records: neither the search service nor
        a provider SDK accepts a deadline that can be moved mid-call, and
        interrupting one is not something a thread can do safely. So the honest
        guarantee is *"no new stage begins after the deadline"*, which bounds a
        run at one stage's overrun instead of claiming a precision the libraries
        do not offer. The alternative — no deadline at all — is a request that
        never returns.

        Raises:
            RagUnavailableError: the deadline has passed.
        """
        if time.monotonic() <= state["deadline"]:
            return

        logger.error(
            "rag_deadline_exceeded",
            stage=stage,
            budget_seconds=settings.RAG_TIMEOUT_SECONDS,
            elapsed_ms=self._elapsed_ms(state["started"]),
        )
        self._unavailable(RagFailureCode.TIMEOUT)

    def _unavailable(self, code: RagFailureCode) -> NoReturn:
        """Raise the 503 that describes a dependency failure.

        ``NoReturn`` is what lets the call sites be statements and still satisfy
        the type checker that the code after them is unreachable — the same shape
        :meth:`services.search.SearchService._fail` uses. It records **nothing**:
        metrics are recorded once, in :meth:`answer`, from the exception this
        raises.

        Raises:
            RagUnavailableError: always.
        """
        raise RagUnavailableError(code.value, failure_message(code))

    def _record_failure(
        self,
        request: RagRequest,
        *,
        actor: User,
        started: float,
        error_code: str,
        already_logged: bool = False,
    ) -> None:
        """Count one failed run, and say why.

        One method rather than a ``record`` call beside each ``raise``, because
        the two must not come apart: a failure path that raised without recording
        would make the failure rate quietly under-report the exact outages the
        metric exists to surface.
        """
        duration_ms = self._elapsed_ms(started)
        try:
            code = RagFailureCode(error_code)
        except ValueError:  # pragma: no cover - defensive
            code = RagFailureCode.UNKNOWN

        self._metrics.record_failure(duration_ms=duration_ms, code=code)

        if not already_logged:
            logger.error(
                "rag_failed",
                error_code=code.value,
                duration_ms=duration_ms,
                **self._event(request, actor=actor),
            )

    def _prompt_available(self) -> bool:
        """Whether the configured template can actually be loaded here."""
        if not self._prompts.is_available():
            return False
        return settings.RAG_PROMPT_VERSION in self._prompts.versions(
            settings.RAG_PROMPT_TEMPLATE
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """Milliseconds since ``started``.

        From :func:`time.monotonic` rather than from wall-clock timestamps: a
        clock adjustment mid-request would produce a negative latency, and a
        negative sample in a monitoring average is worse than a slightly
        imprecise one.
        """
        return max(0, int((time.monotonic() - started) * 1000))

    @staticmethod
    def _event(request: RagRequest, *, actor: User) -> dict[str, Any]:
        """The fields every pipeline log entry carries.

        **The question itself is not one of them, and neither is the answer.**
        ``12-rag-pipeline.md``: *"Never log confidential document contents."* A
        lawyer's question names what they are looking for and an answer is an
        interpretation of a client's file, so both are at least as sensitive as
        the passages the answer was built from — which OCR, indexing, and search
        each already refuse to log.

        What is logged instead is the salted, non-reversible fingerprint
        :mod:`core.search` computes, which is the *same* handle a search for the
        same text produces — so an operator can correlate "this question fails
        every time" across both surfaces while learning nothing about the matter.

        ``RAG_LOG_QUESTIONS`` adds the question text beside the fingerprint,
        never in place of it. There is deliberately **no switch that logs the
        answer**: an answer is generated content about a client's case, and no
        operational question is worth putting it in a log file.
        """
        return {
            "actor_id": str(actor.id),
            "role": actor.role.value,
            "question_fingerprint": question_fingerprint(request.question),
            "question_length": len(request.question),
            "question": loggable_question(request.question),
            "language": request.language,
            "filtered": not request.filters.is_empty,
        }


def citation_document_ids(citations: Sequence[RagCitationRead]) -> list[uuid.UUID]:
    """The distinct documents an answer cited, in citation order.

    Not used by the pipeline itself — it is here for the features the spec says
    this one prepares for: the AI Assistant needs it to render "sources: 3
    documents", and the report agent needs it to list the files a section rests
    on. Both would otherwise write the same de-duplication twice.
    """
    seen: dict[uuid.UUID, None] = {}
    for citation in citations:
        seen.setdefault(citation.document_id, None)
    return list(seen)


__all__ = [
    "RagHealth",
    "RagOutcome",
    "RagService",
    "RagStreamEvent",
    "RagStreamEventKind",
    "citation_document_ids",
]
