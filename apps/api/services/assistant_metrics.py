"""AI Legal Assistant observability.

``13-ai-legal-assistant.md`` requires six figures: **active conversations,
average response time, average conversation length, successful requests, failed
requests, and user feedback statistics**.

They do not all come from the same place, and that is the design rather than an
accident:

* **conversation counts, conversation length, and feedback statistics are
  queried from the database.** They are properties of persisted rows, so counting
  them in a process would produce a figure that resets on restart *and* is wrong
  — a conversation that exists does not stop existing because the API was
  redeployed. :class:`~repositories.conversation.ConversationRepository` answers
  those, and the service asks it.
* **request counts, latency, and failures accumulate in the process**, behind
  the protocol below, exactly as the search and RAG recorders do. A message is an
  *event*, not a row anyone polls, and a table of them would be write
  amplification derived from a user's question.

The limits of the second half are real and stated rather than hidden: those
counters reset when the API restarts and each instance counts only its own
traffic, which is what ``since`` reports. That is precisely why
:class:`AssistantMetricsRecorder` is a protocol — a Redis-backed implementation
is one class plus one line in :mod:`api.deps`.

**No question, answer, title, citation, or user identity is recorded here**, by
construction: the recorder is handed durations, flags, and a failure code, and
there is nowhere for any of them to go.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from core.rag import RagFailureCode


@dataclass(frozen=True, slots=True)
class AssistantMetricsSnapshot:
    """The in-process counters at one instant.

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

    #: Of the successful ones, how many were delivered as a stream rather than
    #: whole. Worth its own counter because streaming is the one part of this
    #: feature that silently degrades: a provider that has stopped supporting it
    #: falls back to a complete answer, which is correct, works, and is invisible
    #: from every other number on the page.
    streamed_requests: int

    #: Successful answers built from retrieved sources, and successful answers
    #: reporting that the documents do not support one. The second is **not a
    #: failure** — it is the pipeline's "No Evidence Handling" working.
    grounded_answers: int
    insufficient_evidence: int

    #: Mean wall-clock time to an answer, across **successful** messages.
    #: Failures are excluded for the reason the RAG recorder excludes them: a
    #: request that timed out against an unresponsive provider would make a
    #: platform that is *blocked* look merely slow.
    average_response_ms: float | None

    failures_by_code: dict[str, int]

    @property
    def success_rate(self) -> float:
        """Share of messages that produced an answer, as a percentage.

        ``0.0`` when nothing has been asked — there is nothing to be successful
        at yet.
        """
        if self.total_requests <= 0:
            return 0.0
        return round(self.successful_requests / self.total_requests * 100, 2)

    @property
    def failure_rate(self) -> float:
        """Share of messages that failed.

        Derived as the complement of the success rate rather than computed
        independently, so the two always sum to 100 — two separate roundings
        would not.
        """
        if self.total_requests <= 0:
            return 0.0
        return round(100.0 - self.success_rate, 2)

    @property
    def grounding_rate(self) -> float:
        """Share of *successful* answers that were built from retrieved evidence.

        Over successful messages rather than over all of them, deliberately: a
        provider outage would otherwise depress this number, and the question it
        answers — "does the corpus cover what people ask?" — has nothing to do
        with whether the provider was up.
        """
        if self.successful_requests <= 0:
            return 0.0
        return round(self.grounded_answers / self.successful_requests * 100, 2)


class AssistantMetricsRecorder(Protocol):
    """What the assistant service requires of a metrics backend.

    Three members, and none of them accepts a message, a title, a citation, a
    user, or a case. A recorder that *cannot be handed* the text cannot leak it —
    the same structural argument :class:`~services.rag_metrics.RagMetricsRecorder`
    makes.
    """

    def record_success(
        self,
        *,
        duration_ms: float,
        grounded: bool,
        streamed: bool,
    ) -> None:
        """Record one message that produced an answer."""
        ...

    def record_failure(self, *, duration_ms: float, code: RagFailureCode) -> None:
        """Record one message that could not be answered."""
        ...

    def snapshot(self) -> AssistantMetricsSnapshot:
        """Read the counters."""
        ...


class InMemoryAssistantMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a *snapshot* has to be internally
    consistent: separately-updated counters read without one can report more
    grounded answers than successes. The critical sections are a handful of
    additions, so contention is not a consideration on an endpoint whose real
    cost is a network round trip to a language model.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._total = 0
        self._successful = 0
        self._failed = 0
        self._streamed = 0
        self._grounded = 0
        self._insufficient = 0
        self._latency_sum = 0.0
        self._failures: dict[str, int] = {}

    def record_success(self, *, duration_ms: float, grounded: bool, streamed: bool) -> None:
        """Record one message that produced an answer, grounded or not.

        A message whose answer reported insufficient evidence is a **success**:
        the assistant retrieved through the pipeline, found nothing that supports
        an answer, and said so. Counting it as a failure would make the failure
        rate a measure of the corpus rather than of the platform, and would hide
        a genuine outage behind it.
        """
        with self._lock:
            self._total += 1
            self._successful += 1
            self._latency_sum += max(0.0, duration_ms)

            if grounded:
                self._grounded += 1
            else:
                self._insufficient += 1

            if streamed:
                self._streamed += 1

    def record_failure(self, *, duration_ms: float, code: RagFailureCode) -> None:
        """Record one message that could not be answered, and why.

        The duration is deliberately **not** added to the latency sum, for the
        reason :class:`~services.rag_metrics.InMemoryRagMetrics` records: a
        failure is usually a timeout against an unresponsive provider, and
        folding that into the average would make the platform look slow when what
        it actually is, is blocked.
        """
        with self._lock:
            self._total += 1
            self._failed += 1
            self._failures[code.value] = self._failures.get(code.value, 0) + 1

    def snapshot(self) -> AssistantMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return AssistantMetricsSnapshot(
                since=self._since,
                total_requests=self._total,
                successful_requests=self._successful,
                failed_requests=self._failed,
                streamed_requests=self._streamed,
                grounded_answers=self._grounded,
                insufficient_evidence=self._insufficient,
                average_response_ms=(
                    round(self._latency_sum / self._successful, 2)
                    if self._successful > 0
                    else None
                ),
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
            self._streamed = 0
            self._grounded = 0
            self._insufficient = 0
            self._latency_sum = 0.0
            self._failures.clear()


class NullAssistantMetrics:
    """A recorder that counts nothing.

    The default for a service constructed without observability — a script, or a
    unit test that is not about metrics. Same role, and same reasoning, as
    :class:`~services.rag_metrics.NullRagMetrics`: recording code stays a plain
    call with no ``if self._metrics`` guard at every site.
    """

    def record_success(self, *, duration_ms: float, grounded: bool, streamed: bool) -> None:
        """Discard the observation."""

    def record_failure(self, *, duration_ms: float, code: RagFailureCode) -> None:
        """Discard the observation."""

    def snapshot(self) -> AssistantMetricsSnapshot:
        """Report an empty window."""
        return AssistantMetricsSnapshot(
            since=datetime.now(UTC),
            total_requests=0,
            successful_requests=0,
            failed_requests=0,
            streamed_requests=0,
            grounded_answers=0,
            insufficient_evidence=0,
            average_response_ms=None,
            failures_by_code={},
        )


#: The one recorder the process shares.
#:
#: Module-level rather than per request, because per-request counters would count
#: to one and reset — the metric is a property of the *process*, so the object
#: holding it has to be too.
_shared = InMemoryAssistantMetrics()


def get_assistant_metrics() -> AssistantMetricsRecorder:
    """Return the process-wide assistant metrics recorder."""
    return _shared


def reset_assistant_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "AssistantMetricsRecorder",
    "AssistantMetricsSnapshot",
    "InMemoryAssistantMetrics",
    "NullAssistantMetrics",
    "get_assistant_metrics",
    "reset_assistant_metrics",
]
