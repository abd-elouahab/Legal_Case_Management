"""Unit tests for trace context and the tracer.

The claims worth pinning down are the ones that would be invisible if they broke:
an inbound ``traceparent`` that is trusted for correlation and validated before
it reaches a log; nesting that survives an exception; and a buffer that is bounded
in both directions.
"""

from __future__ import annotations

import pytest

from core.observability import MonitoringComponent
from core.tracing import (
    SpanKind,
    SpanStatus,
    TraceContext,
    bind_trace_context,
    current_trace_context,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    reset_trace_context,
)
from services.metrics_registry import InMemoryMetricsRegistry
from services.tracer import InMemoryTracer, NullTracer

VALID = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


class TestIdentifiers:
    """W3C shapes, so a future OpenTelemetry SDK finds what it expects."""

    def test_a_trace_id_is_thirty_two_lowercase_hex_characters(self) -> None:
        trace_id = new_trace_id()
        assert len(trace_id) == 32
        assert trace_id == trace_id.lower()
        int(trace_id, 16)

    def test_a_span_id_is_sixteen(self) -> None:
        assert len(new_span_id()) == 16

    def test_identifiers_are_not_reused(self) -> None:
        assert len({new_trace_id() for _ in range(100)}) == 100


class TestPropagation:
    """An inbound header joins a trace and grants nothing."""

    def test_it_parses_a_valid_header(self) -> None:
        context = parse_traceparent(VALID)

        assert context is not None
        assert context.trace_id == "0af7651916cd43dd8448eb211c80319c"
        assert context.parent_span_id == "b7ad6b7169203331"
        assert context.remote is True

    def test_this_process_mints_its_own_span_rather_than_adopting_the_caller_s(self) -> None:
        context = parse_traceparent(VALID)

        assert context is not None
        assert context.span_id != "b7ad6b7169203331"

    def test_a_missing_header_is_not_an_error(self) -> None:
        assert parse_traceparent(None) is None

    @pytest.mark.parametrize(
        "header",
        [
            "garbage",
            "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",  # unknown version
            "00-" + "0" * 32 + "-b7ad6b7169203331-01",  # all-zero trace id
            "00-0af7651916cd43dd8448eb211c80319c-" + "0" * 16 + "-01",  # all-zero span id
            "00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01",  # uppercase
            "00-0af765-b7ad6b7169203331-01",  # wrong length
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331",  # truncated
        ],
    )
    def test_an_unusable_header_starts_a_fresh_trace_rather_than_failing(
        self, header: str
    ) -> None:
        """A caller's bad header must never be the reason a request is refused."""
        assert parse_traceparent(header) is None

    def test_a_tracestate_is_carried_but_stripped_of_control_characters(self) -> None:
        context = parse_traceparent(VALID, trace_state="vendor=abc\r\ninjected")

        assert context is not None
        assert context.trace_state is not None
        assert "\n" not in context.trace_state and "\r" not in context.trace_state

    def test_it_round_trips(self) -> None:
        context = TraceContext(trace_id=new_trace_id(), span_id=new_span_id())
        parsed = parse_traceparent(format_traceparent(context))

        assert parsed is not None
        assert parsed.trace_id == context.trace_id
        assert parsed.parent_span_id == context.span_id


class TestAmbientContext:
    """The current span is found rather than passed."""

    def test_there_is_no_context_by_default(self) -> None:
        assert current_trace_context() is None

    def test_binding_and_resetting_restores_the_predecessor(self) -> None:
        outer = TraceContext(trace_id=new_trace_id(), span_id=new_span_id())
        token = bind_trace_context(outer)
        try:
            assert current_trace_context() == outer
        finally:
            reset_trace_context(token)

        assert current_trace_context() is None

    def test_a_child_keeps_the_trace_and_records_its_parent(self) -> None:
        parent = TraceContext(trace_id=new_trace_id(), span_id=new_span_id())
        child = parent.child()

        assert child.trace_id == parent.trace_id
        assert child.parent_span_id == parent.span_id
        assert child.span_id != parent.span_id


class TestTracer:
    """Spans, their nesting, and the bounded buffer."""

    def test_a_completed_span_becomes_a_trace(self) -> None:
        tracer = InMemoryTracer()
        with tracer.span("GET /cases", component=MonitoringComponent.API, kind=SpanKind.SERVER):
            pass

        snapshot = tracer.snapshot()
        assert snapshot.traces_recorded == 1
        assert snapshot.recent[0].name == "GET /cases"

    def test_children_nest_under_their_parent(self) -> None:
        tracer = InMemoryTracer()
        with (
            tracer.span("GET /cases", component=MonitoringComponent.API, kind=SpanKind.SERVER) as root,
            tracer.span("authorize", component=MonitoringComponent.AUTHORIZATION),
        ):
            pass

        trace = tracer.snapshot().recent[0]
        child = next(span for span in trace.spans if span.name == "authorize")
        assert child.parent_span_id == root.span_id

    def test_spans_are_ordered_as_they_happened(self) -> None:
        """A coarse system clock must not put a child above its own parent."""
        tracer = InMemoryTracer()
        with tracer.span("root", component=MonitoringComponent.API, kind=SpanKind.SERVER):
            with tracer.span("first", component=MonitoringComponent.AUTH):
                pass
            with tracer.span("second", component=MonitoringComponent.CASES):
                pass

        names = [span.name for span in tracer.snapshot().recent[0].spans]
        assert names == ["root", "first", "second"]

    def test_an_exception_is_recorded_and_re_raised(self) -> None:
        """A tracer that swallowed exceptions would change behaviour."""
        tracer = InMemoryTracer()
        with (
            pytest.raises(ValueError),
            tracer.span("boom", component=MonitoringComponent.API, kind=SpanKind.SERVER),
        ):
            raise ValueError("no")

        trace = tracer.snapshot().recent[0]
        assert trace.status is SpanStatus.ERROR
        assert trace.failed is True
        assert trace.spans[0].error_type == "ValueError"

    def test_span_attributes_are_redacted(self) -> None:
        tracer = InMemoryTracer()
        with tracer.span("x", component=MonitoringComponent.API, kind=SpanKind.SERVER) as span:
            span.set_attribute("password", "hunter2")
            span.set_attribute("http.route", "/cases/{case_id}")

        attributes = tracer.snapshot().recent[0].spans[0].attributes
        assert attributes["password"] == "[redacted]"
        assert attributes["http.route"] == "/cases/{case_id}"

    def test_the_span_count_per_trace_is_bounded_and_the_overflow_counted(self) -> None:
        tracer = InMemoryTracer(max_spans_per_trace=3)
        with tracer.span("root", component=MonitoringComponent.API, kind=SpanKind.SERVER):
            for index in range(10):
                with tracer.span(f"child-{index}", component=MonitoringComponent.CASES):
                    pass

        snapshot = tracer.snapshot()
        assert snapshot.spans_dropped > 0
        assert snapshot.recent[0].dropped_spans > 0

    def test_the_buffer_is_bounded_and_keeps_the_newest(self) -> None:
        tracer = InMemoryTracer(buffer_size=2)
        for index in range(5):
            with tracer.span(f"req-{index}", component=MonitoringComponent.API, kind=SpanKind.SERVER):
                pass

        snapshot = tracer.snapshot()
        assert len(snapshot.recent) == 2
        assert snapshot.recent[0].name == "req-4"

    def test_a_completed_span_reported_outside_a_trace_is_not_invented_into_one(self) -> None:
        """A query from a startup routine has no request to belong to."""
        tracer = InMemoryTracer()
        tracer.record_completed(
            "db select", component=MonitoringComponent.DATABASE, duration_ms=1.0
        )

        assert tracer.snapshot().traces_recorded == 0

    def test_span_durations_reach_the_metric_registry(self) -> None:
        registry = InMemoryMetricsRegistry()
        tracer = InMemoryTracer(metrics=registry)
        with tracer.span("x", component=MonitoringComponent.API, kind=SpanKind.SERVER):
            pass

        from core.observability import MetricName

        assert registry.snapshot().total(MetricName.SPANS_STARTED_TOTAL) == 1.0


class TestNullTracer:
    """Instrumented code must not have to guard against it."""

    def test_it_still_yields_a_usable_span(self) -> None:
        tracer = NullTracer()
        with tracer.span("x", component=MonitoringComponent.API) as span:
            span.set_attribute("k", "v").set_status(SpanStatus.OK)

        assert tracer.snapshot().traces_recorded == 0

    def test_it_does_not_make_anything_believe_it_is_being_traced(self) -> None:
        tracer = NullTracer()
        with tracer.span("x", component=MonitoringComponent.API):
            assert current_trace_context() is None
