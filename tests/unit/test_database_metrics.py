"""Unit tests for the SQLAlchemy engine instrumentation.

Tested here rather than through the API, deliberately: the integration harness
substitutes its own SQLite engine for the application's, so a database span is
invisible from that side. What matters about this module is a property of the
*listener* — that it times every statement, records the verb and nothing else,
attaches to the current trace, and can be attached and removed without leaving a
second set of listeners behind — and all four are visible against any engine.

The claim it exists to keep is the one in its module docstring: **the statement
text is never recorded.** A statement embeds no parameters on this platform, but
it still names tables and columns, and a slow-query log that quotes one is one
edit away from quoting the parameters too.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from core.observability import MetricName, MonitoringComponent
from core.tracing import SpanKind
from services.database_metrics import (
    instrument_engine,
    is_instrumented,
    record_pool_gauges,
    uninstrument_engine,
)
from services.error_tracker import InMemoryErrorTracker
from services.metrics_registry import InMemoryMetricsRegistry
from services.tracer import InMemoryTracer


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A throwaway in-memory engine, uninstrumented on the way out."""
    created = create_engine("sqlite://", future=True)
    try:
        yield created
    finally:
        uninstrument_engine(created)
        created.dispose()


@pytest.fixture
def registry() -> InMemoryMetricsRegistry:
    return InMemoryMetricsRegistry()


class TestAttachment:
    """Idempotent, reversible, and never fatal."""

    def test_it_attaches(self, engine: Engine, registry: InMemoryMetricsRegistry) -> None:
        assert instrument_engine(engine, metrics=registry) is True
        assert is_instrumented(engine) is True

    def test_attaching_twice_is_a_no_op(
        self, engine: Engine, registry: InMemoryMetricsRegistry
    ) -> None:
        """A second set of listeners would silently double every count."""
        instrument_engine(engine, metrics=registry)
        assert instrument_engine(engine, metrics=registry) is False

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        assert registry.snapshot().total(MetricName.DB_QUERIES_TOTAL) == 1.0

    def test_it_can_be_removed(self, engine: Engine, registry: InMemoryMetricsRegistry) -> None:
        instrument_engine(engine, metrics=registry)
        uninstrument_engine(engine)

        assert is_instrumented(engine) is False
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        assert registry.snapshot().total(MetricName.DB_QUERIES_TOTAL) == 0.0

    def test_it_declines_when_the_feature_is_switched_off(
        self, engine: Engine, registry: InMemoryMetricsRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config import settings

        monkeypatch.setattr(settings, "MONITORING_DB_INSTRUMENTATION", False)

        assert instrument_engine(engine, metrics=registry) is False


class TestMeasurement:
    """The verb, the duration, and nothing else."""

    def test_it_counts_and_times_a_statement(
        self, engine: Engine, registry: InMemoryMetricsRegistry
    ) -> None:
        instrument_engine(engine, metrics=registry)

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        snapshot = registry.snapshot()
        assert snapshot.total(MetricName.DB_QUERIES_TOTAL) == 1.0
        histogram = snapshot.histogram(MetricName.DB_QUERY_DURATION_MS)
        assert histogram is not None
        assert histogram.count == 1

    def test_it_labels_by_verb(self, engine: Engine, registry: InMemoryMetricsRegistry) -> None:
        instrument_engine(engine, metrics=registry)

        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE t (id INTEGER)"))
            connection.execute(text("INSERT INTO t VALUES (1)"))
            connection.execute(text("SELECT * FROM t"))

        operations = {
            series.label_map["operation"]
            for series in registry.snapshot().by_name(MetricName.DB_QUERIES_TOTAL)
        }
        assert "SELECT" in operations
        assert "INSERT" in operations

    def test_an_unrecognised_statement_is_named_other_rather_than_itself(
        self, engine: Engine, registry: InMemoryMetricsRegistry
    ) -> None:
        """A bounded label cannot be widened by a statement."""
        instrument_engine(engine, metrics=registry)

        with engine.begin() as connection:
            connection.execute(text("PRAGMA user_version"))

        operations = {
            series.label_map["operation"]
            for series in registry.snapshot().by_name(MetricName.DB_QUERIES_TOTAL)
        }
        assert operations <= {"SELECT", "INSERT", "UPDATE", "DELETE", "COMMIT", "BEGIN", "other"}

    def test_the_statement_text_is_never_recorded(
        self, engine: Engine, registry: InMemoryMetricsRegistry
    ) -> None:
        instrument_engine(engine, metrics=registry)

        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE secrets (client_name TEXT)"))
            connection.execute(text("SELECT client_name FROM secrets"))

        rendered = str(registry.snapshot())
        assert "client_name" not in rendered
        assert "secrets" not in rendered


class TestTracing:
    """The database appears on a request's path."""

    def test_a_statement_becomes_a_span_inside_the_current_trace(
        self, engine: Engine, registry: InMemoryMetricsRegistry
    ) -> None:
        tracer = InMemoryTracer(metrics=registry)
        instrument_engine(engine, metrics=registry, tracer=tracer)

        with (
            tracer.span("GET /cases", component=MonitoringComponent.API, kind=SpanKind.SERVER),
            engine.connect() as connection,
        ):
            connection.execute(text("SELECT 1"))

        trace = tracer.snapshot().recent[0]
        components = {span.component for span in trace.spans}
        assert MonitoringComponent.DATABASE in components

    def test_a_statement_outside_a_request_still_times_but_invents_no_trace(
        self, engine: Engine, registry: InMemoryMetricsRegistry
    ) -> None:
        tracer = InMemoryTracer(metrics=registry)
        instrument_engine(engine, metrics=registry, tracer=tracer)

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        assert tracer.snapshot().traces_recorded == 0
        assert registry.snapshot().total(MetricName.DB_QUERIES_TOTAL) == 1.0


class TestFailures:
    """A statement that raises is counted and re-raised unchanged."""

    def test_it_records_and_does_not_suppress(
        self, engine: Engine, registry: InMemoryMetricsRegistry
    ) -> None:
        tracker = InMemoryErrorTracker(metrics=registry)
        instrument_engine(engine, metrics=registry, errors=tracker)

        with (
            pytest.raises(Exception),  # noqa: B017 - the driver's own error is the point
            engine.connect() as connection,
        ):
            connection.execute(text("SELECT * FROM table_that_does_not_exist"))

        assert registry.snapshot().total(MetricName.DB_QUERY_ERRORS_TOTAL) == 1.0
        assert tracker.snapshot().total_errors == 1


class TestPoolGauges:
    """Read when a page is assembled, never on a timer."""

    def test_it_reads_the_pool_without_raising(
        self, engine: Engine, registry: InMemoryMetricsRegistry
    ) -> None:
        record_pool_gauges(engine, registry)

        names = {series.name for series in registry.snapshot().series}
        assert names <= {MetricName.DB_POOL_CHECKED_OUT, MetricName.DB_POOL_CHECKED_IN}
