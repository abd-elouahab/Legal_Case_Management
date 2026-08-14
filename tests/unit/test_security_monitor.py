"""Unit tests for security monitoring.

The claim this module exists to keep is a privacy one: ``22-monitoring.md`` asks
for failed logins and repeated denials to be watched *"without exposing sensitive
information"*, and the monitor is handed a client address in order to count
distinct sources. **That address must be unrecoverable from everything the monitor
can produce**, and the tests below are what says so.

Everything else here is arithmetic: the counters, the three windows, and the
denominator without which a failure count cannot be read.
"""

from __future__ import annotations

from core.observability import MetricName, SecurityEventType, SecuritySeverity
from services.metrics_registry import InMemoryMetricsRegistry
from services.security_monitor import InMemorySecurityMonitor, NullSecurityMonitor

ADDRESS = "203.0.113.7"


class TestCounting:
    """The five things the spec's Security Monitoring section names."""

    def test_a_fresh_monitor_reports_an_empty_window(self) -> None:
        snapshot = InMemorySecurityMonitor().snapshot()

        assert snapshot.total_events == 0
        assert snapshot.events_by_type == {}
        assert snapshot.recent == ()

    def test_it_counts_by_type_and_severity(self) -> None:
        monitor = InMemorySecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_FAILED)
        monitor.record(SecurityEventType.LOGIN_FAILED)
        monitor.record(SecurityEventType.PERMISSION_DENIED)

        snapshot = monitor.snapshot()
        assert snapshot.total_events == 3
        assert snapshot.events_by_type["login_failed"] == 2
        assert snapshot.events_by_severity[SecuritySeverity.WARNING.value] == 3

    def test_a_severity_is_assigned_rather_than_chosen_at_the_call_site(self) -> None:
        monitor = InMemorySecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_LOCKED_OUT)

        assert monitor.snapshot().recent[0].severity is SecuritySeverity.CRITICAL

    def test_the_denominator_is_recorded_beside_the_failures(self) -> None:
        """Fifty failures out of fifty is an attack; out of fifty thousand is a Monday."""
        monitor = InMemorySecurityMonitor()
        for _ in range(3):
            monitor.record(SecurityEventType.LOGIN_SUCCEEDED)
        monitor.record(SecurityEventType.LOGIN_FAILED)

        snapshot = monitor.snapshot()
        assert snapshot.login_attempts == 4
        assert snapshot.failed_logins == 1
        assert snapshot.login_failure_rate == 25.0

    def test_a_rate_over_nothing_is_zero_rather_than_undefined(self) -> None:
        """The alternative reading opens a quiet morning's page at 100 %."""
        assert InMemorySecurityMonitor().snapshot().login_failure_rate == 0.0


class TestWindows:
    """Rates, because a total since startup is unreadable."""

    def test_it_reports_three_windows_per_event_type(self) -> None:
        monitor = InMemorySecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_FAILED)

        rates = monitor.snapshot().recent_rates["login_failed"]
        assert rates == {"1m": 1, "5m": 1, "15m": 1}

    def test_an_event_type_that_has_not_happened_has_no_window(self) -> None:
        monitor = InMemorySecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_FAILED)

        assert "rate_limited" not in monitor.snapshot().recent_rates


class TestPrivacy:
    """The address goes in; nothing that can name it comes out."""

    def test_the_address_never_appears_in_the_feed(self) -> None:
        monitor = InMemorySecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_FAILED, source=ADDRESS)

        record = monitor.snapshot().recent[0]
        assert ADDRESS not in str(record)
        assert record.source is not None
        assert record.source != ADDRESS

    def test_the_same_source_correlates_without_being_named(self) -> None:
        monitor = InMemorySecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_FAILED, source=ADDRESS)
        monitor.record(SecurityEventType.LOGIN_FAILED, source=ADDRESS)

        snapshot = monitor.snapshot()
        assert snapshot.recent[0].source == snapshot.recent[1].source
        assert snapshot.distinct_sources == 1

    def test_two_sources_are_distinguishable(self) -> None:
        monitor = InMemorySecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_FAILED, source=ADDRESS)
        monitor.record(SecurityEventType.LOGIN_FAILED, source="198.51.100.4")

        assert monitor.snapshot().distinct_sources == 2

    def test_the_digest_differs_between_processes(self) -> None:
        """A salt shared across instances would make these a real identifier."""
        first = InMemorySecurityMonitor()
        second = InMemorySecurityMonitor()
        first.record(SecurityEventType.LOGIN_FAILED, source=ADDRESS)
        second.record(SecurityEventType.LOGIN_FAILED, source=ADDRESS)

        assert first.snapshot().recent[0].source != second.snapshot().recent[0].source

    def test_no_account_is_ever_recorded(self) -> None:
        """The recorder has nowhere to put one: there is no such parameter."""
        monitor = InMemorySecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_FAILED, role="lawyer", reason="invalid_credentials")

        record = monitor.snapshot().recent[0]
        assert record.role == "lawyer"
        assert not hasattr(record, "user_id")
        assert not hasattr(record, "email")

    def test_the_distinct_source_count_is_capped_and_says_so(self) -> None:
        """A distributed attempt must not make this set grow with the attack."""
        monitor = InMemorySecurityMonitor()
        monitor._sources = {index.to_bytes(16, "big") for index in range(4_096)}
        monitor.record(SecurityEventType.LOGIN_FAILED, source="new-source")

        snapshot = monitor.snapshot()
        assert snapshot.sources_capped is True


class TestFeed:
    """Bounded, newest first."""

    def test_it_keeps_the_newest_and_drops_the_oldest(self) -> None:
        monitor = InMemorySecurityMonitor(recent_size=2)
        for reason in ("first", "second", "third"):
            monitor.record(SecurityEventType.LOGIN_FAILED, reason=reason)

        reasons = [record.reason for record in monitor.snapshot().recent]
        assert reasons == ["third", "second"]


class TestMetrics:
    """Derived counters, so a stock alert rule finds what it expects."""

    def test_a_failed_sign_in_reaches_the_dedicated_counters(self) -> None:
        registry = InMemoryMetricsRegistry()
        monitor = InMemorySecurityMonitor(metrics=registry)
        monitor.record(SecurityEventType.LOGIN_FAILED, reason="invalid_credentials")

        snapshot = registry.snapshot()
        assert snapshot.total(MetricName.SECURITY_EVENTS_TOTAL) == 1.0
        assert snapshot.total(MetricName.AUTH_LOGIN_ATTEMPTS_TOTAL) == 1.0
        assert snapshot.total(MetricName.AUTH_LOGIN_FAILURES_TOTAL) == 1.0

    def test_a_denial_reaches_the_authorization_counter(self) -> None:
        registry = InMemoryMetricsRegistry()
        monitor = InMemorySecurityMonitor(metrics=registry)
        monitor.record(SecurityEventType.PERMISSION_DENIED, role="court_representative")

        assert registry.snapshot().total(MetricName.AUTHORIZATION_DENIALS_TOTAL) == 1.0

    def test_a_successful_sign_in_is_an_attempt_but_not_a_failure(self) -> None:
        registry = InMemoryMetricsRegistry()
        monitor = InMemorySecurityMonitor(metrics=registry)
        monitor.record(SecurityEventType.LOGIN_SUCCEEDED)

        snapshot = registry.snapshot()
        assert snapshot.total(MetricName.AUTH_LOGIN_ATTEMPTS_TOTAL) == 1.0
        assert snapshot.total(MetricName.AUTH_LOGIN_FAILURES_TOTAL) == 0.0


class TestNullMonitor:
    """Recording code stays a plain call."""

    def test_it_accepts_everything_and_reports_nothing(self) -> None:
        monitor = NullSecurityMonitor()
        monitor.record(SecurityEventType.LOGIN_FAILED, source=ADDRESS)

        assert monitor.snapshot().total_events == 0
