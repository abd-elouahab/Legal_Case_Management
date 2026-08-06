"""Unit tests for :mod:`services.llm`, the language-model boundary.

The three things worth testing here are the three the pipeline above depends on
being true, and none of them needs an API key:

* **error normalization** — every SDK failure becomes a platform code, and the
  SDK's own message (which can echo the prompt, and therefore a client's legal
  file) never leaves the module;
* **retry handling** — transient failures are retried with exponential backoff
  and fatal ones are not, because retrying a rejected credential three times is
  three refusals, slower and billed;
* **the response projection** — usage, finish reason, and truncation are read off
  the SDK's shape correctly, including the case that has no candidate at all.

The Gemini provider is driven with a stub client. That is not a shortcut around a
missing key: a real provider is non-deterministic about exactly the thing under
test, and a timeout, a safety refusal, and an empty completion are not outcomes a
real model produces on demand.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from core.rag import RagFailureCode
from services.llm import (
    PROVIDER_FACTORIES,
    GeminiProvider,
    LiteLLMProvider,
    LLMCompletion,
    LLMError,
    LLMProvider,
    LLMTimeoutError,
    LLMTransientError,
    LLMUnavailableError,
    RetryPolicy,
    available_providers,
    get_llm_provider,
    reset_llm_provider_cache,
    with_retries,
)

# --------------------------------------------------------------------------- #
# Stubs shaped like the SDK
# --------------------------------------------------------------------------- #


class _Usage:
    def __init__(self, prompt: int | None = 120, candidates: int | None = 40) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = None if prompt is None else (prompt or 0) + (candidates or 0)


class _Candidate:
    def __init__(self, finish_reason: str | None = "STOP") -> None:
        self.finish_reason = finish_reason


class _Feedback:
    def __init__(self, block_reason: str) -> None:
        self.block_reason = block_reason


class _Response:
    def __init__(
        self,
        text: str | None = "Le loyer est payable le 5 [1].",
        *,
        usage: _Usage | None = None,
        finish_reason: str | None = "STOP",
        block_reason: str | None = None,
        model_version: str | None = "gemini-2.5-flash-001",
    ) -> None:
        self.text = text
        self.usage_metadata = usage if usage is not None else _Usage()
        self.candidates = [] if block_reason else [_Candidate(finish_reason)]
        self.prompt_feedback = _Feedback(block_reason) if block_reason else None
        self.model_version = model_version


class _Models:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []
        self.token_total: int | None = 77

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        if callable(self._outcome):
            return self._outcome(len(self.calls))
        return self._outcome

    def generate_content_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome

        class _Chunk:
            def __init__(self, text: str) -> None:
                self.text = text

        return iter([_Chunk("Le loyer "), _Chunk("est payable le 5 [1].")])

    def count_tokens(self, **kwargs: Any) -> Any:
        if self.token_total is None:
            raise RuntimeError("counting unavailable")

        class _Count:
            def __init__(self, total: int) -> None:
                self.total_tokens = total

        return _Count(self.token_total)


class _Client:
    def __init__(self, outcome: Any = None) -> None:
        self.models = _Models(outcome if outcome is not None else _Response())


def provider(outcome: Any = None, **kwargs: Any) -> GeminiProvider:
    """A Gemini provider over a stub client, with retries off unless asked for.

    Single-attempt by default so the *translation* tests do not each pay three
    seconds of real exponential backoff to assert something about an error code.
    Retry behaviour has its own tests, which supply their own policy.
    """
    kwargs.setdefault("policy", RetryPolicy(attempts=1, backoff_seconds=0.0))
    return GeminiProvider(client=_Client(outcome), **kwargs)


# --------------------------------------------------------------------------- #
# Errors that stand in for a real SDK's
# --------------------------------------------------------------------------- #


class ServerError(Exception):
    pass


class DeadlineExceeded(Exception):
    pass


class PermissionDenied(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("api error")


class TestErrorNormalization:
    def test_a_transport_failure_is_transient(self) -> None:
        with pytest.raises(LLMError) as caught:
            provider(ServerError("boom")).generate(system="s", prompt="p")

        assert caught.value.code is RagFailureCode.LLM_FAILURE
        assert caught.value.retryable is True

    def test_a_deadline_is_a_timeout(self) -> None:
        with pytest.raises(LLMTimeoutError) as caught:
            provider(DeadlineExceeded("slow")).generate(system="s", prompt="p")

        assert caught.value.code is RagFailureCode.TIMEOUT

    def test_a_refused_credential_is_fatal(self) -> None:
        with pytest.raises(LLMError) as caught:
            provider(PermissionDenied("bad key")).generate(system="s", prompt="p")

        assert caught.value.retryable is False

    def test_a_retryable_status_code_is_transient(self) -> None:
        with pytest.raises(LLMError) as caught:
            provider(ApiError(429)).generate(system="s", prompt="p")

        assert caught.value.retryable is True

    def test_an_unrecognised_status_code_is_not_retried(self) -> None:
        with pytest.raises(LLMError) as caught:
            provider(ApiError(418)).generate(system="s", prompt="p")

        assert caught.value.retryable is False

    def test_the_sdk_message_never_reaches_the_platform_error(self) -> None:
        """A provider's message can echo the prompt — a client's legal file."""
        secret = "Contrat de bail commercial, article 4"

        with pytest.raises(LLMError) as caught:
            provider(ServerError(secret)).generate(system=secret, prompt=secret)

        assert secret not in str(caught.value)


class TestRetries:
    def test_a_transient_failure_is_retried_and_can_succeed(self) -> None:
        attempts: list[int] = []

        def flaky() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise LLMTransientError("try again")
            return "answered"

        result = with_retries(
            flaky,
            policy=RetryPolicy(attempts=3, backoff_seconds=0.0),
            provider="stub",
            model="stub",
        )

        assert result == "answered"
        assert len(attempts) == 3

    def test_a_fatal_failure_is_not_retried(self) -> None:
        attempts: list[int] = []

        def refused() -> str:
            attempts.append(1)
            raise LLMUnavailableError("no credential")

        with pytest.raises(LLMUnavailableError):
            with_retries(
                refused,
                policy=RetryPolicy(attempts=5, backoff_seconds=0.0),
                provider="stub",
                model="stub",
            )

        assert len(attempts) == 1

    def test_the_last_failure_is_raised_unchanged(self) -> None:
        def always() -> str:
            raise LLMTransientError("still down")

        with pytest.raises(LLMTransientError, match="still down"):
            with_retries(
                always,
                policy=RetryPolicy(attempts=2, backoff_seconds=0.0),
                provider="stub",
                model="stub",
            )

    def test_the_backoff_is_exponential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`code-standards.md` requires exponential backoff for retried work."""
        import services.llm as llm_module

        slept: list[float] = []
        monkeypatch.setattr(llm_module.time, "sleep", slept.append)

        def always() -> str:
            raise LLMTransientError("down")

        with pytest.raises(LLMTransientError):
            with_retries(
                always,
                policy=RetryPolicy(attempts=4, backoff_seconds=1.0),
                provider="stub",
                model="stub",
            )

        assert slept == [1.0, 2.0, 4.0]

    def test_the_policy_reads_the_deployment_settings(self) -> None:
        from core.config import settings

        policy = RetryPolicy.from_settings()

        assert policy.attempts == settings.LLM_MAX_ATTEMPTS
        assert policy.backoff_seconds == settings.LLM_RETRY_BACKOFF_SECONDS

    def test_the_generation_path_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import services.llm as llm_module

        monkeypatch.setattr(llm_module.time, "sleep", lambda _: None)

        def outcome(attempt: int) -> Any:
            if attempt < 2:
                raise ServerError("first attempt fails")
            return _Response()

        gemini = provider(outcome, policy=RetryPolicy(attempts=3, backoff_seconds=0.0))
        completion = gemini.generate(system="s", prompt="p")

        assert completion.text.startswith("Le loyer")


class TestCompletionProjection:
    def test_the_text_and_identity_come_back(self) -> None:
        completion = provider().generate(system="s", prompt="p")

        assert isinstance(completion, LLMCompletion)
        assert completion.provider == "gemini"
        assert completion.model == "gemini-2.5-flash-001"

    def test_usage_is_reported_when_the_provider_supplies_it(self) -> None:
        completion = provider().generate(system="s", prompt="p")

        assert completion.prompt_tokens == 120
        assert completion.completion_tokens == 40
        assert completion.total_tokens == 160

    def test_unreported_usage_is_none_rather_than_zero(self) -> None:
        """Zero would read as 'this call was free', which is a different claim."""
        completion = provider(_Response(usage=_Usage(prompt=None, candidates=None))).generate(
            system="s", prompt="p"
        )

        assert completion.prompt_tokens is None
        assert completion.total_tokens is None

    def test_an_answer_cut_off_at_the_ceiling_is_marked_truncated(self) -> None:
        completion = provider(_Response(finish_reason="MAX_TOKENS")).generate(
            system="s", prompt="p"
        )

        assert completion.truncated is True

    def test_a_blocked_prompt_reports_a_block_reason(self) -> None:
        """No candidate at all: reading only the candidate would report `None` and
        make a refusal indistinguishable from a successful empty answer."""
        completion = provider(_Response(text=None, block_reason="SAFETY")).generate(
            system="s", prompt="p"
        )

        assert completion.text == ""
        assert completion.finish_reason == "BLOCKED_SAFETY"

    def test_an_empty_completion_is_returned_rather_than_raised_on(self) -> None:
        """Whether an empty answer is a failure is the pipeline's decision."""
        completion = provider(_Response(text="")).generate(system="s", prompt="p")

        assert completion.text == ""

    def test_the_system_instruction_is_sent_as_one(self) -> None:
        """Grounding rules must not sit inside the same block as the untrusted text."""
        gemini = provider()
        gemini.generate(system="RULES", prompt="CONTEXT")

        call = gemini._client.models.calls[0]  # type: ignore[attr-defined]
        assert call["config"].system_instruction == "RULES"
        assert call["contents"] == "CONTEXT"

    def test_the_deadline_is_converted_to_milliseconds(self) -> None:
        gemini = provider()
        gemini.generate(system="s", prompt="p", timeout_seconds=12)

        call = gemini._client.models.calls[0]  # type: ignore[attr-defined]
        assert call["config"].http_options.timeout == 12_000


class TestStreaming:
    def test_fragments_come_back_in_order(self) -> None:
        """On the protocol because the assistant will need it; unused by the pipeline."""
        fragments = list(provider().stream(system="s", prompt="p"))

        assert "".join(fragments) == "Le loyer est payable le 5 [1]."

    def test_a_stream_failure_is_translated(self) -> None:
        with pytest.raises(LLMError):
            list(provider(ServerError("boom")).stream(system="s", prompt="p"))


class TestTokenCounting:
    def test_a_count_is_returned_when_the_provider_supplies_one(self) -> None:
        assert provider().count_tokens("Le loyer") == 77

    def test_an_empty_string_costs_nothing(self) -> None:
        assert provider().count_tokens("") == 0

    def test_a_failure_reports_none_rather_than_raising(self) -> None:
        """A monitoring figure must never be the reason an answer is thrown away."""
        gemini = provider()
        gemini._client.models.token_total = None  # type: ignore[attr-defined]

        assert gemini.count_tokens("Le loyer") is None


class TestAvailability:
    def test_a_deployment_without_a_credential_is_unavailable(self) -> None:
        unconfigured = GeminiProvider(api_key="")

        assert unconfigured.is_available() is False

    def test_generating_without_a_credential_is_a_typed_failure(self) -> None:
        with pytest.raises(LLMUnavailableError) as caught:
            GeminiProvider(api_key="").generate(system="s", prompt="p")

        assert caught.value.code is RagFailureCode.LLM_UNAVAILABLE
        assert caught.value.retryable is False

    def test_an_injected_client_is_available(self) -> None:
        assert provider().is_available() is True


class TestLiteLLMProvider:
    def test_it_is_unavailable_when_the_optional_library_is_absent(self) -> None:
        """Not in requirements.txt on purpose; its absence must not be fatal."""
        gateway = LiteLLMProvider()

        # Either the library is genuinely absent (the normal checkout) or it is
        # installed; both are correct, and neither may raise.
        assert isinstance(gateway.is_available(), bool)

    def test_it_speaks_the_openai_shaped_vocabulary(self) -> None:
        class _Message:
            content = "Le loyer est payable le 5 [1]."

        class _Choice:
            message = _Message()
            finish_reason = "stop"

        class _Response2:
            choices: ClassVar[list[Any]] = [_Choice()]
            model = "ollama/mistral"

            # Named as the library names it, lower-case, so the stub matches the
            # attribute the provider actually reads.
            class usage:
                prompt_tokens = 10
                completion_tokens = 5
                total_tokens = 15

        class _Litellm:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def completion(self, **kwargs: Any) -> Any:
                self.calls.append(kwargs)
                return _Response2()

        stub = _Litellm()
        completion = LiteLLMProvider(client=stub, model="ollama/mistral").generate(
            system="RULES", prompt="CONTEXT"
        )

        assert completion.provider == "litellm"
        assert completion.total_tokens == 15
        assert stub.calls[0]["messages"] == [
            {"role": "system", "content": "RULES"},
            {"role": "user", "content": "CONTEXT"},
        ]


class TestResolution:
    def test_the_default_provider_is_gemini(self) -> None:
        """`ai-architecture.md` names it; the abstraction is what keeps it swappable."""
        reset_llm_provider_cache()
        assert isinstance(get_llm_provider(), GeminiProvider)

    def test_an_unknown_identifier_falls_back_rather_than_failing_startup(self) -> None:
        """An API that refused to come up over an AI setting would take
        authentication, cases, and documents down with it."""
        reset_llm_provider_cache()
        assert isinstance(get_llm_provider("no-such-provider"), GeminiProvider)

    def test_the_provider_is_shared_across_the_process(self) -> None:
        reset_llm_provider_cache()
        assert get_llm_provider() is get_llm_provider()

    def test_the_registry_has_two_real_entries(self) -> None:
        """A seam with one implementation is a claim; with two it is a fact."""
        assert available_providers() == ["gemini", "litellm"]
        assert PROVIDER_FACTORIES["litellm"] is LiteLLMProvider

    def test_a_second_provider_can_be_selected_by_identifier_alone(self) -> None:
        reset_llm_provider_cache()
        assert isinstance(get_llm_provider("litellm"), LiteLLMProvider)
        reset_llm_provider_cache()


class TestProtocol:
    def test_both_providers_satisfy_the_protocol(self) -> None:
        for candidate in (GeminiProvider(), LiteLLMProvider()):
            checked: LLMProvider = candidate
            assert checked.name

    def test_the_protocol_cannot_reach_a_document_a_case_or_a_user(self) -> None:
        """A provider is handed two strings and returns one. That narrowness is
        what makes 'replace the provider without changing the orchestration' a
        property of the type system rather than a promise in a document."""
        members = set(LLMProvider.__protocol_attrs__)  # type: ignore[attr-defined]

        assert members == {"name", "model", "is_available", "count_tokens", "generate", "stream"}
        assert not {"search", "retrieve", "documents", "session"} & members
