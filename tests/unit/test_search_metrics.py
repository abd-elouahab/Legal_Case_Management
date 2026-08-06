"""Unit tests for :mod:`services.search_metrics`.

The four figures ``11-semantic-search.md`` asks for — search count, average
latency, average relevance, and failed searches — plus the two judgement calls
behind them, both asserted rather than left in a docstring:

* **a search that matches nothing is a success.** Counting it as a failure would
  make the failure rate a measure of the corpus rather than of the platform, and
  would hide a genuine outage behind it.
* **a failed search's duration is excluded from the latency average.** A failure
  is usually a timeout against an unreachable dependency, and folding that in
  makes a platform that is *down* look merely *slow*.
"""

from __future__ import annotations

import threading

from core.search import SearchFailureCode
from services.search_metrics import (
    InMemorySearchMetrics,
    NullSearchMetrics,
    SearchMetricsRecorder,
    get_search_metrics,
)


class TestCounting:
    def test_a_fresh_recorder_reports_an_empty_window(self) -> None:
        snapshot = InMemorySearchMetrics().snapshot()

        assert snapshot.total_searches == 0
        assert snapshot.successful_searches == 0
        assert snapshot.failed_searches == 0
        assert snapshot.average_latency_ms is None
        assert snapshot.average_score is None
        assert snapshot.average_results is None
        assert snapshot.success_rate == 0.0
        assert snapshot.failure_rate == 0.0

    def test_successes_and_failures_are_counted_separately(self) -> None:
        metrics = InMemorySearchMetrics()
        metrics.record_success(duration_ms=10, result_count=3, average_score=0.8)
        metrics.record_failure(
            duration_ms=3000, code=SearchFailureCode.VECTOR_STORE_UNAVAILABLE
        )

        snapshot = metrics.snapshot()

        assert snapshot.total_searches == 2
        assert snapshot.successful_searches == 1
        assert snapshot.failed_searches == 1

    def test_a_search_matching_nothing_counts_as_a_success(self) -> None:
        """An empty corpus is an answer, not a fault."""
        metrics = InMemorySearchMetrics()
        metrics.record_success(duration_ms=8, result_count=0, average_score=None)

        snapshot = metrics.snapshot()

        assert snapshot.successful_searches == 1
        assert snapshot.failed_searches == 0
        assert snapshot.success_rate == 100.0
        assert snapshot.average_score is None

    def test_the_rates_always_sum_to_one_hundred(self) -> None:
        """Derived as complements, so two independent roundings cannot disagree."""
        metrics = InMemorySearchMetrics()
        for _ in range(3):
            metrics.record_success(duration_ms=5, result_count=1, average_score=0.5)
        metrics.record_failure(duration_ms=1, code=SearchFailureCode.UNKNOWN)

        snapshot = metrics.snapshot()

        assert snapshot.success_rate + snapshot.failure_rate == 100.0


class TestAverages:
    def test_the_latency_average_covers_successes_only(self) -> None:
        """A three-second timeout must not make retrieval look slow."""
        metrics = InMemorySearchMetrics()
        metrics.record_success(duration_ms=10, result_count=1, average_score=0.5)
        metrics.record_success(duration_ms=20, result_count=1, average_score=0.5)
        metrics.record_failure(
            duration_ms=30_000, code=SearchFailureCode.VECTOR_STORE_UNAVAILABLE
        )

        assert metrics.snapshot().average_latency_ms == 15.0

    def test_the_relevance_average_is_weighted_by_result_count(self) -> None:
        """A ten-result search should influence the platform mean ten times as much."""
        metrics = InMemorySearchMetrics()
        metrics.record_success(duration_ms=5, result_count=10, average_score=0.9)
        metrics.record_success(duration_ms=5, result_count=1, average_score=0.1)

        expected = (0.9 * 10 + 0.1 * 1) / 11
        assert metrics.snapshot().average_score == round(expected, 4)

    def test_an_empty_search_does_not_drag_the_relevance_average_down(self) -> None:
        """It contributed no scores, so it must contribute nothing to their mean."""
        metrics = InMemorySearchMetrics()
        metrics.record_success(duration_ms=5, result_count=2, average_score=0.8)
        metrics.record_success(duration_ms=5, result_count=0, average_score=None)

        assert metrics.snapshot().average_score == 0.8

    def test_average_results_per_search_is_reported(self) -> None:
        metrics = InMemorySearchMetrics()
        metrics.record_success(duration_ms=5, result_count=4, average_score=0.5)
        metrics.record_success(duration_ms=5, result_count=0, average_score=None)

        snapshot = metrics.snapshot()

        assert snapshot.total_results == 4
        assert snapshot.average_results == 2.0


class TestFailureBreakdown:
    def test_failures_are_grouped_by_cause(self) -> None:
        """A rate says something is wrong; only the breakdown says what."""
        metrics = InMemorySearchMetrics()
        metrics.record_failure(duration_ms=1, code=SearchFailureCode.EMBEDDING_UNAVAILABLE)
        metrics.record_failure(duration_ms=1, code=SearchFailureCode.EMBEDDING_UNAVAILABLE)
        metrics.record_failure(
            duration_ms=1, code=SearchFailureCode.VECTOR_STORE_UNAVAILABLE
        )

        assert metrics.snapshot().failures_by_code == {
            "embedding_unavailable": 2,
            "vector_store_unavailable": 1,
        }

    def test_the_breakdown_is_a_copy(self) -> None:
        """A caller mutating the report must not mutate the counters."""
        metrics = InMemorySearchMetrics()
        metrics.record_failure(duration_ms=1, code=SearchFailureCode.UNKNOWN)

        metrics.snapshot().failures_by_code["unknown"] = 999

        assert metrics.snapshot().failures_by_code == {"unknown": 1}


class TestPrivacy:
    def test_the_recorder_cannot_be_handed_a_query_or_a_passage(self) -> None:
        """Structural, not a matter of care.

        A recorder that has nowhere to *put* the query text cannot leak it —
        the same argument the timeline's narrow ``TimelineRecorder`` makes.
        """
        import inspect

        success = set(inspect.signature(InMemorySearchMetrics.record_success).parameters)
        failure = set(inspect.signature(InMemorySearchMetrics.record_failure).parameters)

        assert success == {"self", "duration_ms", "result_count", "average_score"}
        assert failure == {"self", "duration_ms", "code"}


class TestConcurrency:
    def test_concurrent_recording_loses_no_observation(self) -> None:
        """Search is the platform's most frequent operation and runs on many threads."""
        metrics = InMemorySearchMetrics()

        def record() -> None:
            for _ in range(200):
                metrics.record_success(duration_ms=1, result_count=1, average_score=0.5)

        threads = [threading.Thread(target=record) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert metrics.snapshot().successful_searches == 1600


class TestNullRecorder:
    def test_it_counts_nothing_and_still_reports_a_snapshot(self) -> None:
        recorder: SearchMetricsRecorder = NullSearchMetrics()
        recorder.record_success(duration_ms=5, result_count=3, average_score=0.9)
        recorder.record_failure(duration_ms=5, code=SearchFailureCode.UNKNOWN)

        assert recorder.snapshot().total_searches == 0


class TestProcessWideRecorder:
    def test_the_shared_recorder_is_one_object(self) -> None:
        """A counter rebuilt per request counts to one."""
        assert get_search_metrics() is get_search_metrics()

    def test_reset_restarts_the_window(self) -> None:
        metrics = InMemorySearchMetrics()
        before = metrics.snapshot().since
        metrics.record_success(duration_ms=1, result_count=1, average_score=0.5)

        metrics.reset()
        snapshot = metrics.snapshot()

        assert snapshot.total_searches == 0
        assert snapshot.since >= before
