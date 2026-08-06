"""RAG pipeline request and response schemas.

Two responsibilities, the same two the case, document, OCR, indexing, and search
schemas carry:

* **Validation** — the question's length, the number of passages retrieved, the
  similarity floor, the answer language, and every metadata filter are enforced
  here, so the route stays thin and every rejection comes back in the standard
  envelope with a per-field message.
* **Serialization** — an answer is returned with its citations, its provenance
  (which prompt, which model, which provider), its timings, and the derived
  values a client would otherwise compute and get subtly wrong.

**The filters are :class:`~schemas.search.SearchFilterInput`, reused verbatim.**
``12-rag-pipeline.md`` requires the pipeline to use the existing Semantic Search
service and to reuse existing abstractions; a parallel filter model here would be
a second vocabulary to keep in step with the first, and the first change that
touched only one of them would produce a filter the pipeline accepts and
retrieval ignores.

**The request is a POST body, not a query string**, for exactly the reason
``11-semantic-search.md`` gives for search: a URL is written to the reverse
proxy's access log, the browser's history, and the ``Referer`` header of anything
the page loads next, and a question about a client's matter is at least as
revealing as the passage it retrieves.

**No schema here carries a conversation, a message, a session, or a turn.**
``ai-architecture.md`` states that the RAG pipeline must never manage
conversations; that boundary is structural, and this file is where it would first
have been broken.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from core.config import settings
from core.rag import (
    MIN_QUESTION_LENGTH,
    SUPPORTED_ANSWER_LANGUAGES,
    normalize_question,
)
from schemas.search import SearchFilterInput

__all__ = [
    "RagAnswerResponse",
    "RagCitationRead",
    "RagMetricsRead",
    "RagRequest",
]


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #


class RagRequest(BaseModel):
    """A question to be answered from indexed legal documents.

    The question is normalised on the way in (NFC, whitespace collapsed, control
    characters dropped) by the *same* function that normalises a search query, so
    the text that reaches the embedding model is byte-identical to the form the
    indexed passages were normalised into. An Arabic or French question typed
    with decomposed characters must embed to the same vector as the composed
    form, or it would miss the very page that answers it.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=1,
        max_length=settings.RAG_QUESTION_MAX_LENGTH,
        description=(
            "The question, in natural language. Arabic, French, and English are supported by the "
            "same retrieval model, so a question in one language finds passages written in the "
            "others."
        ),
    )
    language: str | None = Field(
        default=None,
        description=(
            "ISO 639-1 code the answer must be written in (`ar`, `fr`, or `en`). Omitted detects "
            "the language of the question, which is the right default for a user typing in their "
            "own language and the wrong one for a French interface asking about an Arabic filing."
        ),
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=settings.SEARCH_MAX_LIMIT,
        description=(
            f"Passages to ground the answer in (max {settings.SEARCH_MAX_LIMIT}). Omitted uses the "
            f"deployment's default. More is not better: every passage is paid for twice, in the "
            f"context window and in the model's attention."
        ),
    )
    min_score: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description=(
            "Similarity floor for retrieved passages, between -1 and 1. Omitted uses the "
            "deployment's default. Raising it trades recall for grounding: fewer passages, each "
            "more certainly relevant."
        ),
    )
    filters: SearchFilterInput = Field(
        default_factory=SearchFilterInput,
        description=(
            "Which documents may be retrieved from. The same filters semantic search accepts, and "
            "with the same guarantee: they narrow the caller's authorized scope and can never "
            "widen it."
        ),
    )

    @field_validator("question")
    @classmethod
    def _normalize(cls, value: str) -> str:
        """Normalise the question and reject one that carries nothing to answer.

        Rejected here rather than answered, because retrieving on a single
        character returns a page of arbitrary passages — and the model then
        writes a fluent, confident paragraph out of them, which is exactly the
        fabrication this pipeline exists to prevent.
        """
        normalized = normalize_question(value)
        if len(normalized) < MIN_QUESTION_LENGTH or not any(
            character.isalnum() for character in normalized
        ):
            raise ValueError(f"Enter a question of at least {MIN_QUESTION_LENGTH} characters.")
        return normalized

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str | None) -> str | None:
        """Accept only a language the platform actually answers in.

        Rejected rather than silently ignored: a caller who asked for German and
        received French would have no way to tell that the request was
        understood and overruled rather than honoured.
        """
        if value is None:
            return None

        wanted = value.strip().lower()
        if not wanted:
            return None
        if wanted not in SUPPORTED_ANSWER_LANGUAGES:
            supported = ", ".join(sorted(SUPPORTED_ANSWER_LANGUAGES))
            raise ValueError(f"Answers are available in: {supported}.")
        return wanted


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #


class RagCitationRead(BaseModel):
    """One source an answer was grounded in.

    Exactly the four references ``12-rag-pipeline.md`` names — **document,
    document version, page, case** — plus the marker the answer cites it by, the
    excerpt that was actually placed in the prompt, and whether the answer
    referenced it.

    **And deliberately nothing else.** The chunk number, the point identifier,
    the embedding model, and the vector are all available at this layer and are
    all withheld: the spec says not to expose internal identifiers unnecessarily,
    and none of the four means anything to a lawyer. The page number is what a
    legal citation points at, and it is here.

    ``document_id`` and ``case_id`` *are* exposed, and that is not a contradiction
    of the same rule: they are the identifiers the client needs to open the
    document the citation refers to, they are random UUIDs rather than guessable
    sequence numbers, and the caller is by construction already entitled to both —
    a passage can only be retrieved from a document they could already read.
    """

    marker: int = Field(
        description=(
            "The number the answer cites this source by, as `[1]`. 1-based, assigned in relevance "
            "order before generation, so the markers in the prose resolve here without the client "
            "having to re-sort anything."
        )
    )
    document_id: uuid.UUID = Field(description="Document this passage was taken from.")
    document_name: str = Field(description="Name the document is known by, as uploaded.")
    document_version: int = Field(
        description=(
            "Version of that document. Each version keeps its own index, so a citation is always "
            "attributed to the exact revision the text appears in."
        )
    )
    page_number: int = Field(
        description="1-based page the passage appears on — what a legal citation points at."
    )
    case_id: uuid.UUID = Field(description="Case the document is filed under.")
    score: float = Field(
        description=(
            "Cosine similarity between the question and this passage, higher is closer. Comparable "
            "across questions, because every vector is unit length."
        )
    )
    excerpt: str = Field(
        description=(
            "The passage exactly as it was placed in the prompt — not as it is stored. When the "
            "context budget clipped it, this is the clipped text, because the honest evidence for "
            "an answer is what the model actually read."
        )
    )
    excerpt_truncated: bool = Field(
        default=False,
        description="Whether this passage was shortened to fit the context budget.",
    )
    referenced: bool = Field(
        default=False,
        description=(
            "Whether the answer actually cited this source. Sources the model was given but did "
            "not use are still returned, so a client can show what the answer was grounded in even "
            "when the model omitted a marker — but they are marked, so the citation list stays "
            "honest about which references the prose rests on."
        ),
    )


class RagAnswerResponse(BaseModel):
    """One grounded answer, its citations, and how it was produced.

    Carries the provenance an evaluation needs — which prompt, which version,
    which provider, which model — because ``ai-architecture.md`` requires prompts
    to be versioned and `architecture.md` names Ragas and DeepEval for AI
    quality evaluation, and neither can compare two runs that do not say what
    produced them.
    """

    question: str = Field(description="The normalised question that was actually asked, echoed back.")
    answer: str = Field(
        description=(
            "The grounded answer, in the requested or detected language. When no supporting "
            "evidence was found this is the platform's own 'nothing found' message rather than "
            "anything a model wrote."
        )
    )
    language: str = Field(description="ISO 639-1 code the answer is written in.")

    grounded: bool = Field(
        description=(
            "Whether the answer was produced from retrieved passages. False means the pipeline "
            "declined to answer — never that it answered from the model's own knowledge."
        )
    )
    insufficient_evidence: bool = Field(
        description=(
            "Whether the pipeline reported that the documents do not support an answer. A "
            "successful outcome, not an error: either retrieval found nothing, or the model was "
            "given passages and judged them insufficient."
        )
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Whether the answer hit the model's output ceiling and stops mid-thought. Reported "
            "rather than hidden: a legal answer that ends early must not be read as a complete one."
        ),
    )

    citations: list[RagCitationRead] = Field(
        default_factory=list,
        description=(
            "Sources the answer was grounded in, in relevance order. Empty whenever `grounded` is "
            "false — an answer reading 'no supporting document was found' beside a list of sources "
            "contradicts itself, and `retrieved_count` still reports what was considered."
        ),
    )

    retrieved_count: int = Field(
        description="Passages semantic search returned for this question."
    )
    context_count: int = Field(
        description=(
            "Passages that fitted the context budget and were placed in the prompt. Lower than "
            "`retrieved_count` means the budget, not retrieval, was the limit."
        )
    )
    context_characters: int = Field(
        description="Characters of retrieved text placed in the prompt."
    )
    context_truncated: bool = Field(
        default=False,
        description="Whether any passage was dropped or shortened to fit the context budget.",
    )

    duration_ms: int = Field(description="Wall-clock time the whole pipeline took.")
    retrieval_ms: int = Field(description="Of which, time spent retrieving passages.")
    generation_ms: int | None = Field(
        default=None,
        description=(
            "Of which, time spent inside the language model. Absent when no model was called, "
            "which is what happens when retrieval found nothing — the pipeline does not spend a "
            "request to be told there is nothing to say."
        ),
    )

    prompt_name: str = Field(description="Prompt template that produced this answer.")
    prompt_version: int = Field(description="Version of that template.")
    provider: str | None = Field(
        default=None, description="Provider that generated the answer, when one was called."
    )
    model: str | None = Field(
        default=None, description="Model that generated the answer, when one was called."
    )
    prompt_tokens: int | None = Field(
        default=None, description="Tokens the prompt occupied, when the provider reports usage."
    )
    completion_tokens: int | None = Field(
        default=None, description="Tokens the answer occupied, when the provider reports usage."
    )
    total_tokens: int | None = Field(
        default=None, description="Total tokens billed, when the provider reports usage."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Sources attached to this answer.",
    )
    @property
    def citation_count(self) -> int:
        """Derived rather than stored, so it cannot disagree with `citations`."""
        return len(self.citations)

    @computed_field(  # type: ignore[prop-decorator]
        description="Sources the answer's prose actually cites, of those attached.",
    )
    @property
    def referenced_count(self) -> int:
        """The honest half of the citation count.

        A grounded answer with citations but a `referenced_count` of zero is a
        model that ignored the citation instruction — visible here rather than
        only in an aggregate, so the reader of one answer can see it too.
        """
        return sum(1 for citation in self.citations if citation.referenced)


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class RagMetricsRead(BaseModel):
    """Platform-wide RAG health, as the spec's "Monitoring" section describes it.

    The five figures it names — **response latency, retrieval latency, token
    usage, successful requests, failed requests** — plus the rates, the failure
    breakdown, the grounding rate, and the configuration behind them.

    ``since`` is not decoration. These counters live in the API process rather
    than in a table (see :mod:`services.rag_metrics` for why), so they reset when
    it restarts and each instance counts only its own traffic. Reporting the
    start of the window is what keeps that limitation visible instead of making
    the numbers quietly mean less than they appear to.
    """

    since: datetime = Field(
        description=(
            "When this process started counting. The figures cover traffic since then, on this "
            "instance only."
        )
    )

    total_requests: int = Field(description="Questions attempted in the window.")
    successful_requests: int = Field(
        description=(
            "Questions that produced an answer. A question the documents could not support counts "
            "here: declining to answer is the pipeline working, not failing."
        )
    )
    failed_requests: int = Field(
        description="Questions that could not be completed because a dependency failed."
    )

    success_rate: float = Field(description="Percentage of questions that answered, 0-100.")
    failure_rate: float = Field(description="Percentage that failed. Complements `success_rate`.")

    grounded_answers: int = Field(description="Successful runs that answered from retrieved sources.")
    insufficient_evidence: int = Field(
        description="Successful runs that reported that the documents do not support an answer."
    )
    grounding_rate: float = Field(
        description=(
            "Percentage of *successful* runs that answered from evidence. The number to watch: a "
            "falling rate means the corpus no longer covers what people ask, which is a document "
            "problem rather than an AI one."
        )
    )

    average_latency_ms: float | None = Field(
        default=None,
        description=(
            "Mean end-to-end duration of successful runs. Failures are excluded: a run that timed "
            "out against an unresponsive provider would make the platform look slow when what it "
            "is, is blocked."
        ),
    )
    average_retrieval_ms: float | None = Field(
        default=None,
        description=(
            "Mean time spent retrieving. Reported separately because it is the half the platform "
            "controls — a slow pipeline with fast retrieval is the provider's latency, and the two "
            "call for opposite fixes."
        ),
    )
    average_generation_ms: float | None = Field(
        default=None,
        description=(
            "Mean time spent inside the model, over the runs that called it. A run that found no "
            "evidence never calls one and is excluded, so this does not fall as the platform "
            "answers less."
        ),
    )

    total_passages: int = Field(description="Passages retrieved across every successful run.")
    average_passages: float | None = Field(
        default=None, description="Mean passages retrieved per successful run."
    )
    total_citations: int = Field(description="Citations attached across every grounded answer.")
    average_citations: float | None = Field(
        default=None, description="Mean citations per grounded answer."
    )

    total_prompt_tokens: int | None = Field(
        default=None,
        description=(
            "Prompt tokens across every metered run. Absent when no provider has reported usage — "
            "zero would read as 'this platform's answers are free'."
        ),
    )
    total_completion_tokens: int | None = Field(
        default=None, description="Completion tokens across every metered run."
    )
    metered_requests: int = Field(
        description="Runs whose provider reported token usage — the denominator for the totals."
    )
    average_total_tokens: float | None = Field(
        default=None, description="Mean tokens per metered run, prompt plus completion."
    )

    failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Failed runs grouped by cause. A failure rate says something is wrong; this says what "
            "— a missing credential, an unreachable vector database, and a model timing out read "
            "identically otherwise."
        ),
    )

    provider: str = Field(description="LLM provider this deployment is configured to use.")
    model: str = Field(description="Model that provider is configured to call.")
    llm_available: bool = Field(
        description=(
            "Whether a provider client can be built here — the library is installed and a "
            "credential is configured. It does not prove the credential is accepted; a rejected "
            "key appears under `failures_by_code` instead."
        )
    )

    prompt_name: str = Field(description="Prompt template answers are produced with.")
    prompt_version: int = Field(description="Version of that template.")
    prompt_available: bool = Field(
        description="Whether that template can actually be loaded. False means every question fails."
    )

    retrieval_top_k: int = Field(description="Passages retrieved per question by default.")
    max_context_characters: int = Field(
        description="Retrieved text placed in one prompt, at most, in characters."
    )
    max_citations: int = Field(description="Citations attached to one answer, at most.")
    enabled: bool = Field(
        description="Whether the pipeline is permitted to answer at all on this deployment."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Mean end-to-end latency in seconds, for display.",
    )
    @property
    def average_latency_seconds(self) -> float | None:
        """Formatted once, server-side, so every surface expresses it identically."""
        if self.average_latency_ms is None:
            return None
        return round(self.average_latency_ms / 1000, 3)
