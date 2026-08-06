"""Search observability.

``11-semantic-search.md`` requires four figures: **search count, average latency,
average relevance score, and failed searches**. This module records them.

**Why there is no table for this, and no migration.** OCR and Document Indexing
get their metrics from the rows they already write, because each of them *is* a
persisted run with a lifecycle a lawyer polls. A search is not: it is a read that
answers in milliseconds and has nothing to poll. Giving it a table would mean

* writing a row per search — a write amplification on the platform's most
  frequent operation, and one that grows without bound;
* storing something derived from the user's query, which is exactly what the
  spec's logging rule tells us not to persist; and
* an entity `architecture.md` does not list under PostgreSQL, added by a feature
  whose spec asks for metrics rather than for history.

So the figures are accumulated **in the process**, behind a protocol. The limits
of that are real and are stated rather than hidden: counters reset when the API
restarts, and each instance counts only its own traffic. With one API process
that is the whole platform; with several, the aggregate needs a shared backend —
which is precisely why :class:`SearchMetricsRecorder` is a protocol and
:class:`InMemorySearchMetrics` is only one implementation. A Redis-backed
recorder is one class plus one line in :mod:`api.deps`, and Redis is already in
the stack.

**No query text and no passage is recorded here**, by construction: the recorder
is handed a duration, a count, and a score, and there is nowhere for either to
go. That is the same structural guarantee as `IndexingService._fail` holding no
write path to the OCR tables.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from core.search import SearchFailureCode, round_score


@dataclass(frozen=True, slots=True)
class SearchMetricsSnapshot:
    """The counters at one instant, as the monitoring endpoint reports them.

    A frozen snapshot rather than a live view of the recorder: reading five
    fields off a mutable accumulator while another thread updates it would
    produce a report whose numbers do not add up, and a monitoring page whose
    figures contradict each other is worse than one that is a second stale.
    """

    #: When this process started counting. The window the figures cover, and the
    #: honest way to say "these reset on restart" without a paragraph.
    since: datetime

    total_searches: int
    successful_searches: int
    failed_searches: int

    #: Results returned across every successful search — the numerator of
    #: :attr:`average_results`.
    total_results: int

    #: Mean wall-clock duration of **successful** searches. Failures are excluded
    #: for the same reason the indexing average excludes them: a search that
    #: failed because Qdrant was unreachable timed out against a dead socket and
    #: answers a different question than "how fast is retrieval".
    average_latency_ms: float | None
    #: Mean relevance across every result of every successful search. The spec's
    #: "average relevance score" — a platform-level answer to "are the passages
    #: we return actually close to what people ask for".
    average_score: float | None

    failures_by_code: dict[str, int]

    @property
    def success_rate(self) -> float:
        """Share of searches that answered, as a percentage.

        ``0.0`` when nothing has been searched — there is nothing to be
        successful at yet. Same shape and reasoning as
        :func:`~core.ocr.success_rate` and :func:`~core.indexing.success_rate`.
        """
        if self.total_searches <= 0:
            return 0.0
        return round(self.successful_searches / self.total_searches * 100, 2)

    @property
    def failure_rate(self) -> float:
        """Share of searches that failed.

        Derived as the complement of the success rate rather than computed
        independently, so the two always sum to 100 — two separate roundings
        would not.
        """
        if self.total_searches <= 0:
            return 0.0
        return round(100.0 - self.success_rate, 2)

    @property
    def average_results(self) -> float | None:
        """Mean results per successful search.

        ``None`` rather than ``0`` when nothing has succeeded: an average over no
        searches is undefined, while ``0`` would read as "every search returns
        nothing", which is a very different operational situation.
        """
        if self.successful_searches <= 0:
            return None
        return round(self.total_results / self.successful_searches, 2)


class SearchMetricsRecorder(Protocol):
    """What the search service requires of a metrics backend.

    Three members, and none of them accepts a query, a passage, a user, or a
    case. A recorder that *cannot be handed* the query text cannot leak it —
    which is the same structural argument the timeline's ``TimelineRecorder``
    makes about publishers not reaching the read side.
    """

    def record_success(
        self, *, duration_ms: float, result_count: int, average_score: float | None
    ) -> None:
        """Record one search that answered."""
        ...

    def record_failure(self, *, duration_ms: float, code: SearchFailureCode) -> None:
        """Record one search that could not be answered."""
        ...

    def snapshot(self) -> SearchMetricsSnapshot:
        """Read the counters."""
        ...


class InMemorySearchMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a *snapshot* has to be internally
    consistent: five separately-updated counters read without one can report more
    successes than searches. The critical sections are a handful of additions, so
    contention is not a consideration even on the platform's busiest endpoint.

    Sums are accumulated rather than storing every observation: a running total
    and a count give the same mean as a list of samples, in constant memory, and
    a metrics recorder that grows with traffic is a leak with a chart on it. The
    trade-off is that percentiles are not available — which is the right one
    here, because the spec asks for averages and a percentile backend is exactly
    what a future implementation of this protocol would add.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._total = 0
        self._successful = 0
        self._failed = 0
        self._results = 0
        self._latency_sum = 0.0
        self._score_sum = 0.0
        #: Counted separately from `_results`: a search returning nothing
        #: contributes no scores, and dividing the score sum by the result count
        #: would be right only by coincidence.
        self._scored_results = 0
        self._failures: dict[str, int] = {}

    def record_success(
        self, *, duration_ms: float, result_count: int, average_score: float | None
    ) -> None:
        """Record one search that answered, however many results it found.

        A search that matched nothing is a **success**: the corpus contains
        nothing near the query, which is an answer. Counting it as a failure
        would make the failure rate a measure of the corpus rather than of the
        platform's health, and would hide a genuine outage behind it.
        """
        with self._lock:
            self._total += 1
            self._successful += 1
            self._results += max(0, result_count)
            self._latency_sum += max(0.0, duration_ms)
            if average_score is not None and result_count > 0:
                # Weighted by the number of results, so a search returning ten
                # results influences the platform average ten times as much as
                # one returning a single result — which is what "average
                # relevance score" means across searches of different sizes.
                self._score_sum += average_score * result_count
                self._scored_results += result_count

    def record_failure(self, *, duration_ms: float, code: SearchFailureCode) -> None:
        """Record one search that could not be answered, and why.

        The duration is deliberately **not** added to the latency sum: a failure
        is usually a timeout against an unreachable dependency, and folding that
        into the average would make the platform look slow when what it actually
        is, is down. It is still passed in, because a future recorder may well
        want to chart time-to-failure separately.
        """
        with self._lock:
            self._total += 1
            self._failed += 1
            self._failures[code.value] = self._failures.get(code.value, 0) + 1

    def snapshot(self) -> SearchMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return SearchMetricsSnapshot(
                since=self._since,
                total_searches=self._total,
                successful_searches=self._successful,
                failed_searches=self._failed,
                total_results=self._results,
                average_latency_ms=(
                    round(self._latency_sum / self._successful, 2)
                    if self._successful > 0
                    else None
                ),
                average_score=(
                    round_score(self._score_sum / self._scored_results)
                    if self._scored_results > 0
                    else None
                ),
                failures_by_code=dict(self._failures),
            )

    def reset(self) -> None:
        """Discard every counter.

        For tests, and for an operator who wants a fresh window. Not called by
        the application — the counters are the process's history, and clearing
        them on a schedule would make the "since" timestamp a lie.
        """
        with self._lock:
            self._since = datetime.now(UTC)
            self._total = 0
            self._successful = 0
            self._failed = 0
            self._results = 0
            self._latency_sum = 0.0
            self._score_sum = 0.0
            self._scored_results = 0
            self._failures.clear()


class NullSearchMetrics:
    """A recorder that counts nothing.

    The default for a service constructed without observability — a script, or a
    unit test that is not about metrics. Same role, and same reasoning, as
    :class:`~services.timeline.NullTimelineRecorder`: recording code stays a
    plain call with no ``if self._metrics`` guard at every site.
    """

    def record_success(
        self, *, duration_ms: float, result_count: int, average_score: float | None
    ) -> None:
        """Discard the observation."""

    def record_failure(self, *, duration_ms: float, code: SearchFailureCode) -> None:
        """Discard the observation."""

    def snapshot(self) -> SearchMetricsSnapshot:
        """Report an empty window."""
        return SearchMetricsSnapshot(
            since=datetime.now(UTC),
            total_searches=0,
            successful_searches=0,
            failed_searches=0,
            total_results=0,
            average_latency_ms=None,
            average_score=None,
            failures_by_code={},
        )


#: The one recorder the process shares.
#:
#: Module-level rather than per request, because per-request counters would count
#: to one and reset — the metric is a property of the *process*, so the object
#: holding it has to be too. The same reasoning that makes the embedder and the
#: job queues process-wide.
_shared = InMemorySearchMetrics()


def get_search_metrics() -> SearchMetricsRecorder:
    """Return the process-wide search metrics recorder."""
    return _shared


def reset_search_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "InMemorySearchMetrics",
    "NullSearchMetrics",
    "SearchMetricsRecorder",
    "SearchMetricsSnapshot",
    "get_search_metrics",
    "reset_search_metrics",
]
