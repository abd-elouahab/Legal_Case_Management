"""The platform's metric registry: counters, gauges, and histograms.

The shape eleven features already use — a protocol, an in-memory implementation,
a null implementation, and a frozen snapshot — applied one level up. Where
:mod:`services.search_metrics` and its nine siblings each hold *their feature's*
figures in named fields, this holds **arbitrary declared series** and is what the
cross-cutting instrumentation records into: HTTP, the database, background jobs,
errors, security, and the process itself.

**It does not replace those recorders and does not read them.** ``22-monitoring.md``
forbids duplicate metric collection, and the ten existing recorders are the
platform's answer for their own features — a request handler that recorded a RAG
latency here as well would produce two numbers for one question that drift apart
the first time one call site moves. :class:`~services.monitoring.MonitoringService`
reads both and reports them side by side; nothing writes to both.

**Why in-process, and what that costs.** Same reasoning
:mod:`services.event_metrics` gives, and it applies more strongly here: a row per
HTTP request is write amplification proportional to traffic, to store something
that is a *rate* rather than a fact. So the counters live in the process,
``since`` is the honest caveat, and each API instance counts its own work. That
is precisely why :class:`MetricsRegistry` is a protocol — a StatsD or Prometheus
push-gateway backend is one class plus one line in :mod:`api.deps`, and the
:mod:`services.metrics_export` renderer already turns a snapshot into the text a
scraper wants.

**Cardinality is bounded by construction, in three ways**, because an unbounded
metric registry is a memory leak with a chart on it:

* a series may only carry the labels its :class:`~core.observability.
  MetricDefinition` declares — anything else is dropped rather than stored;
* a label *value* is bounded in length and stripped of anything that is not
  printable, so a stray identifier cannot be a hundred-character series name;
* the total number of series is capped, and what the cap refuses is **counted**
  (:attr:`~core.observability.MetricName.METRIC_SERIES_DROPPED_TOTAL`) rather
  than silently discarded — a monitoring page that is quietly incomplete is worse
  than one that says so.

**Nothing here can hold anything identifying**, and it is structural rather than
a matter of care: every method takes a declared :class:`MetricName`, a number,
and labels that are filtered against a closed list. There is no parameter for a
user, a case, a document, or a body.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from core.observability import (
    METRICS,
    MetricDefinition,
    MetricName,
    MetricType,
    MetricUnit,
    MonitoringComponent,
    buckets_for,
)

__all__ = [
    "HistogramValue",
    "InMemoryMetricsRegistry",
    "MetricSeries",
    "MetricsRegistry",
    "MetricsSnapshot",
    "NullMetricsRegistry",
    "get_metrics_registry",
    "reset_metrics_registry",
]

#: Longest a label value may be. Long enough for a route template
#: (``/api/v1/documents/{document_id}/index``), far too short for anything a
#: person typed.
_MAX_LABEL_LENGTH: Final[int] = 120

#: Series the registry holds before it starts refusing new ones. Generous
#: relative to what the declarations can actually produce — the routes times the
#: methods times five status classes is the dominant term — and the point of it
#: is to bound a *bug*, not the design.
_DEFAULT_MAX_SERIES: Final[int] = 4_096

#: The label key/value pairs identifying one series, sorted for stability.
type LabelSet = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class HistogramValue:
    """An accumulated distribution.

    Sums and bucket counts rather than the observations themselves, for the
    reason :class:`~services.event_metrics.InMemoryRealtimeMetrics` records:
    constant memory, and a recorder that grows with traffic is a leak. The trade
    is that quantiles are *estimated from buckets* rather than exact — see
    :meth:`quantile`, which says so rather than implying precision it does not
    have.
    """

    count: int
    sum: float
    minimum: float | None
    maximum: float | None
    #: Cumulative counts, one per boundary in
    #: :func:`~core.observability.buckets_for`, in ascending order. Cumulative
    #: (each bucket counts everything at or below its bound) because that is what
    #: every exposition format and every quantile estimator expects, and deriving
    #: it once here is cheaper than every reader deriving it wrongly.
    buckets: tuple[tuple[float, int], ...]

    @property
    def average(self) -> float | None:
        """Mean observation, or ``None`` when there have been none.

        ``None`` rather than ``0.0``, for the reason every snapshot on this
        platform gives: an average over no observations is undefined, while zero
        reads as "instantaneous", which is a very different claim.
        """
        if self.count <= 0:
            return None
        return round(self.sum / self.count, 3)

    def quantile(self, fraction: float) -> float | None:
        """Estimate a quantile from the bucket counts.

        Linear interpolation inside the bucket the quantile falls in, which is
        the standard estimate and is accurate to the width of that bucket —
        which is why :data:`~core.observability.LATENCY_BUCKETS_MS` places its
        boundaries where this platform's interesting decisions sit rather than on
        a generic ladder.

        Returns ``None`` when nothing has been observed, and the largest boundary
        when the quantile falls past the last bucket — an observation above every
        boundary is known only to be above it, and inventing a number for it
        would be the fabrication a monitoring view least needs.
        """
        if self.count <= 0 or not self.buckets:
            return None

        target = fraction * self.count
        previous_bound = 0.0
        previous_count = 0
        for bound, cumulative in self.buckets:
            if cumulative >= target:
                span = cumulative - previous_count
                if span <= 0:
                    return round(bound, 3)
                position = (target - previous_count) / span
                return round(previous_bound + (bound - previous_bound) * position, 3)
            previous_bound, previous_count = bound, cumulative

        return round(self.buckets[-1][0], 3)


@dataclass(frozen=True, slots=True)
class MetricSeries:
    """One metric at one label combination, at one instant."""

    name: MetricName
    type: MetricType
    unit: MetricUnit
    component: MonitoringComponent
    description: str
    labels: LabelSet
    #: Set for counters and gauges; ``None`` for histograms.
    value: float | None = None
    #: Set for histograms; ``None`` otherwise.
    histogram: HistogramValue | None = None

    @property
    def label_map(self) -> dict[str, str]:
        """The labels as a mapping, for serialization."""
        return dict(self.labels)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Every series the registry holds, at one instant.

    Frozen and taken under the lock, for the reason every other snapshot on this
    platform is: a page whose figures contradict each other is worse than one
    that is a second stale.
    """

    #: When this process started counting.
    since: datetime
    taken_at: datetime
    series: tuple[MetricSeries, ...]
    #: Series refused because the cardinality ceiling was reached. Non-zero means
    #: some figure on the page is incomplete, and is the first thing to look at.
    dropped_series: int

    def by_name(self, name: MetricName) -> tuple[MetricSeries, ...]:
        """Every series recorded under one metric name."""
        return tuple(item for item in self.series if item.name is name)

    def total(self, name: MetricName) -> float:
        """Sum a counter or gauge across its label combinations.

        Summing a **gauge** across labels is meaningful here and only here
        because the platform's gauges are either unlabelled (uptime, threads,
        connections in flight) or partition a whole (queue depth by queue). A
        caller summing something else gets what they asked for; the declaration
        is the place that says what a series means.
        """
        return round(sum(item.value or 0.0 for item in self.by_name(name)), 3)

    def histogram(self, name: MetricName) -> HistogramValue | None:
        """Merge every label combination of a histogram into one distribution.

        Merging is exact rather than approximate — cumulative bucket counts add,
        and so do sums and counts — which is the property that makes bucketed
        histograms the right structure for a platform that will one day have more
        than one API instance.
        """
        parts = [item.histogram for item in self.by_name(name) if item.histogram is not None]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]

        bounds = [bound for bound, _ in parts[0].buckets]
        merged = [
            (bound, sum(part.buckets[index][1] for part in parts))
            for index, bound in enumerate(bounds)
        ]
        minimums = [part.minimum for part in parts if part.minimum is not None]
        maximums = [part.maximum for part in parts if part.maximum is not None]
        return HistogramValue(
            count=sum(part.count for part in parts),
            sum=round(sum(part.sum for part in parts), 3),
            minimum=min(minimums) if minimums else None,
            maximum=max(maximums) if maximums else None,
            buckets=tuple(merged),
        )


class MetricsRegistry(Protocol):
    """What the instrumentation requires of a metrics backend.

    Four methods, and none of them accepts a user, a case, a document, a query,
    or a body. A registry that *cannot be handed* an identifier cannot leak one —
    the same structural argument every recorder on this platform makes, stated
    once more because this is the recorder the most code touches.
    """

    def increment(
        self,
        name: MetricName,
        *,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Add to a counter. Negative values are ignored, never subtracted."""
        ...

    def set_gauge(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Record a gauge's current reading, replacing the previous one."""
        ...

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Add one observation to a histogram."""
        ...

    def snapshot(self) -> MetricsSnapshot:
        """Read every series as one consistent value."""
        ...


class InMemoryMetricsRegistry:
    """Process-local series, guarded by a lock.

    A lock rather than atomics for the reason every recorder here gives: a
    *snapshot* has to be internally consistent, and a hundred separately-updated
    series read without one can report more requests than responses. The critical
    sections are a dictionary lookup and an addition, on paths that are already
    doing socket I/O and JSON encoding, so contention is not a consideration.

    **Every public method swallows its own failures.** ``22-monitoring.md``:
    *"monitoring failures must never interrupt business operations"*. A metric
    name with no declaration, a value that is not a number, a label that is not a
    string — none of them raises out of here, because the call site is a
    ``finally`` in a request handler and an exception there would turn an
    observation into an outage.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self, *, max_series: int = _DEFAULT_MAX_SERIES) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._max_series = max(1, max_series)
        self._counters: dict[tuple[MetricName, LabelSet], float] = {}
        self._gauges: dict[tuple[MetricName, LabelSet], float] = {}
        self._histograms: dict[tuple[MetricName, LabelSet], _HistogramAccumulator] = {}
        self._dropped = 0

    # ------------------------------------------------------------- recording #

    def increment(
        self,
        name: MetricName,
        *,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Add to a counter, ignoring anything that is not a positive number.

        A counter that could go down would make every rate computed from it
        wrong, and the usual cause of a negative increment is a caller that meant
        a gauge — so it is dropped rather than applied, and the declaration is
        what says which of the two a metric is.
        """
        try:
            definition = self._checked(name, MetricType.COUNTER)
            if definition is None or value <= 0:
                return
            key = (name, self._labels(definition, labels))
            with self._lock:
                if key not in self._counters and not self._has_room(len(self._counters)):
                    self._dropped += 1
                    return
                self._counters[key] = self._counters.get(key, 0.0) + float(value)
        except Exception:  # pragma: no cover - defensive; see the class docstring
            return

    def set_gauge(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Replace a gauge's reading."""
        try:
            definition = self._checked(name, MetricType.GAUGE)
            if definition is None:
                return
            key = (name, self._labels(definition, labels))
            with self._lock:
                if key not in self._gauges and not self._has_room(len(self._gauges)):
                    self._dropped += 1
                    return
                self._gauges[key] = float(value)
        except Exception:  # pragma: no cover - defensive
            return

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Add one observation to a histogram.

        Negative observations are clamped to zero rather than dropped: every
        histogram here measures a duration or a size, a negative one means a
        clock went backwards mid-measurement (which happens), and losing the
        observation would understate throughput while clamping only understates
        that one latency.
        """
        try:
            definition = self._checked(name, MetricType.HISTOGRAM)
            if definition is None:
                return
            key = (name, self._labels(definition, labels))
            with self._lock:
                accumulator = self._histograms.get(key)
                if accumulator is None:
                    if not self._has_room(len(self._histograms)):
                        self._dropped += 1
                        return
                    accumulator = _HistogramAccumulator(buckets_for(definition))
                    self._histograms[key] = accumulator
                accumulator.observe(max(0.0, float(value)))
        except Exception:  # pragma: no cover - defensive
            return

    # -------------------------------------------------------------- snapshot #

    def snapshot(self) -> MetricsSnapshot:
        """Read every series as one consistent value."""
        with self._lock:
            series: list[MetricSeries] = []

            for (name, labels), value in self._counters.items():
                series.append(self._series(name, labels, value=round(value, 3)))
            for (name, labels), value in self._gauges.items():
                series.append(self._series(name, labels, value=round(value, 3)))
            for (name, labels), accumulator in self._histograms.items():
                series.append(self._series(name, labels, histogram=accumulator.value()))

            dropped = self._dropped
            since = self._since

        # Sorted outside the lock: a stable order makes two snapshots diffable and
        # keeps an exposition endpoint's output byte-comparable between scrapes.
        series.sort(key=lambda item: (item.name.value, item.labels))
        return MetricsSnapshot(
            since=since,
            taken_at=datetime.now(UTC),
            series=tuple(series),
            dropped_series=dropped,
        )

    def reset(self) -> None:
        """Discard every series.

        For tests, and for an operator who wants a fresh window. Not called by
        the application — the counters are the process's history, and clearing
        them on a schedule would make ``since`` a lie.
        """
        with self._lock:
            self._since = datetime.now(UTC)
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._dropped = 0

    # --------------------------------------------------------------- helpers #

    @staticmethod
    def _checked(name: MetricName, expected: MetricType) -> MetricDefinition | None:
        """Return the declaration, or ``None`` if this operation does not fit it.

        A counter recorded as a gauge is a bug at the call site, and the response
        is to drop the observation rather than to raise: the caller is a
        ``finally`` block, and the declaration in :mod:`core.observability` is the
        single source of truth about what a metric is.
        """
        definition = METRICS.get(name)
        if definition is None or definition.type is not expected:
            return None
        return definition

    @staticmethod
    def _labels(definition: MetricDefinition, labels: Mapping[str, str] | None) -> LabelSet:
        """Filter, clean, and order the labels for one observation.

        Undeclared labels are **dropped rather than rejected**, which is the
        deliberate half of this: an observation with a stray label is still a real
        observation, and losing it would make a metric wrong to punish a typo.
        What the drop protects is cardinality — the label cannot create a series.
        """
        if not definition.labels:
            return ()
        if not labels:
            return ()

        allowed = set(definition.labels)
        cleaned = {
            key: _clean_label(value)
            for key, value in labels.items()
            if key in allowed and value is not None
        }
        return tuple(sorted(cleaned.items()))

    def _has_room(self, current: int) -> bool:
        """Whether another series may be created in a dictionary of this size.

        Called with the lock held. The ceiling is applied per dictionary rather
        than across all three, which is intentional: the three are populated by
        unrelated instrumentation, and a burst of counter cardinality must not be
        able to stop histograms from being recorded at all.
        """
        return current < self._max_series

    @staticmethod
    def _series(
        name: MetricName,
        labels: LabelSet,
        *,
        value: float | None = None,
        histogram: HistogramValue | None = None,
    ) -> MetricSeries:
        """Attach a metric's declaration to one recorded series."""
        definition = METRICS[name]
        return MetricSeries(
            name=name,
            type=definition.type,
            unit=definition.unit,
            component=definition.component,
            description=definition.description,
            labels=labels,
            value=value,
            histogram=histogram,
        )


def _clean_label(value: object) -> str:
    """Render a label value as bounded, printable, single-line text.

    Label values reach a log line, a JSON response, and a text exposition
    endpoint whose format is newline-delimited — so a value containing a newline
    is a forged metric, exactly as a value containing a newline in a log line is a
    forged entry. Everything unprintable goes, and the result is truncated.
    """
    text = str(value)
    cleaned = "".join(char if char.isprintable() and char not in '\r\n"\\' else "_" for char in text)
    if len(cleaned) > _MAX_LABEL_LENGTH:
        cleaned = cleaned[:_MAX_LABEL_LENGTH]
    return cleaned or "unknown"


class _HistogramAccumulator:
    """Running totals for one histogram series.

    Not frozen and not shared: it lives inside the registry's lock, and
    :meth:`value` produces the frozen :class:`HistogramValue` a snapshot carries.
    """

    __slots__ = ("_bounds", "_count", "_counts", "_max", "_min", "_sum")

    def __init__(self, bounds: tuple[float, ...]) -> None:
        self._bounds = bounds
        self._counts = [0] * len(bounds)
        self._count = 0
        self._sum = 0.0
        self._min: float | None = None
        self._max: float | None = None

    def observe(self, value: float) -> None:
        """Fold one observation in."""
        self._count += 1
        self._sum += value
        self._min = value if self._min is None else min(self._min, value)
        self._max = value if self._max is None else max(self._max, value)
        for index, bound in enumerate(self._bounds):
            if value <= bound:
                self._counts[index] += 1

    def value(self) -> HistogramValue:
        """Freeze the accumulator into a snapshot value."""
        return HistogramValue(
            count=self._count,
            sum=round(self._sum, 3),
            minimum=self._min,
            maximum=self._max,
            buckets=tuple(zip(self._bounds, self._counts, strict=True)),
        )


class NullMetricsRegistry:
    """A registry that records nothing.

    The default for code constructed without observability — a management script,
    a unit test that is not about metrics. Same role and reasoning as
    :class:`~services.search_metrics.NullSearchMetrics`: recording stays a plain
    call with no ``if self._metrics`` guard at every site, which is what keeps
    instrumentation from being something a reader has to skip over.
    """

    def increment(
        self,
        name: MetricName,
        *,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Discard the observation."""

    def set_gauge(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Discard the observation."""

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Discard the observation."""

    def snapshot(self) -> MetricsSnapshot:
        """Report an empty window."""
        now = datetime.now(UTC)
        return MetricsSnapshot(since=now, taken_at=now, series=(), dropped_series=0)


#: The one registry the process shares.
#:
#: Module-level for the reason every recorder on this platform is: a per-request
#: registry would count to one and reset, and these are properties of the
#: *process*.
_shared = InMemoryMetricsRegistry()


def get_metrics_registry() -> MetricsRegistry:
    """Return the process-wide metric registry."""
    return _shared


def reset_metrics_registry() -> None:
    """Clear every series. For tests."""
    _shared.reset()
