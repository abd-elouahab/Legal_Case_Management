"""Rendering a metrics snapshot into a scraper's exposition format.

``22-monitoring.md`` requires that *"specific monitoring technologies should
remain replaceable"* while naming Prometheus, Grafana, and OpenTelemetry as what
the platform should be ready for. This module is the whole of that readiness, and
its position in the design is the point: **the platform's metrics are declared in
:mod:`core.observability` and recorded in :mod:`services.metrics_registry`,
neither of which knows this file exists.** An exporter is a *renderer over a
snapshot*, so a second one — OpenMetrics, StatsD, a JSON push to a collector — is
a second function here and no change anywhere else.

It is deliberately a **hand-written renderer rather than a client library**. The
text exposition format is a line per series with a documented escaping rule, this
is a hundred lines of it, and `prometheus_client` would have brought its own
global registry, its own metric objects, and its own opinions about types —
which is to say a second source of truth for what a metric *is*, sitting beside
the declarations that already are one. The same reasoning
:mod:`services.email_provider` gives for reaching for ``smtplib`` and
:mod:`services.whatsapp_provider` for ``urllib.request``: the standard thing
speaks the protocol, and the dependency would have bought coupling rather than
capability.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from core.observability import MetricType, MetricUnit
from services.metrics_registry import MetricSeries, MetricsSnapshot

__all__ = ["PROMETHEUS_CONTENT_TYPE", "render_prometheus"]

#: The content type a Prometheus scraper expects. Version-pinned because the
#: scraper negotiates on it, and an unversioned ``text/plain`` is accepted but
#: makes the platform look like something that does not know what it is speaking.
PROMETHEUS_CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"

#: Suffix appended to a millisecond metric when it is converted to seconds.
_SECONDS_SUFFIX: Final[str] = "_seconds"


def render_prometheus(snapshot: MetricsSnapshot, *, prefix: str = "") -> str:
    """Render every series in ``snapshot`` as the Prometheus text format.

    **Milliseconds are converted to seconds**, name and all. Prometheus's own
    conventions are unambiguous that a duration is a float of seconds, and every
    stock dashboard, recording rule, and alert expression assumes it; exporting
    ``..._milliseconds`` would produce charts whose axes are wrong by three orders
    of magnitude in a way nobody notices until an alert does not fire. The
    platform's *internal* unit stays milliseconds, because that is what a person
    reading a JSON monitoring page wants, and the conversion happens exactly here
    — at the boundary, where a unit convention belongs.

    Series are emitted grouped by metric with their ``# HELP`` and ``# TYPE``
    headers once each, which the format requires, and in the snapshot's sorted
    order, so two consecutive scrapes of an unchanged process are byte-identical.
    """
    lines: list[str] = []
    for name, series in _grouped(snapshot.series):
        first = series[0]
        metric_name = _render_name(name, first.unit, prefix)
        lines.append(f"# HELP {metric_name} {_escape_help(first.description)}")
        lines.append(f"# TYPE {metric_name} {_render_type(first.type)}")
        for item in series:
            lines.extend(_render_series(metric_name, item))
    # The format requires a trailing newline; a scraper is entitled to reject a
    # body without one.
    return "\n".join(lines) + "\n"


def _grouped(series: Iterable[MetricSeries]) -> list[tuple[str, list[MetricSeries]]]:
    """Group series by metric name, preserving the snapshot's order."""
    grouped: dict[str, list[MetricSeries]] = {}
    for item in series:
        grouped.setdefault(item.name.value, []).append(item)
    return list(grouped.items())


def _render_name(name: str, unit: MetricUnit, prefix: str) -> str:
    """Apply the deployment's prefix and the unit convention to a metric name."""
    if unit is MetricUnit.MILLISECONDS:
        name = name.removesuffix("_milliseconds") + _SECONDS_SUFFIX
    cleaned_prefix = "".join(char for char in prefix if char.isalnum() or char == "_")
    return f"{cleaned_prefix}_{name}" if cleaned_prefix else name


def _render_type(metric_type: MetricType) -> str:
    """Map a declared type onto the exposition format's own vocabulary."""
    return {
        MetricType.COUNTER: "counter",
        MetricType.GAUGE: "gauge",
        MetricType.HISTOGRAM: "histogram",
    }[metric_type]


def _render_series(metric_name: str, series: MetricSeries) -> list[str]:
    """Render one series: a single line, or a histogram's bucket family."""
    scale = 0.001 if series.unit is MetricUnit.MILLISECONDS else 1.0

    if series.histogram is None:
        value = (series.value or 0.0) * (scale if series.type is not MetricType.COUNTER else 1.0)
        return [f"{metric_name}{_render_labels(series.labels)} {_render_value(value)}"]

    lines: list[str] = []
    histogram = series.histogram
    for bound, cumulative in histogram.buckets:
        labels = (*series.labels, ("le", _render_value(bound * scale)))
        lines.append(f"{metric_name}_bucket{_render_labels(labels)} {cumulative}")
    # The ``+Inf`` bucket is mandatory and must equal the observation count —
    # without it a scraper cannot compute a quantile, because it cannot know how
    # much of the distribution lies above the last boundary.
    lines.append(
        f"{metric_name}_bucket{_render_labels((*series.labels, ('le', '+Inf')))} {histogram.count}"
    )
    lines.append(f"{metric_name}_sum{_render_labels(series.labels)} {_render_value(histogram.sum * scale)}")
    lines.append(f"{metric_name}_count{_render_labels(series.labels)} {histogram.count}")
    return lines


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    """Render a label set, or an empty string when there are none."""
    if not labels:
        return ""
    rendered = ",".join(f'{key}="{_escape_label(value)}"' for key, value in labels)
    return "{" + rendered + "}"


def _escape_label(value: str) -> str:
    """Escape a label value per the exposition format.

    Backslash, double quote, and newline, in that order — the order matters,
    because escaping the backslash after the quote would double-escape the
    backslash the quote's escape introduced. The registry already strips these
    characters when a label is recorded (see
    :func:`~services.metrics_registry._clean_label`), so this is the second of two
    independent defences rather than the only one.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(text: str) -> str:
    """Escape a ``# HELP`` line's text: backslash and newline only."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _render_value(value: float) -> str:
    """Render a number without a trailing ``.0`` on whole values.

    Cosmetic, and worth the four lines: a scrape response is read by people
    during an incident as often as by a scraper, and ``http_requests_total 41`` is
    easier to scan than ``41.0`` across a hundred lines.
    """
    if value != value or value in (float("inf"), float("-inf")):  # NaN / infinity
        return "0"
    if float(value).is_integer():
        return str(int(value))
    return repr(round(float(value), 6))
