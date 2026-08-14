"""Unit tests for the metric registry and the Prometheus renderer.

The properties that matter are the ones an unbounded metrics layer gets wrong:
labels that cannot be invented, a series count that cannot grow without bound,
and a snapshot that is internally consistent. The exporter is tested for the two
things that silently break a dashboard — the unit convention and the mandatory
``+Inf`` bucket.
"""

from __future__ import annotations

from core.observability import MetricName
from services.metrics_export import render_prometheus
from services.metrics_registry import (
    InMemoryMetricsRegistry,
    NullMetricsRegistry,
)


def _record_latencies(registry: InMemoryMetricsRegistry, values: list[float]) -> None:
    for value in values:
        registry.observe(
            MetricName.HTTP_REQUEST_DURATION_MS, value, labels={"method": "GET", "route": "/x"}
        )


class TestCounters:
    """Monotonic by construction."""

    def test_it_counts(self) -> None:
        registry = InMemoryMetricsRegistry()
        registry.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL)
        registry.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL)

        assert registry.snapshot().total(MetricName.UNHANDLED_EXCEPTIONS_TOTAL) == 2.0

    def test_a_negative_increment_is_ignored_rather_than_subtracted(self) -> None:
        """A counter that could fall would make every rate computed from it wrong."""
        registry = InMemoryMetricsRegistry()
        registry.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL)
        registry.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL, value=-5)

        assert registry.snapshot().total(MetricName.UNHANDLED_EXCEPTIONS_TOTAL) == 1.0

    def test_recording_a_counter_as_a_gauge_is_dropped_rather_than_raising(self) -> None:
        registry = InMemoryMetricsRegistry()
        registry.set_gauge(MetricName.UNHANDLED_EXCEPTIONS_TOTAL, 5)

        assert registry.snapshot().series == ()


class TestLabels:
    """The cardinality guard, which is what keeps this from being a leak."""

    def test_an_undeclared_label_is_dropped_and_the_observation_kept(self) -> None:
        """Losing a real observation to punish a typo would be the wrong trade."""
        registry = InMemoryMetricsRegistry()
        registry.increment(
            MetricName.HTTP_REQUESTS_TOTAL,
            labels={"method": "GET", "route": "/x", "status_class": "2xx", "case_id": "secret"},
        )

        series = registry.snapshot().by_name(MetricName.HTTP_REQUESTS_TOTAL)
        assert series[0].value == 1.0
        assert "case_id" not in series[0].label_map

    def test_a_label_value_cannot_contain_a_newline(self) -> None:
        """The exposition format is newline-delimited; one would forge a metric."""
        registry = InMemoryMetricsRegistry()
        registry.increment(
            MetricName.HTTP_REQUESTS_TOTAL,
            labels={"method": "GET", "route": "/x\nevil 99", "status_class": "2xx"},
        )

        route = registry.snapshot().by_name(MetricName.HTTP_REQUESTS_TOTAL)[0].label_map["route"]
        assert "\n" not in route

    def test_a_label_value_is_length_bounded(self) -> None:
        registry = InMemoryMetricsRegistry()
        registry.increment(
            MetricName.HTTP_REQUESTS_TOTAL,
            labels={"method": "GET", "route": "/" + "a" * 500, "status_class": "2xx"},
        )

        route = registry.snapshot().by_name(MetricName.HTTP_REQUESTS_TOTAL)[0].label_map["route"]
        assert len(route) <= 120

    def test_labels_are_order_independent(self) -> None:
        registry = InMemoryMetricsRegistry()
        registry.increment(
            MetricName.HTTP_REQUESTS_TOTAL,
            labels={"route": "/x", "method": "GET", "status_class": "2xx"},
        )
        registry.increment(
            MetricName.HTTP_REQUESTS_TOTAL,
            labels={"method": "GET", "status_class": "2xx", "route": "/x"},
        )

        assert len(registry.snapshot().by_name(MetricName.HTTP_REQUESTS_TOTAL)) == 1

    def test_the_series_ceiling_is_enforced_and_what_it_refuses_is_counted(self) -> None:
        """A page that is quietly incomplete is worse than one that says so."""
        registry = InMemoryMetricsRegistry(max_series=3)
        for index in range(10):
            registry.increment(
                MetricName.HTTP_REQUESTS_TOTAL,
                labels={"method": "GET", "route": f"/r{index}", "status_class": "2xx"},
            )

        snapshot = registry.snapshot()
        assert len(snapshot.by_name(MetricName.HTTP_REQUESTS_TOTAL)) == 3
        assert snapshot.dropped_series == 7


class TestHistograms:
    """Distributions in constant memory."""

    def test_it_accumulates_count_sum_and_extremes(self) -> None:
        registry = InMemoryMetricsRegistry()
        _record_latencies(registry, [10.0, 20.0, 30.0])

        histogram = registry.snapshot().histogram(MetricName.HTTP_REQUEST_DURATION_MS)
        assert histogram is not None
        assert histogram.count == 3
        assert histogram.sum == 60.0
        assert histogram.minimum == 10.0
        assert histogram.maximum == 30.0
        assert histogram.average == 20.0

    def test_an_average_over_nothing_is_none_rather_than_zero(self) -> None:
        registry = InMemoryMetricsRegistry()
        assert registry.snapshot().histogram(MetricName.HTTP_REQUEST_DURATION_MS) is None

    def test_buckets_are_cumulative(self) -> None:
        registry = InMemoryMetricsRegistry()
        _record_latencies(registry, [3.0, 30.0, 300.0])

        histogram = registry.snapshot().histogram(MetricName.HTTP_REQUEST_DURATION_MS)
        assert histogram is not None
        counts = dict(histogram.buckets)
        assert counts[5.0] == 1
        assert counts[50.0] == 2
        assert counts[500.0] == 3

    def test_a_quantile_is_estimated_within_its_bucket(self) -> None:
        registry = InMemoryMetricsRegistry()
        _record_latencies(registry, [1.0] * 99 + [40_000.0])

        histogram = registry.snapshot().histogram(MetricName.HTTP_REQUEST_DURATION_MS)
        assert histogram is not None
        assert histogram.quantile(0.50) is not None
        assert histogram.quantile(0.50) < 10.0
        assert histogram.quantile(0.99) < histogram.quantile(1.0)

    def test_a_negative_observation_is_clamped_rather_than_dropped(self) -> None:
        """A clock that stepped backwards must not cost a throughput count."""
        registry = InMemoryMetricsRegistry()
        _record_latencies(registry, [-5.0])

        histogram = registry.snapshot().histogram(MetricName.HTTP_REQUEST_DURATION_MS)
        assert histogram is not None
        assert histogram.count == 1
        assert histogram.minimum == 0.0

    def test_merging_label_combinations_is_exact(self) -> None:
        registry = InMemoryMetricsRegistry()
        registry.observe(
            MetricName.HTTP_REQUEST_DURATION_MS, 10.0, labels={"method": "GET", "route": "/a"}
        )
        registry.observe(
            MetricName.HTTP_REQUEST_DURATION_MS, 30.0, labels={"method": "GET", "route": "/b"}
        )

        merged = registry.snapshot().histogram(MetricName.HTTP_REQUEST_DURATION_MS)
        assert merged is not None
        assert merged.count == 2
        assert merged.sum == 40.0


class TestSnapshot:
    """Frozen, ordered, and consistent."""

    def test_series_are_sorted_so_two_scrapes_are_comparable(self) -> None:
        registry = InMemoryMetricsRegistry()
        for route in ("/z", "/a", "/m"):
            registry.increment(
                MetricName.HTTP_REQUESTS_TOTAL,
                labels={"method": "GET", "route": route, "status_class": "2xx"},
            )

        names = [item.labels for item in registry.snapshot().by_name(MetricName.HTTP_REQUESTS_TOTAL)]
        assert names == sorted(names)

    def test_a_snapshot_does_not_change_when_the_registry_does(self) -> None:
        registry = InMemoryMetricsRegistry()
        registry.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL)
        snapshot = registry.snapshot()
        registry.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL)

        assert snapshot.total(MetricName.UNHANDLED_EXCEPTIONS_TOTAL) == 1.0


class TestPrometheusExport:
    """The two things that silently break a dashboard."""

    def test_milliseconds_are_converted_to_seconds_name_and_all(self) -> None:
        """Every stock dashboard and alert expression assumes base units."""
        registry = InMemoryMetricsRegistry()
        _record_latencies(registry, [1_000.0])

        body = render_prometheus(registry.snapshot())
        assert "http_request_duration_seconds_bucket" in body
        assert "_milliseconds" not in body
        assert "http_request_duration_seconds_sum{method=\"GET\",route=\"/x\"} 1" in body

    def test_the_mandatory_infinity_bucket_is_present_and_equals_the_count(self) -> None:
        registry = InMemoryMetricsRegistry()
        _record_latencies(registry, [1.0, 90_000.0])

        body = render_prometheus(registry.snapshot())
        assert 'le="+Inf"} 2' in body

    def test_each_family_carries_help_and_type_exactly_once(self) -> None:
        registry = InMemoryMetricsRegistry()
        for route in ("/a", "/b"):
            registry.increment(
                MetricName.HTTP_REQUESTS_TOTAL,
                labels={"method": "GET", "route": route, "status_class": "2xx"},
            )

        body = render_prometheus(registry.snapshot())
        assert body.count("# TYPE http_requests_total counter") == 1
        assert body.count("# HELP http_requests_total") == 1

    def test_the_prefix_is_applied(self) -> None:
        registry = InMemoryMetricsRegistry()
        registry.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL)

        body = render_prometheus(registry.snapshot(), prefix="legal_platform")
        assert "legal_platform_unhandled_exceptions_total" in body

    def test_the_body_ends_with_a_newline(self) -> None:
        """A scraper is entitled to reject a body without one."""
        registry = InMemoryMetricsRegistry()
        registry.increment(MetricName.UNHANDLED_EXCEPTIONS_TOTAL)

        assert render_prometheus(registry.snapshot()).endswith("\n")

    def test_an_empty_registry_renders_without_failing(self) -> None:
        assert render_prometheus(InMemoryMetricsRegistry().snapshot()) == "\n"


class TestNullRegistry:
    """Recording code stays a plain call."""

    def test_it_accepts_everything_and_reports_nothing(self) -> None:
        registry = NullMetricsRegistry()
        registry.increment(MetricName.HTTP_REQUESTS_TOTAL)
        registry.set_gauge(MetricName.PROCESS_THREADS, 4)
        registry.observe(MetricName.HTTP_REQUEST_DURATION_MS, 1.0)

        assert registry.snapshot().series == ()
