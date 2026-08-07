"""Unit tests for :mod:`services.assistant_metrics`.

The arithmetic decisions are the point here, not the plumbing: which runs go into
which average, and which figure is ``None`` rather than ``0``.
"""

from __future__ import annotations

from core.rag import RagFailureCode
from services.assistant_metrics import (
    AssistantMetricsRecorder,
    InMemoryAssistantMetrics,
    NullAssistantMetrics,
    get_assistant_metrics,
)


class TestEmptyWindow:
    def test_nothing_recorded_reports_zero_and_none(self) -> None:
        snapshot = InMemoryAssistantMetrics().snapshot()

        assert snapshot.total_requests == 0
        assert snapshot.success_rate == 0.0
        assert snapshot.failure_rate == 0.0
        assert snapshot.grounding_rate == 0.0
        assert snapshot.average_response_ms is None

    def test_the_window_start_is_reported(self) -> None:
        """Not decoration: these counters reset on restart and each instance
        counts only its own traffic, and ``since`` is how that is said."""
        assert InMemoryAssistantMetrics().snapshot().since is not None


class TestSuccess:
    def test_a_grounded_answer_is_counted_as_grounded(self) -> None:
        recorder = InMemoryAssistantMetrics()
        recorder.record_success(duration_ms=100, grounded=True, streamed=False)

        snapshot = recorder.snapshot()
        assert snapshot.successful_requests == 1
        assert snapshot.grounded_answers == 1
        assert snapshot.insufficient_evidence == 0

    def test_an_answer_with_no_evidence_is_still_a_success(self) -> None:
        """Declining is the assistant working. Counting it as a failure would
        make the failure rate a measure of the corpus rather than the platform,
        and would hide a genuine outage behind it."""
        recorder = InMemoryAssistantMetrics()
        recorder.record_success(duration_ms=100, grounded=False, streamed=False)

        snapshot = recorder.snapshot()
        assert snapshot.successful_requests == 1
        assert snapshot.failed_requests == 0
        assert snapshot.insufficient_evidence == 1

    def test_streamed_answers_are_counted_separately(self) -> None:
        """Streaming is the one part of the feature that degrades silently: a
        provider that stopped supporting it falls back to a complete answer,
        which is correct, works, and is invisible from every other number."""
        recorder = InMemoryAssistantMetrics()
        recorder.record_success(duration_ms=10, grounded=True, streamed=True)
        recorder.record_success(duration_ms=10, grounded=True, streamed=False)

        assert recorder.snapshot().streamed_requests == 1

    def test_the_average_is_over_successful_runs(self) -> None:
        recorder = InMemoryAssistantMetrics()
        recorder.record_success(duration_ms=100, grounded=True, streamed=False)
        recorder.record_success(duration_ms=300, grounded=True, streamed=False)

        assert recorder.snapshot().average_response_ms == 200.0

    def test_a_negative_duration_is_clamped(self) -> None:
        """A clock adjustment mid-request must not produce a negative sample."""
        recorder = InMemoryAssistantMetrics()
        recorder.record_success(duration_ms=-50, grounded=True, streamed=False)

        assert recorder.snapshot().average_response_ms == 0.0


class TestFailure:
    def test_a_failure_is_grouped_by_cause(self) -> None:
        """A failure rate says something is wrong; this says what."""
        recorder = InMemoryAssistantMetrics()
        recorder.record_failure(duration_ms=50, code=RagFailureCode.LLM_UNAVAILABLE)
        recorder.record_failure(duration_ms=50, code=RagFailureCode.TIMEOUT)
        recorder.record_failure(duration_ms=50, code=RagFailureCode.TIMEOUT)

        assert recorder.snapshot().failures_by_code == {
            "llm_unavailable": 1,
            "timeout": 2,
        }

    def test_a_failures_duration_is_excluded_from_the_average(self) -> None:
        """A failure is usually a timeout against an unresponsive provider, and
        folding that in would make a platform that is *blocked* look merely slow."""
        recorder = InMemoryAssistantMetrics()
        recorder.record_success(duration_ms=100, grounded=True, streamed=False)
        recorder.record_failure(duration_ms=45000, code=RagFailureCode.TIMEOUT)

        assert recorder.snapshot().average_response_ms == 100.0


class TestRates:
    def test_the_rates_always_sum_to_a_hundred(self) -> None:
        """Derived as complements rather than computed independently — two
        separate roundings would not."""
        recorder = InMemoryAssistantMetrics()
        for _ in range(3):
            recorder.record_success(duration_ms=10, grounded=True, streamed=False)
        recorder.record_failure(duration_ms=10, code=RagFailureCode.UNKNOWN)

        snapshot = recorder.snapshot()
        assert snapshot.success_rate + snapshot.failure_rate == 100.0

    def test_the_grounding_rate_is_over_successful_runs_only(self) -> None:
        """A provider outage would otherwise depress a number that answers "does
        the corpus cover what people ask?" — which has nothing to do with it."""
        recorder = InMemoryAssistantMetrics()
        recorder.record_success(duration_ms=10, grounded=True, streamed=False)
        recorder.record_success(duration_ms=10, grounded=False, streamed=False)
        recorder.record_failure(duration_ms=10, code=RagFailureCode.LLM_UNAVAILABLE)

        assert recorder.snapshot().grounding_rate == 50.0


class TestIsolation:
    def test_reset_starts_a_new_window(self) -> None:
        recorder = InMemoryAssistantMetrics()
        recorder.record_success(duration_ms=10, grounded=True, streamed=False)
        recorder.reset()

        assert recorder.snapshot().total_requests == 0

    def test_the_null_recorder_counts_nothing(self) -> None:
        recorder = NullAssistantMetrics()
        recorder.record_success(duration_ms=10, grounded=True, streamed=False)
        recorder.record_failure(duration_ms=10, code=RagFailureCode.UNKNOWN)

        assert recorder.snapshot().total_requests == 0

    def test_the_process_shares_one_recorder(self) -> None:
        """Per-request counters would count to one and reset: the metric is a
        property of the process, so the object holding it has to be too."""
        assert get_assistant_metrics() is get_assistant_metrics()


class TestProtocol:
    def test_the_recorder_cannot_be_handed_any_text(self) -> None:
        """A recorder that *cannot be handed* a question, an answer, a title, or
        a user cannot leak one — the same structural argument the RAG recorder
        makes."""
        import inspect

        parameters = set()
        for name in ("record_success", "record_failure"):
            parameters |= set(
                inspect.signature(getattr(AssistantMetricsRecorder, name)).parameters
            )

        assert not parameters & {
            "question",
            "answer",
            "title",
            "content",
            "citation",
            "actor",
            "user",
            "conversation",
        }
