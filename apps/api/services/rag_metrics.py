"""RAG pipeline observability.

``12-rag-pipeline.md`` requires five figures: **response latency, retrieval
latency, token usage (when available), successful requests, and failed
requests**. This module records them, plus the one the spec's own
"Hallucination Prevention" section makes the most important number on the page —
how often the pipeline answered *"I could not find supporting information"*
rather than answering.

**Why there is no table for this, and no migration.** The same reasoning
:mod:`services.search_metrics` records, and it applies more strongly here: a
pipeline run is not a persisted entity with a lifecycle anyone polls, a row per
question would be write amplification derived from the user's question, and
``architecture.md`` lists no such table under PostgreSQL. Conversations *are*
persisted — by the AI Assistant, which is a different feature with a different
spec, and which is where a durable record of a question belongs.

So the figures are accumulated **in the process**, behind a protocol. The limits
are real and are stated rather than hidden: counters reset when the API restarts,
and each instance counts only its own traffic. With one API process that is the
whole platform; with several, the aggregate needs a shared backend — which is
precisely why :class:`RagMetricsRecorder` is a protocol and
:class:`InMemoryRagMetrics` is only one implementation.

**No question text, no passage, and no answer is recorded here**, by
construction: the recorder is handed durations, counts, and a failure code, and
there is nowhere for any of the three to go. That is the same structural
guarantee :class:`~services.search_metrics.SearchMetricsRecorder` makes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from core.rag import RagFailureCode


@dataclass(frozen=True, slots=True)
class RagMetricsSnapshot:
    """The counters at one instant, as the monitoring endpoint reports them.

    A frozen snapshot rather than a live view of the recorder: reading a dozen
    fields off a mutable accumulator while another thread updates it would
    produce a report whose numbers do not add up, and a monitoring page whose
    figures contradict each other is worse than one that is a second stale.
    """

    #: When this process started counting — the window the figures cover, and the
    #: honest way to say "these reset on restart" without a paragraph.
    since: datetime

    total_requests: int
    successful_requests: int
    failed_requests: int

    #: Successful runs that produced a grounded answer with at least one source.
    grounded_answers: int
    #: Successful runs that reported insufficient evidence — either because
    #: retrieval found nothing, or because the model said the context did not
    #: support an answer. **A success, not a failure**: the pipeline did exactly
    #: what the spec's "No Evidence Handling" section requires.
    insufficient_evidence: int

    #: Mean wall-clock duration of **successful** runs, end to end. Failures are
    #: excluded for the same reason the search average excludes them: a run that
    #: failed because the provider was unreachable timed out against a dead
    #: socket and answers a different question than "how fast is the pipeline".
    average_latency_ms: float | None
    #: Mean time spent in retrieval, across successful runs. Reported separately
    #: because it is the half the platform controls: a slow pipeline with fast
    #: retrieval is the provider's latency, and the two call for opposite fixes.
    average_retrieval_ms: float | None
    #: Mean time spent inside the provider, across runs that actually called one.
    #: Counted over its own denominator, because a no-evidence run never calls
    #: the model and averaging its zero in would make generation look faster the
    #: less the platform could answer.
    average_generation_ms: float | None

    #: Passages retrieved and citations attached, across successful runs.
    total_passages: int
    total_citations: int

    #: Token usage, when the provider reports it. ``None`` throughout when it
    #: never has: ``0`` would read as "this platform's answers are free".
    total_prompt_tokens: int | None
    total_completion_tokens: int | None
    #: Runs whose provider reported usage — the denominator that makes the totals
    #: interpretable when only some providers report.
    metered_requests: int

    failures_by_code: dict[str, int]

    @property
    def success_rate(self) -> float:
        """Share of requests that produced an answer, as a percentage.

        ``0.0`` when nothing has been asked — there is nothing to be successful
        at yet. Same shape and reasoning as
        :attr:`~services.search_metrics.SearchMetricsSnapshot.success_rate`.
        """
        if self.total_requests <= 0:
            return 0.0
        return round(self.successful_requests / self.total_requests * 100, 2)

    @property
    def failure_rate(self) -> float:
        """Share of requests that failed.

        Derived as the complement of the success rate rather than computed
        independently, so the two always sum to 100 — two separate roundings
        would not.
        """
        if self.total_requests <= 0:
            return 0.0
        return round(100.0 - self.success_rate, 2)

    @property
    def grounding_rate(self) -> float:
        """Share of *successful* runs that answered from retrieved evidence.

        Over successful runs rather than over all requests, deliberately: a
        provider outage would otherwise depress this number, and the question it
        answers — "is the corpus able to support what people ask?" — has nothing
        to do with whether the provider was up.
        """
        if self.successful_requests <= 0:
            return 0.0
        return round(self.grounded_answers / self.successful_requests * 100, 2)

    @property
    def average_citations(self) -> float | None:
        """Mean citations attached per grounded answer.

        ``None`` rather than ``0`` when nothing has been grounded: an average
        over no answers is undefined, while ``0`` would read as "every answer is
        uncited", which is a very different and much more alarming situation.
        """
        if self.grounded_answers <= 0:
            return None
        return round(self.total_citations / self.grounded_answers, 2)

    @property
    def average_passages(self) -> float | None:
        """Mean passages retrieved per successful run."""
        if self.successful_requests <= 0:
            return None
        return round(self.total_passages / self.successful_requests, 2)

    @property
    def average_total_tokens(self) -> float | None:
        """Mean tokens per metered run, prompt plus completion."""
        if self.metered_requests <= 0:
            return None
        prompt = self.total_prompt_tokens or 0
        completion = self.total_completion_tokens or 0
        return round((prompt + completion) / self.metered_requests, 2)


class RagMetricsRecorder(Protocol):
    """What the RAG service requires of a metrics backend.

    Three members, and none of them accepts a question, a passage, an answer, a
    user, or a case. A recorder that *cannot be handed* the text cannot leak it —
    the same structural argument :class:`~services.timeline.TimelineRecorder`
    makes about publishers not reaching the read side.
    """

    def record_success(
        self,
        *,
        duration_ms: float,
        retrieval_ms: float,
        generation_ms: float | None,
        passage_count: int,
        citation_count: int,
        grounded: bool,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        """Record one run that produced an answer."""
        ...

    def record_failure(self, *, duration_ms: float, code: RagFailureCode) -> None:
        """Record one run that could not be completed."""
        ...

    def snapshot(self) -> RagMetricsSnapshot:
        """Read the counters."""
        ...


class InMemoryRagMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a *snapshot* has to be internally
    consistent: a dozen separately-updated counters read without one can report
    more grounded answers than successes. The critical sections are a handful of
    additions, so contention is not a consideration on an endpoint whose real
    cost is a network round trip to a language model.

    Sums are accumulated rather than storing every observation: a running total
    and a count give the same mean in constant memory, and a metrics recorder
    that grows with traffic is a leak with a chart on it. The trade-off is that
    percentiles are unavailable — the right one here, because the spec asks for
    averages and a percentile backend is exactly what a future implementation of
    this protocol would add.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._total = 0
        self._successful = 0
        self._failed = 0
        self._grounded = 0
        self._insufficient = 0
        self._latency_sum = 0.0
        self._retrieval_sum = 0.0
        self._generation_sum = 0.0
        #: Counted separately from `_successful`: a run that found no evidence
        #: never calls the provider, and dividing the generation sum by the
        #: success count would make the model look faster the less it was used.
        self._generated = 0
        self._passages = 0
        self._citations = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._metered = 0
        self._failures: dict[str, int] = {}

    def record_success(
        self,
        *,
        duration_ms: float,
        retrieval_ms: float,
        generation_ms: float | None,
        passage_count: int,
        citation_count: int,
        grounded: bool,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        """Record one run that produced an answer, grounded or not.

        A run that reported insufficient evidence is a **success**: the pipeline
        retrieved, found nothing that supports an answer, and said so, which is
        precisely what the spec requires of it. Counting it as a failure would
        make the failure rate a measure of the corpus rather than of the
        platform, and would hide a genuine outage behind it.
        """
        with self._lock:
            self._total += 1
            self._successful += 1
            self._latency_sum += max(0.0, duration_ms)
            self._retrieval_sum += max(0.0, retrieval_ms)
            self._passages += max(0, passage_count)

            if grounded:
                self._grounded += 1
                self._citations += max(0, citation_count)
            else:
                self._insufficient += 1

            if generation_ms is not None:
                self._generation_sum += max(0.0, generation_ms)
                self._generated += 1

            if prompt_tokens is not None or completion_tokens is not None:
                self._prompt_tokens += max(0, prompt_tokens or 0)
                self._completion_tokens += max(0, completion_tokens or 0)
                self._metered += 1

    def record_failure(self, *, duration_ms: float, code: RagFailureCode) -> None:
        """Record one run that could not be completed, and why.

        The duration is deliberately **not** added to the latency sum: a failure
        is usually a timeout against an unresponsive provider, and folding that
        into the average would make the platform look slow when what it actually
        is, is blocked. It is still passed in, because a future recorder may well
        want to chart time-to-failure separately.
        """
        with self._lock:
            self._total += 1
            self._failed += 1
            self._failures[code.value] = self._failures.get(code.value, 0) + 1

    def snapshot(self) -> RagMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return RagMetricsSnapshot(
                since=self._since,
                total_requests=self._total,
                successful_requests=self._successful,
                failed_requests=self._failed,
                grounded_answers=self._grounded,
                insufficient_evidence=self._insufficient,
                average_latency_ms=(
                    round(self._latency_sum / self._successful, 2)
                    if self._successful > 0
                    else None
                ),
                average_retrieval_ms=(
                    round(self._retrieval_sum / self._successful, 2)
                    if self._successful > 0
                    else None
                ),
                average_generation_ms=(
                    round(self._generation_sum / self._generated, 2)
                    if self._generated > 0
                    else None
                ),
                total_passages=self._passages,
                total_citations=self._citations,
                total_prompt_tokens=self._prompt_tokens if self._metered > 0 else None,
                total_completion_tokens=self._completion_tokens if self._metered > 0 else None,
                metered_requests=self._metered,
                failures_by_code=dict(self._failures),
            )

    def reset(self) -> None:
        """Discard every counter.

        For tests, and for an operator who wants a fresh window. Not called by
        the application — the counters are the process's history, and clearing
        them on a schedule would make the ``since`` timestamp a lie.
        """
        with self._lock:
            self._since = datetime.now(UTC)
            self._total = 0
            self._successful = 0
            self._failed = 0
            self._grounded = 0
            self._insufficient = 0
            self._latency_sum = 0.0
            self._retrieval_sum = 0.0
            self._generation_sum = 0.0
            self._generated = 0
            self._passages = 0
            self._citations = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._metered = 0
            self._failures.clear()


class NullRagMetrics:
    """A recorder that counts nothing.

    The default for a service constructed without observability — a script, or a
    unit test that is not about metrics. Same role, and same reasoning, as
    :class:`~services.search_metrics.NullSearchMetrics`: recording code stays a
    plain call with no ``if self._metrics`` guard at every site.
    """

    def record_success(
        self,
        *,
        duration_ms: float,
        retrieval_ms: float,
        generation_ms: float | None,
        passage_count: int,
        citation_count: int,
        grounded: bool,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        """Discard the observation."""

    def record_failure(self, *, duration_ms: float, code: RagFailureCode) -> None:
        """Discard the observation."""

    def snapshot(self) -> RagMetricsSnapshot:
        """Report an empty window."""
        return RagMetricsSnapshot(
            since=datetime.now(UTC),
            total_requests=0,
            successful_requests=0,
            failed_requests=0,
            grounded_answers=0,
            insufficient_evidence=0,
            average_latency_ms=None,
            average_retrieval_ms=None,
            average_generation_ms=None,
            total_passages=0,
            total_citations=0,
            total_prompt_tokens=None,
            total_completion_tokens=None,
            metered_requests=0,
            failures_by_code={},
        )


#: The one recorder the process shares.
#:
#: Module-level rather than per request, because per-request counters would count
#: to one and reset — the metric is a property of the *process*, so the object
#: holding it has to be too. The same reasoning that makes the embedder, the
#: prompt library, the provider, and the job queues process-wide.
_shared = InMemoryRagMetrics()


def get_rag_metrics() -> RagMetricsRecorder:
    """Return the process-wide RAG metrics recorder."""
    return _shared


def reset_rag_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "InMemoryRagMetrics",
    "NullRagMetrics",
    "RagMetricsRecorder",
    "RagMetricsSnapshot",
    "get_rag_metrics",
    "reset_rag_metrics",
]
