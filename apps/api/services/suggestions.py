"""Suggested follow-up questions.

``13-ai-legal-assistant.md``: *"After a successful response, generate a small
number of suggested follow-up questions"*, relevant to the retrieved context, the
conversation topic, and the legal workflow — and *"Suggestions should never
invent unsupported facts."*

This is the one place the assistant calls a language model, and the boundaries
around that are deliberate:

* it **does not retrieve**. It is handed the answer that was just produced and
  the documents it cited, and it has no search service, no repository, and no
  database session — so there is no path from here to a passage the caller could
  not already read. Everything it sees has already passed through the RAG
  pipeline's authorization;
* it **does not construct a prompt in Python**. The template lives in
  ``apps/api/prompts/assistant/`` and is versioned in its filename, exactly as the
  answer prompt is, and it is rendered through the *same*
  :class:`~services.prompts.PromptLibrary`;
* it **does not call an SDK**. Generation goes through the same
  :class:`~services.llm.LLMProvider` the pipeline uses, so a deployment that
  switches provider switches this too, and every SDK failure is already
  translated at that boundary.

So the spec's *"do not duplicate retrieval, prompt construction, or orchestration
logic"* holds: this adds a **new** prompt for a **new** purpose, and reuses every
abstraction underneath it.

**Failure is never fatal.** A suggestion is a convenience that arrives after the
answer the user actually asked for. Every failure path here returns an empty list
and logs — a timeout, a missing template, a missing credential, an unparseable
reply. An answer that reached the user must never be lost because the platform
could not think of what to ask next.

**Nothing here reaches a log**: not the question, not the answer, not a document
name, and not a suggestion. The same rule the pipeline follows, for the same
reason — all four are a client's matter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import structlog

from core.assistant import parse_suggestions
from core.config import settings
from core.rag import language_name
from schemas.rag import RagCitationRead
from services.llm import LLMError, LLMProvider, get_llm_provider
from services.prompts import PromptError, PromptLibrary, get_prompt_library

logger = structlog.get_logger(__name__)


class FollowUpSuggester(Protocol):
    """What the assistant requires of a follow-up generator.

    Three members, and none of them mentions a conversation, a user, a case, or a
    repository. A suggester is handed one exchange and returns a list of strings —
    which is what makes it substitutable, and what keeps it from becoming a second
    way to reach a document.
    """

    @property
    def name(self) -> str:
        """Stable identifier of the backend ("llm")."""
        ...

    def is_available(self) -> bool:
        """Whether suggestions can actually be produced here, right now."""
        ...

    def suggest(
        self,
        *,
        question: str,
        answer: str,
        citations: Sequence[RagCitationRead],
        language: str,
    ) -> list[str]:
        """Propose the questions this exchange makes worth asking next.

        Returns an empty list rather than raising, always. See the module
        docstring for why a suggestion failure must never surface.
        """
        ...


class LlmFollowUpSuggester:
    """Follow-ups from the configured language model, on a versioned template."""

    #: The identifier recorded for this backend.
    name = "llm"

    def __init__(
        self,
        prompts: PromptLibrary | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self._prompts = prompts or get_prompt_library()
        self._provider = provider or get_llm_provider()

    def is_available(self) -> bool:
        """Whether the switch is on, a provider can be built, and the template loads.

        Probed rather than assumed, so the monitoring view can tell "suggestions
        are turned off" apart from "suggestions are on and silently producing
        nothing" — which look identical from the transcript.
        """
        if not settings.ASSISTANT_SUGGESTIONS_ENABLED:
            return False
        if not self._provider.is_available():
            return False
        try:
            return settings.ASSISTANT_SUGGESTION_PROMPT_VERSION in self._prompts.versions(
                settings.ASSISTANT_SUGGESTION_PROMPT_TEMPLATE
            )
        except PromptError:  # pragma: no cover - defensive
            return False

    def suggest(
        self,
        *,
        question: str,
        answer: str,
        citations: Sequence[RagCitationRead],
        language: str,
    ) -> list[str]:
        """Propose follow-up questions, or return nothing at all.

        **Nothing is proposed for an ungrounded answer**, which is why an empty
        citation list short-circuits before the provider is called. The spec says
        suggestions must never invent unsupported facts, and an answer that found
        no supporting document supports no follow-up either — every question the
        model produced from it would be a guess about material the platform does
        not have. It is also the cheapest correct behaviour: no call is made.

        The **document names are given, the passages are not.** The answer was
        already built from those passages, so a question grounded in the answer is
        grounded in them — and sending the full context twice would double the
        cost of every exchange for a list of three short questions.
        """
        if not settings.ASSISTANT_SUGGESTIONS_ENABLED or settings.ASSISTANT_SUGGESTION_COUNT <= 0:
            return []
        if not citations or not answer.strip():
            return []

        try:
            prompt = self._prompts.render(
                settings.ASSISTANT_SUGGESTION_PROMPT_TEMPLATE,
                version=settings.ASSISTANT_SUGGESTION_PROMPT_VERSION,
                context={
                    "sources": [
                        {
                            "document_name": citation.document_name,
                            "document_version": citation.document_version,
                            "page_number": citation.page_number,
                        }
                        for citation in citations
                    ],
                    "question": question,
                    "answer": answer,
                    "language_name": language_name(language),
                    "suggestion_count": settings.ASSISTANT_SUGGESTION_COUNT,
                    "suggestion_max_length": settings.ASSISTANT_SUGGESTION_MAX_LENGTH,
                },
            )
        except PromptError as exc:
            logger.warning(
                "assistant_suggestions_unavailable",
                reason="prompt_failed",
                template=settings.ASSISTANT_SUGGESTION_PROMPT_TEMPLATE,
                error_type=type(exc).__name__,
            )
            return []

        try:
            completion = self._provider.generate(
                system=prompt.system,
                prompt=prompt.user,
                # Warmer than an answer, and deliberately so: three suggestions
                # at temperature 0 tend to be three rephrasings of one idea,
                # which is the failure mode this feature has to avoid. It is also
                # the one call on the platform where variation costs nothing — a
                # suggestion is a shortcut the user may ignore.
                temperature=min(1.0, settings.LLM_TEMPERATURE + 0.5),
                max_output_tokens=settings.ASSISTANT_SUGGESTION_MAX_OUTPUT_TOKENS,
                timeout_seconds=float(settings.ASSISTANT_SUGGESTION_TIMEOUT_SECONDS),
            )
        except LLMError as exc:
            # The provider has already translated and logged the SDK's failure
            # without quoting it. Nothing else to do: the answer stands.
            logger.info(
                "assistant_suggestions_unavailable",
                reason="llm_failed",
                error_code=exc.code.value,
            )
            return []

        suggestions = parse_suggestions(
            completion.text, exclude=[question], truncated=completion.truncated
        )

        logger.info(
            "assistant_suggestions_generated",
            suggestion_count=len(suggestions),
            # Logged because it is the difference between "the model had nothing
            # to suggest" and "the deployment's output ceiling is too small for
            # the model it is configured with", which look identical in a
            # transcript and need opposite responses.
            truncated=completion.truncated,
            provider=completion.provider,
            model=completion.model,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
        )
        return suggestions


class NullFollowUpSuggester:
    """A suggester that proposes nothing.

    The default for a service constructed without one — a script, or a unit test
    that is not about suggestions — and what a deployment gets when
    ``ASSISTANT_SUGGESTIONS_ENABLED`` is off. Same role, and same reasoning, as
    :class:`~services.assistant_metrics.NullAssistantMetrics`: the calling code
    stays a plain call with no ``if self._suggester`` guard.
    """

    #: The identifier recorded for this backend.
    name = "none"

    def is_available(self) -> bool:
        """Never available, which is the honest report for a backend that suggests nothing."""
        return False

    def suggest(
        self,
        *,
        question: str,
        answer: str,
        citations: Sequence[RagCitationRead],
        language: str,
    ) -> list[str]:
        """Propose nothing."""
        return []


def get_follow_up_suggester() -> FollowUpSuggester:
    """Return the suggester this deployment is configured for.

    Resolved from the switch rather than from a registry of names, because there
    are exactly two behaviours — suggest, or do not — and a ``SUGGESTER`` setting
    naming a class would be configuration with one meaningful value. A second
    *strategy* (a cheaper model, a template per role) becomes a registry here in
    the shape :data:`~services.llm.PROVIDER_FACTORIES` uses.
    """
    if not settings.ASSISTANT_SUGGESTIONS_ENABLED:
        return NullFollowUpSuggester()
    return LlmFollowUpSuggester()


__all__ = [
    "FollowUpSuggester",
    "LlmFollowUpSuggester",
    "NullFollowUpSuggester",
    "get_follow_up_suggester",
]
