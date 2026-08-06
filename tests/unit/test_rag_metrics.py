"""Unit tests for :mod:`services.rag_metrics`.

The counters the spec's Monitoring section names, plus the three arithmetic
decisions that would otherwise be invisible and wrong:

* a failure's duration is **not** folded into the latency average, or a platform
  that is blocked looks merely slow;
* generation latency has its **own denominator**, or a platform answering fewer
  questions looks like a platform with a faster model;
* an average over nothing is ``None`` rather than ``0``, because "no answers
  yet" and "every answer is uncited" are very different operational situations.
"""

from __future__ import annotations

import pytest

from core.rag import RagFailureCode
from services.rag_metrics import (
    InMemoryRagMetrics,
    NullRagMetrics,
    RagMetricsRecorder,
    get_rag_metrics,
    reset_rag_metrics,
)


@pytest.fixture
def metrics() -> InMemoryRagMetrics:
    return InMemoryRagMetrics()


def succeed(recorder: InMemoryRagMetrics, **fields: object) -> None:
    defaults: dict[str, object] = {
        "duration_ms": 900.0,
        "retrieval_ms": 120.0,
        "generation_ms": 700.0,
        "passage_count": 4,
        "citation_count": 4,
        "grounded": True,
        "prompt_tokens": 100,
        "completion_tokens": 30,
    }
    recorder.record_success(**{**defaults, **fields})  # type: ignore[arg-type]


class TestCounting:
    def test_an_empty_window_reports_zeros_without_dividing_by_them(
        self, metrics: InMemoryRagMetrics
    ) -> None:
        snapshot = metrics.snapshot()

        assert snapshot.total_requests == 0
        assert snapshot.success_rate == 0.0
        assert snapshot.failure_rate == 0.0
        assert snapshot.average_latency_ms is None
        assert snapshot.average_citations is None

    def test_a_grounded_answer_is_counted(self, metrics: InMemoryRagMetrics) -> None:
        succeed(metrics)
        snapshot = metrics.snapshot()

        assert snapshot.total_requests == 1
        assert snapshot.successful_requests == 1
        assert snapshot.grounded_answers == 1
        assert snapshot.insufficient_evidence == 0

    def test_declining_to_answer_is_a_success(self, metrics: InMemoryRagMetrics) -> None:
        succeed(metrics, grounded=False, generation_ms=None, citation_count=0)
        snapshot = metrics.snapshot()

        assert snapshot.successful_requests == 1
        assert snapshot.failed_requests == 0
        assert snapshot.insufficient_evidence == 1

    def test_a_failure_is_grouped_by_cause(self, metrics: InMemoryRagMetrics) -> None:
        """A failure rate says something is wrong; this says what."""
        metrics.record_failure(duration_ms=5_000, code=RagFailureCode.LLM_UNAVAILABLE)
        metrics.record_failure(duration_ms=5_000, code=RagFailureCode.TIMEOUT)
        metrics.record_failure(duration_ms=5_000, code=RagFailureCode.TIMEOUT)

        assert metrics.snapshot().failures_by_code == {"llm_unavailable": 1, "timeout": 2}

    def test_the_rates_always_sum_to_one_hundred(self, metrics: InMemoryRagMetrics) -> None:
        for _ in range(3):
            succeed(metrics)
        metrics.record_failure(duration_ms=1.0, code=RagFailureCode.UNKNOWN)

        snapshot = metrics.snapshot()
        assert snapshot.success_rate + snapshot.failure_rate == 100.0


class TestAverages:
    def test_a_failure_does_not_slow_the_latency_average(
        self, metrics: InMemoryRagMetrics
    ) -> None:
        """A timeout against an unresponsive provider would make a blocked
        platform look merely slow."""
        succeed(metrics, duration_ms=900.0)
        metrics.record_failure(duration_ms=45_000.0, code=RagFailureCode.TIMEOUT)

        assert metrics.snapshot().average_latency_ms == 900.0

    def test_generation_latency_has_its_own_denominator(
        self, metrics: InMemoryRagMetrics
    ) -> None:
        """A run that found no evidence never calls a model; averaging its
        absence in would make the model look faster the less it was used."""
        succeed(metrics, generation_ms=700.0)
        succeed(metrics, generation_ms=None, grounded=False, citation_count=0)

        assert metrics.snapshot().average_generation_ms == 700.0

    def test_retrieval_latency_is_reported_separately(
        self, metrics: InMemoryRagMetrics
    ) -> None:
        succeed(metrics, retrieval_ms=100.0)
        succeed(metrics, retrieval_ms=200.0)

        assert metrics.snapshot().average_retrieval_ms == 150.0

    def test_citations_are_averaged_over_grounded_answers_only(
        self, metrics: InMemoryRagMetrics
    ) -> None:
        succeed(metrics, citation_count=4)
        succeed(metrics, grounded=False, citation_count=0, generation_ms=None)

        assert metrics.snapshot().average_citations == 4.0

    def test_the_grounding_rate_ignores_failures(self, metrics: InMemoryRagMetrics) -> None:
        """A provider outage says nothing about whether the corpus covers what
        people ask."""
        succeed(metrics)
        succeed(metrics, grounded=False, citation_count=0, generation_ms=None)
        metrics.record_failure(duration_ms=1.0, code=RagFailureCode.LLM_UNAVAILABLE)

        assert metrics.snapshot().grounding_rate == 50.0


class TestTokenUsage:
    def test_usage_is_accumulated_with_its_own_denominator(
        self, metrics: InMemoryRagMetrics
    ) -> None:
        succeed(metrics, prompt_tokens=100, completion_tokens=30)
        succeed(metrics, prompt_tokens=200, completion_tokens=70)
        snapshot = metrics.snapshot()

        assert snapshot.total_prompt_tokens == 300
        assert snapshot.total_completion_tokens == 100
        assert snapshot.metered_requests == 2
        assert snapshot.average_total_tokens == 200.0

    def test_a_provider_that_reports_nothing_leaves_the_totals_absent(
        self, metrics: InMemoryRagMetrics
    ) -> None:
        """Zero would read as 'this platform's answers are free'."""
        succeed(metrics, prompt_tokens=None, completion_tokens=None)
        snapshot = metrics.snapshot()

        assert snapshot.total_prompt_tokens is None
        assert snapshot.metered_requests == 0
        assert snapshot.average_total_tokens is None


class TestSnapshotIsolation:
    def test_a_snapshot_does_not_change_under_later_traffic(
        self, metrics: InMemoryRagMetrics
    ) -> None:
        succeed(metrics)
        snapshot = metrics.snapshot()
        succeed(metrics)

        assert snapshot.total_requests == 1

    def test_resetting_starts_a_new_window(self, metrics: InMemoryRagMetrics) -> None:
        succeed(metrics)
        before = metrics.snapshot().since
        metrics.reset()
        after = metrics.snapshot()

        assert after.total_requests == 0
        assert after.since >= before


class TestRecorderContract:
    def test_the_recorder_cannot_be_handed_a_question_or_an_answer(self) -> None:
        """A recorder that cannot be handed the text cannot leak it."""
        import inspect

        parameters = set(inspect.signature(InMemoryRagMetrics.record_success).parameters)

        assert not {"question", "answer", "text", "passage", "actor", "user"} & parameters

    def test_the_null_recorder_counts_nothing_and_still_answers(self) -> None:
        recorder: RagMetricsRecorder = NullRagMetrics()
        recorder.record_success(
            duration_ms=1.0,
            retrieval_ms=1.0,
            generation_ms=1.0,
            passage_count=1,
            citation_count=1,
            grounded=True,
            prompt_tokens=1,
            completion_tokens=1,
        )
        recorder.record_failure(duration_ms=1.0, code=RagFailureCode.UNKNOWN)

        assert recorder.snapshot().total_requests == 0

    def test_the_process_shares_one_recorder(self) -> None:
        """A counter rebuilt on every request counts to one."""
        assert get_rag_metrics() is get_rag_metrics()
        reset_rag_metrics()
        assert get_rag_metrics().snapshot().total_requests == 0
