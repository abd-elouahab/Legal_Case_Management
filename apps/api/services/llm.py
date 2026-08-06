"""The language-model boundary.

``ai-architecture.md`` is explicit about the shape this takes:

.. code-block:: text

    Application → LLM Provider Interface → Gemini Provider → Gemini API

and *"The system must never call the Gemini SDK directly outside the provider
implementation."* ``12-rag-pipeline.md`` adds *"Do not hardcode a specific LLM
provider"* and *"The implementation must make provider replacement possible
without changing the orchestration logic."*

This module is that boundary, and it is the **only** place in the platform that
imports a model SDK. The shape mirrors every other seam in this codebase
(:mod:`services.ocr_engine`, :mod:`services.chunking`,
:mod:`services.embedding`, :mod:`services.vector_store`,
:mod:`services.vector_search`, :mod:`services.prompts`):

* :class:`LLMProvider` is the protocol :mod:`services.rag` depends on — the six
  responsibilities ``ai-architecture.md`` lists under "Provider Interface": text
  generation, streaming generation, model selection, token counting, retry
  handling, and error normalization;
* :class:`GeminiProvider` is the default, because ``ai-architecture.md`` names
  ``gemini-2.5-flash``;
* :class:`LiteLLMProvider` is the second, because `architecture.md` names LiteLLM
  as the gateway to OpenAI, Ollama, and future models and
  ``ai-workflow-rules.md`` requires models to stay *"replaceable through
  LiteLLM"*. It is present so that ``PROVIDER_FACTORIES`` has two real entries
  rather than an aspirational one — a seam with a single implementation is a
  claim, and a seam with two is a fact;
* :func:`get_llm_provider` resolves the configured provider by identifier.

**Every SDK failure is translated here**, into an :class:`LLMError` carrying a
:class:`~core.rag.RagFailureCode`. That is what lets the pipeline above record a
*cause* without knowing what a ``ClientError`` is — and, just as importantly, it
is what keeps the SDK's own message out of the log and out of the response. A
model SDK's exception text routinely echoes the prompt it was sent, and the
prompt here contains passages of a client's legal file.

**Retries live here, not in the orchestration.** ``12-rag-pipeline.md`` requires
retry handling and ``code-standards.md`` requires exponential backoff; putting
them in the provider means the graph above stays a description of *what happens*
rather than of what happens when the network is flaky, and means a second
provider inherits the policy instead of reimplementing it. Only transient
failures are retried — a refused credential retried three times is three refusals
and a slower error.

**Nothing here retrieves, prompts, or cites.** A provider is handed a system
instruction and a user turn and returns text. It has no repository, no user, no
search service, and no way to reach a document — which is what makes it
impossible for a future provider to become a second, unauthorized retrieval path.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

import structlog

from core.config import settings
from core.rag import RagFailureCode

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class LLMError(Exception):
    """The language model could not produce a completion.

    Deliberately **not** an :class:`~core.exceptions.AppException`, exactly as
    :class:`~services.embedding.EmbeddingError` and
    :class:`~services.vector_search.VectorSearchError` are not: this module is a
    library boundary, and translating a failure into a status line is the
    service's job. :class:`~services.rag.RagService` raises
    :class:`~core.exceptions.RagUnavailableError` carrying this code.
    """

    #: The cause, reported to the caller and used to group failures in the
    #: monitoring view.
    code: RagFailureCode = RagFailureCode.LLM_FAILURE
    #: Whether trying the identical request again could plausibly succeed. A
    #: rejected API key is not retryable; a 503 from the provider is.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: RagFailureCode | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.code = code or self.code
        self.retryable = self.retryable if retryable is None else retryable
        super().__init__(message)


class LLMUnavailableError(LLMError):
    """No provider is configured here, or the SDK is not installed.

    An operational fault rather than a fault of the question: the same question
    will be answered once a key is configured. Not retryable, because nothing
    about a retry supplies a missing credential — the same posture
    :class:`~services.embedding.EmbedderUnavailableError` takes for a missing
    model file.
    """

    code = RagFailureCode.LLM_UNAVAILABLE
    retryable = False


class LLMTimeoutError(LLMError):
    """The model did not answer inside the deadline.

    Retryable: a deadline is a property of one attempt, and the platform's
    experience of a slow model is exactly the case a second attempt exists for.
    """

    code = RagFailureCode.TIMEOUT
    retryable = True


class LLMTransientError(LLMError):
    """The provider failed in a way that may not recur — rate limit, 5xx, reset."""

    code = RagFailureCode.LLM_FAILURE
    retryable = True


# --------------------------------------------------------------------------- #
# What comes back
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LLMCompletion:
    """One generated answer, and everything the provider said about producing it.

    The provider and model identities travel with the text rather than being read
    from configuration afterwards, for the same reason an index records the
    embedding model it was built with: configuration is *current* and an answer
    is *historical*. An evaluation comparing two weeks of answers is only
    possible if each one says what produced it.
    """

    text: str
    provider: str
    model: str
    #: Token counts, when the provider reports them. ``None`` rather than ``0``
    #: throughout: ``ai-architecture.md`` asks for token counting *"when
    #: available"*, and zero would read as "this call was free", which is a
    #: different and false statement.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: The provider's own word for why generation stopped, passed through rather
    #: than mapped onto a platform vocabulary — it is diagnostic, and normalising
    #: it would lose the detail an operator opens the log for.
    finish_reason: str | None = None
    #: Whether the answer was cut off at the output ceiling. Reported to the
    #: caller, because a truncated legal answer that ends mid-sentence must not
    #: be presented as a complete one.
    truncated: bool = False


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class LLMProvider(Protocol):
    """What the RAG pipeline requires of a language model.

    Five members, and none of them mentions a document, a passage, a case, a
    user, or a citation. A provider is handed two strings and returns one. That
    narrowness is the seam: it is what makes "replace the provider without
    changing the orchestration logic" a property of the type system rather than a
    promise in a document.
    """

    @property
    def name(self) -> str:
        """Stable identifier of the provider ("gemini")."""
        ...

    @property
    def model(self) -> str:
        """The model this provider is configured to call ("gemini-2.5-flash")."""
        ...

    def is_available(self) -> bool:
        """Whether this provider can be used here, right now."""
        ...

    def count_tokens(self, text: str) -> int | None:
        """Tokens ``text`` occupies for this model, or ``None`` when unknown.

        Optional by design: ``ai-architecture.md`` asks for token counting *"when
        available"*, and a provider that cannot count must be able to say so
        rather than guess. The platform's own budgets are in **characters** for
        exactly this reason (see :func:`~core.rag.fit_to_budget`), so a ``None``
        here costs a number on a monitoring page and nothing else.
        """
        ...

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> LLMCompletion:
        """Generate one completion.

        Raises:
            LLMError: the completion could not be produced.
        """
        ...

    def stream(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> Iterator[str]:
        """Generate one completion as a sequence of text fragments.

        On the protocol because ``ai-architecture.md`` lists streaming generation
        as a provider responsibility and the AI Assistant will need it.
        **Deliberately unused by this feature**: ``12-rag-pipeline.md`` puts
        response streaming under *Extensibility*, not under *Goals*, and a
        pipeline that streamed would have to emit citations before it had
        finished deciding which sources the answer used.

        Raises:
            LLMError: the stream could not be produced.
        """
        ...


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times a transient provider failure is retried, and how patiently.

    Shared by every provider rather than reimplemented in each, so a second
    backend inherits the platform's policy instead of inventing one.
    """

    attempts: int
    backoff_seconds: float

    @classmethod
    def from_settings(cls) -> RetryPolicy:
        """The deployment's configured policy."""
        return cls(
            attempts=max(1, settings.LLM_MAX_ATTEMPTS),
            backoff_seconds=max(0.0, settings.LLM_RETRY_BACKOFF_SECONDS),
        )


def with_retries[T](
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    provider: str,
    model: str,
) -> T:
    """Run ``operation``, retrying transient failures with exponential backoff.

    ``code-standards.md`` requires exponential backoff for retried work, and the
    doubling here is that: a provider under load that refused one request in a
    burst is far more likely to accept the second a second later than one
    immediately after.

    **Only :attr:`LLMError.retryable` failures are retried.** Retrying a rejected
    credential or a malformed request produces the same rejection three times and
    turns a fast, clear error into a slow one — and, on a metered API, pays for
    it. The last failure is re-raised unchanged, so the caller sees the real
    cause rather than a wrapper.
    """
    last: LLMError | None = None

    for attempt in range(1, policy.attempts + 1):
        try:
            return operation()
        except LLMError as exc:
            last = exc
            if not exc.retryable or attempt >= policy.attempts:
                raise
            delay = policy.backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "llm_retry",
                provider=provider,
                model=model,
                attempt=attempt,
                attempts=policy.attempts,
                error_code=exc.code.value,
                delay_seconds=round(delay, 3),
            )
            if delay > 0:
                time.sleep(delay)

    # Unreachable: the loop either returns or raises. Present so the function has
    # one exit type rather than an implicit `None` the checker has to reason about.
    raise last or LLMError("The AI service could not be reached.")


# --------------------------------------------------------------------------- #
# Google Gemini
# --------------------------------------------------------------------------- #

#: Substrings of an SDK error that mark it as worth trying again.
#:
#: Matched on the exception's *type name and status*, never on its message body:
#: a provider's message can quote the prompt, and the prompt here contains a
#: client's legal documents. Kept as data so a new provider's vocabulary is an
#: edit to a tuple rather than to a chain of ``if``\\ s.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504})

#: Type names, lowercased, that always mean "try again".
_RETRYABLE_TYPE_HINTS: tuple[str, ...] = (
    "timeout",
    "connect",
    "connection",
    "servererror",
    "unavailable",
    "resourceexhausted",
    "ratelimit",
)

#: Type names, lowercased, that always mean "do not try again".
_FATAL_TYPE_HINTS: tuple[str, ...] = (
    "permissiondenied",
    "unauthenticated",
    "invalidargument",
    "notfound",
)


def _classify(exc: Exception) -> LLMError:
    """Turn an SDK exception into a platform error, without quoting it.

    The SDK's own text is never carried into the raised error and never logged,
    because it can echo the prompt — which here is a client's legal file. What is
    used is the exception's *type name* and, when the SDK exposes one, its HTTP
    *status code*: both are structural, neither is content.
    """
    name = type(exc).__name__.casefold()
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)

    if any(hint in name for hint in _FATAL_TYPE_HINTS):
        return LLMError(
            "The AI service refused this request.", code=RagFailureCode.LLM_FAILURE, retryable=False
        )

    if "timeout" in name or "deadline" in name:
        return LLMTimeoutError("The AI service did not respond in time.")

    if isinstance(status, int) and status in _RETRYABLE_STATUS_CODES:
        return LLMTransientError("The AI service is temporarily unavailable.")

    if any(hint in name for hint in _RETRYABLE_TYPE_HINTS):
        return LLMTransientError("The AI service is temporarily unavailable.")

    return LLMError("The AI service could not answer this request.")


class GeminiProvider:
    """Google Gemini through the ``google-genai`` SDK.

    ``ai-architecture.md`` selects Gemini for its generous free tier, long
    context window, and — the reason that matters most here — *excellent
    multilingual support*: this platform answers in Arabic and in French from a
    corpus that routinely mixes both inside one filing.

    The client is built lazily and once, behind a lock, for the same reason the
    embedding model is: constructing it at import would make startup depend on
    configuration that only this feature needs, and per request would build a
    connection pool per question.
    """

    #: The identifier recorded for this backend.
    name = "gemini"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        policy: RetryPolicy | None = None,
        client: Any | None = None,
    ) -> None:
        self._model = model or settings.LLM_MODEL
        self._api_key = api_key if api_key is not None else settings.LLM_API_KEY
        self._policy = policy or RetryPolicy.from_settings()
        # Injectable so a unit test can drive the translation, retry, and usage
        # handling with a stub and no API key — the same reason
        # `QdrantVectorSearcher` takes one.
        self._client = client
        self._lock = threading.Lock()

    # ------------------------------------------------------------ identity #

    @property
    def model(self) -> str:
        """The model recorded on every answer this produces."""
        return self._model

    def is_available(self) -> bool:
        """Whether a client can be constructed here.

        **This is a configuration probe, not a reachability probe**, and the
        distinction is stated rather than hidden: it reports that the SDK is
        installed and a credential is present, not that the credential is
        accepted. Proving the second means a network round trip, and the
        monitoring endpoint is not the place to spend one on every refresh — a
        rejected key surfaces as an ``llm_failure`` in ``failures_by_code``,
        which is where an operator would look next anyway.
        """
        try:
            self._genai()
        except LLMError:
            return False
        return True

    # -------------------------------------------------------------- tokens #

    def count_tokens(self, text: str) -> int | None:
        """Ask Gemini how many tokens ``text`` occupies.

        Returns ``None`` on any failure rather than raising: a token count is
        reporting, and a monitoring figure must never be the reason an answer
        the user already has is thrown away.
        """
        if not text:
            return 0
        try:
            response = self._genai().models.count_tokens(model=self._model, contents=text)
        except Exception:
            logger.info("llm_token_count_unavailable", provider=self.name, model=self._model)
            return None
        total = getattr(response, "total_tokens", None)
        return int(total) if isinstance(total, int) else None

    # ------------------------------------------------------------ generate #

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> LLMCompletion:
        """Generate one grounded answer.

        Raises:
            LLMUnavailableError: no key, or the SDK is not installed.
            LLMTimeoutError: the deadline passed.
            LLMError: the model refused, or failed.
        """
        client = self._genai()
        config = self._config(
            system=system,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

        def call() -> Any:
            try:
                return client.models.generate_content(
                    model=self._model, contents=prompt, config=config
                )
            except Exception as exc:
                raise self._translate(exc, "generate_content") from exc

        return self._completion(
            with_retries(call, policy=self._policy, provider=self.name, model=self._model)
        )

    def stream(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> Iterator[str]:
        """Stream one answer as text fragments.

        Not retried, unlike :meth:`generate`: a stream that fails halfway has
        already delivered text to the caller, and silently restarting would
        duplicate it. The caller decides whether to start again.
        """
        client = self._genai()
        config = self._config(
            system=system,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

        try:
            chunks = client.models.generate_content_stream(
                model=self._model, contents=prompt, config=config
            )
            for chunk in chunks:
                text = getattr(chunk, "text", None)
                if text:
                    yield str(text)
        except LLMError:
            raise
        except Exception as exc:
            raise self._translate(exc, "generate_content_stream") from exc

    # ------------------------------------------------------------- helpers #

    def _config(
        self,
        *,
        system: str,
        temperature: float | None,
        max_output_tokens: int | None,
        timeout_seconds: float | None,
    ) -> Any:
        """Build the SDK's request configuration.

        The system instruction is passed as a *system instruction* rather than
        prepended to the prompt, because Gemini weights the two differently — and
        because the grounding rules must not sit inside the same block as the
        untrusted text they govern.
        """
        from google.genai import types

        deadline = timeout_seconds if timeout_seconds is not None else settings.LLM_TIMEOUT_SECONDS

        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=(
                temperature if temperature is not None else settings.LLM_TEMPERATURE
            ),
            max_output_tokens=(
                max_output_tokens
                if max_output_tokens is not None
                else settings.LLM_MAX_OUTPUT_TOKENS
            ),
            # The SDK takes milliseconds; the platform configures seconds, like
            # every other timeout in `core/config.py`.
            http_options=types.HttpOptions(timeout=int(deadline * 1000)),
        )

    def _completion(self, response: Any) -> LLMCompletion:
        """Project an SDK response onto :class:`LLMCompletion`.

        A response with no text is returned as an **empty** completion rather
        than raised on. Whether an empty answer is a failure is a decision for
        the pipeline's response-validation step — which is where the spec puts it
        — and a provider that raised here would take that decision away from it.
        """
        usage = getattr(response, "usage_metadata", None)
        finish = self._finish_reason(response)

        return LLMCompletion(
            text=str(getattr(response, "text", None) or ""),
            provider=self.name,
            model=str(getattr(response, "model_version", None) or self._model),
            prompt_tokens=_as_optional_int(getattr(usage, "prompt_token_count", None)),
            completion_tokens=_as_optional_int(getattr(usage, "candidates_token_count", None)),
            total_tokens=_as_optional_int(getattr(usage, "total_token_count", None)),
            finish_reason=finish,
            truncated=finish is not None and finish.upper() == "MAX_TOKENS",
        )

    @staticmethod
    def _finish_reason(response: Any) -> str | None:
        """Why generation stopped, from the candidate or from a prompt block.

        A prompt refused by the provider's safety filters has **no candidate at
        all**, so reading only the candidate would report ``None`` and make a
        refusal indistinguishable from a successful empty answer. The block
        reason is reported instead, which is what tells an operator that the
        pipeline is being refused rather than failing.
        """
        feedback = getattr(response, "prompt_feedback", None)
        blocked = getattr(feedback, "block_reason", None)
        if blocked is not None:
            return f"BLOCKED_{_enum_value(blocked)}"

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        reason = getattr(candidates[0], "finish_reason", None)
        return _enum_value(reason) if reason is not None else None

    def _translate(self, exc: Exception, operation: str) -> LLMError:
        """Translate an SDK failure, keeping its message out of the log."""
        error = _classify(exc)
        logger.error(
            "llm_call_failed",
            provider=self.name,
            model=self._model,
            operation=operation,
            error_type=type(exc).__name__,
            error_code=error.code.value,
            retryable=error.retryable,
        )
        return error

    def _genai(self) -> Any:
        """Build (and cache) the SDK client.

        Raises:
            LLMUnavailableError: the SDK is not installed, no key is configured,
                or the client could not be constructed.
        """
        if self._client is not None:
            return self._client

        with self._lock:
            if self._client is not None:
                return self._client

            if not self._api_key:
                # Logged once per process rather than per question: a deployment
                # without a key is a standing condition, not an event.
                logger.warning("llm_unavailable", provider=self.name, reason="no_api_key")
                raise LLMUnavailableError("No AI provider credential is configured.")

            try:
                from google import genai
            except ImportError as exc:
                logger.error("llm_unavailable", provider=self.name, reason="library_missing")
                raise LLMUnavailableError("The AI provider library is not installed.") from exc

            try:
                self._client = genai.Client(api_key=self._api_key)
            except Exception as exc:
                logger.error(
                    "llm_unavailable",
                    provider=self.name,
                    reason="client_failed",
                    error_type=type(exc).__name__,
                )
                raise LLMUnavailableError("The AI provider client could not be built.") from exc

            return self._client


# --------------------------------------------------------------------------- #
# LiteLLM
# --------------------------------------------------------------------------- #


class LiteLLMProvider:
    """Any model reachable through LiteLLM's unified interface.

    `architecture.md` names LiteLLM as the platform's *"unified interface for
    OpenAI, Ollama, and future models"*, and ``ai-workflow-rules.md`` requires
    models to remain *"replaceable through LiteLLM"*. This class is that
    requirement, and it is also what makes the provider seam demonstrable rather
    than asserted: :data:`PROVIDER_FACTORIES` has two entries, and neither
    knows about the other.

    ``litellm`` is **not** in ``requirements.txt`` and is not installed by
    default — it pulls a large dependency tree that a Gemini deployment does not
    need. The import is lazy, so its absence is reported as
    :class:`LLMUnavailableError` (and ``llm_available: false`` on the monitoring
    endpoint) exactly as a missing Tesseract or a missing embedding model is,
    rather than breaking startup for every deployment that does not use it.
    """

    #: The identifier recorded for this backend.
    name = "litellm"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        policy: RetryPolicy | None = None,
        client: Any | None = None,
    ) -> None:
        self._model = model or settings.LLM_MODEL
        self._api_key = api_key if api_key is not None else settings.LLM_API_KEY
        self._policy = policy or RetryPolicy.from_settings()
        self._client = client

    # ------------------------------------------------------------ identity #

    @property
    def model(self) -> str:
        """The model recorded on every answer this produces."""
        return self._model

    def is_available(self) -> bool:
        """Whether the library is importable here. See :meth:`GeminiProvider.is_available`."""
        try:
            self._litellm()
        except LLMError:
            return False
        return True

    def count_tokens(self, text: str) -> int | None:
        """Count tokens with LiteLLM's model-aware counter, or report ``None``."""
        if not text:
            return 0
        try:
            return int(self._litellm().token_counter(model=self._model, text=text))
        except Exception:
            logger.info("llm_token_count_unavailable", provider=self.name, model=self._model)
            return None

    # ------------------------------------------------------------ generate #

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> LLMCompletion:
        """Generate one grounded answer through the gateway.

        Raises:
            LLMUnavailableError: LiteLLM is not installed.
            LLMTimeoutError: the deadline passed.
            LLMError: the model refused, or failed.
        """
        client = self._litellm()
        kwargs = self._kwargs(
            system=system,
            prompt=prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

        def call() -> Any:
            try:
                return client.completion(**kwargs)
            except Exception as exc:
                raise self._translate(exc, "completion") from exc

        return self._completion(
            with_retries(call, policy=self._policy, provider=self.name, model=self._model)
        )

    def stream(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> Iterator[str]:
        """Stream one answer as text fragments. Not retried — see :meth:`GeminiProvider.stream`."""
        client = self._litellm()
        kwargs = self._kwargs(
            system=system,
            prompt=prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

        try:
            for chunk in client.completion(stream=True, **kwargs):
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                text = getattr(delta, "content", None)
                if text:
                    yield str(text)
        except LLMError:
            raise
        except Exception as exc:
            raise self._translate(exc, "completion_stream") from exc

    # ------------------------------------------------------------- helpers #

    def _kwargs(
        self,
        *,
        system: str,
        prompt: str,
        temperature: float | None,
        max_output_tokens: int | None,
        timeout_seconds: float | None,
    ) -> dict[str, Any]:
        """The request, in the OpenAI-shaped vocabulary LiteLLM normalises from."""
        arguments: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": (
                temperature if temperature is not None else settings.LLM_TEMPERATURE
            ),
            "max_tokens": (
                max_output_tokens
                if max_output_tokens is not None
                else settings.LLM_MAX_OUTPUT_TOKENS
            ),
            "timeout": (
                timeout_seconds if timeout_seconds is not None else settings.LLM_TIMEOUT_SECONDS
            ),
        }
        if self._api_key:
            arguments["api_key"] = self._api_key
        return arguments

    def _completion(self, response: Any) -> LLMCompletion:
        """Project a LiteLLM response onto :class:`LLMCompletion`."""
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        finish = str(getattr(choices[0], "finish_reason", "") or "") if choices else ""
        usage = getattr(response, "usage", None)

        return LLMCompletion(
            text=str(getattr(message, "content", None) or ""),
            provider=self.name,
            model=str(getattr(response, "model", None) or self._model),
            prompt_tokens=_as_optional_int(getattr(usage, "prompt_tokens", None)),
            completion_tokens=_as_optional_int(getattr(usage, "completion_tokens", None)),
            total_tokens=_as_optional_int(getattr(usage, "total_tokens", None)),
            finish_reason=finish or None,
            truncated=finish.lower() == "length",
        )

    def _translate(self, exc: Exception, operation: str) -> LLMError:
        """Translate a gateway failure, keeping its message out of the log."""
        error = _classify(exc)
        logger.error(
            "llm_call_failed",
            provider=self.name,
            model=self._model,
            operation=operation,
            error_type=type(exc).__name__,
            error_code=error.code.value,
            retryable=error.retryable,
        )
        return error

    def _litellm(self) -> Any:
        """The LiteLLM module, or the injected stub.

        Raises:
            LLMUnavailableError: the library is not installed.
        """
        if self._client is not None:
            return self._client

        try:
            import litellm
        except ImportError as exc:
            logger.warning("llm_unavailable", provider=self.name, reason="library_missing")
            raise LLMUnavailableError("The AI gateway library is not installed.") from exc

        return litellm


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _as_optional_int(value: Any) -> int | None:
    """Coerce a usage figure, keeping "not reported" distinct from zero."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enum_value(value: Any) -> str:
    """Render an SDK enum (or a plain string) as a stable identifier."""
    return str(getattr(value, "name", None) or getattr(value, "value", None) or value)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every provider this build can be configured to use.
#:
#: Adding one is a class implementing :class:`LLMProvider` plus an entry here —
#: ``ai-architecture.md``'s "future providers should be implementable without
#: changing application logic", in the same shape as
#: :data:`~services.embedding.EMBEDDER_FACTORIES`.
PROVIDER_FACTORIES: Mapping[str, type[GeminiProvider] | type[LiteLLMProvider]] = MappingProxyType(
    {
        GeminiProvider.name: GeminiProvider,
        LiteLLMProvider.name: LiteLLMProvider,
    }
)

#: The one provider the process shares, built on first use.
#:
#: Shared rather than per request because it owns an HTTP client and its
#: connection pool; a provider per question would open a pool per question. The
#: same reasoning that makes the embedder process-wide, at a much smaller scale.
_shared: dict[str, LLMProvider] = {}
_shared_lock = threading.Lock()


def available_providers() -> list[str]:
    """Every provider identifier this build can be configured to use."""
    return sorted(PROVIDER_FACTORIES)


def get_llm_provider(identifier: str | None = None) -> LLMProvider:
    """Return the configured provider, shared across the process.

    Falls back to the default backend for an unrecognised identifier rather than
    failing startup, with the fallback logged so the misconfiguration is visible
    — the same posture every other factory in this codebase takes. Startup must
    not depend on an AI setting: an API that refuses to come up over a
    mistyped provider name would take authentication, cases, and documents down
    with it.
    """
    wanted = (identifier or settings.LLM_PROVIDER).strip().lower()

    factory = PROVIDER_FACTORIES.get(wanted)
    if factory is None:
        logger.warning("llm_provider_unknown", requested=wanted, fallback=GeminiProvider.name)
        wanted = GeminiProvider.name
        factory = GeminiProvider

    cached = _shared.get(wanted)
    if cached is not None:
        return cached

    with _shared_lock:
        existing = _shared.get(wanted)
        if existing is not None:
            return existing
        built: LLMProvider = factory()
        _shared[wanted] = built
        return built


def reset_llm_provider_cache() -> None:
    """Discard the shared provider.

    For tests, and for a deployment that re-keys at runtime. Not called by the
    application.
    """
    with _shared_lock:
        _shared.clear()


__all__ = [
    "PROVIDER_FACTORIES",
    "GeminiProvider",
    "LLMCompletion",
    "LLMError",
    "LLMProvider",
    "LLMTimeoutError",
    "LLMTransientError",
    "LLMUnavailableError",
    "LiteLLMProvider",
    "RetryPolicy",
    "available_providers",
    "get_llm_provider",
    "reset_llm_provider_cache",
    "with_retries",
]
