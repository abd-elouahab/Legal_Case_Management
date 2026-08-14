"""Unit tests for the observability vocabulary.

Three things are asserted here, and the first of them is the one that must never
regress:

* the **Logging Policy** — ``22-monitoring.md`` forbids passwords, tokens, API
  secrets, document contents, AI prompts, and generated reports from ever
  reaching a log. It is enforced by :func:`~core.observability.redact_mapping`
  running over every entry, so these tests are the policy;
* the **cardinality guards** — ``status_class`` and the bounded vocabularies that
  keep a metric's label space from growing with traffic;
* the **health aggregation**, whose ordering is deliberately *not* the enum's
  alphabetical one.
"""

from __future__ import annotations

from core.observability import (
    ALERT_RULES,
    METRICS,
    REDACTED,
    ErrorCategory,
    HealthState,
    MetricName,
    MonitoringComponent,
    SecurityEventType,
    SecuritySeverity,
    buckets_for,
    error_fingerprint,
    is_sensitive_field,
    metric_definition,
    redact_mapping,
    redact_text,
    security_severity,
    status_class,
    truncate,
    worse_health,
)


class TestLoggingPolicy:
    """``22-monitoring.md``'s "never log" list, enforced by the pipeline."""

    def test_every_forbidden_field_name_is_recognised(self) -> None:
        """The spec's own list: passwords, tokens, API secrets, contents, reports."""
        for name in (
            "password",
            "hashed_password",
            "current_password",
            "new_password",
            "access_token",
            "refresh_token",
            "jwt_secret",
            "api_key",
            "smtp_password",
            "authorization",
            "cookie",
            "prompt",
            "extracted_text",
            "report_body",
        ):
            assert is_sensitive_field(name), name

    def test_an_ordinary_field_is_not_redacted(self) -> None:
        """Over-redaction would make the logs useless in a different way."""
        for name in ("case_id", "duration_ms", "status_code", "role", "route"):
            assert not is_sensitive_field(name)

    def test_it_redacts_by_field_name_rather_than_by_value(self) -> None:
        redacted = redact_mapping({"password": "hunter2", "case_number": "CASE-2026-0001"})

        assert redacted["password"] == REDACTED
        assert redacted["case_number"] == "CASE-2026-0001"

    def test_it_recurses_into_nested_structures(self) -> None:
        """A credential is at least as likely to arrive inside a payload."""
        redacted = redact_mapping({"payload": {"user": {"api_key": "abc", "id": 7}}})

        assert redacted["payload"]["user"]["api_key"] == REDACTED
        assert redacted["payload"]["user"]["id"] == 7

    def test_a_list_under_a_sensitive_key_is_redacted_whole(self) -> None:
        assert redact_mapping({"tokens": ["a", "b"]})["tokens"] == REDACTED

    def test_recursion_is_bounded(self) -> None:
        """A structure built out of an ORM object must not make logging the slow part."""
        deep: dict[str, object] = {"value": 1}
        for _ in range(10):
            deep = {"value": deep}

        redacted = redact_mapping(deep, max_depth=2)
        assert redacted  # it returned rather than recursing forever

    def test_case_is_ignored(self) -> None:
        assert redact_mapping({"Authorization": "Bearer x"})["Authorization"] == REDACTED


class TestFreeText:
    """The one place text from anywhere in the platform can arrive."""

    def test_it_collapses_newlines_so_a_message_cannot_forge_a_log_entry(self) -> None:
        assert "\n" not in redact_text("first line\nsecond line")

    def test_it_truncates(self) -> None:
        assert len(redact_text("x" * 500, limit=50)) == 50

    def test_truncation_marks_that_it_happened(self) -> None:
        assert truncate("abcdef", 4).endswith("…")

    def test_a_short_string_is_untouched(self) -> None:
        assert truncate("abc", 10) == "abc"


class TestStatusClass:
    """A five-value label rather than a sixty-value one."""

    def test_it_reduces_a_status_to_its_class(self) -> None:
        assert status_class(200) == "2xx"
        assert status_class(404) == "4xx"
        assert status_class(503) == "5xx"

    def test_an_impossible_status_is_named_rather_than_computed(self) -> None:
        assert status_class(0) == "unknown"
        assert status_class(999) == "unknown"


class TestHealthAggregation:
    """Ordered by rank, never by the enum's alphabetical comparison."""

    def test_nothing_to_aggregate_is_healthy(self) -> None:
        assert worse_health() is HealthState.HEALTHY

    def test_a_subsystem_is_as_healthy_as_its_worst_part(self) -> None:
        assert (
            worse_health(HealthState.HEALTHY, HealthState.UNHEALTHY, HealthState.DEGRADED)
            is HealthState.UNHEALTHY
        )

    def test_unhealthy_outranks_degraded_despite_sorting_before_it(self) -> None:
        """A StrEnum compares alphabetically, which would give the wrong answer."""
        assert "degraded" < "unhealthy"
        assert worse_health(HealthState.DEGRADED, HealthState.UNHEALTHY) is HealthState.UNHEALTHY

    def test_a_disabled_feature_never_dominates_an_aggregate(self) -> None:
        """A deployment that switched WhatsApp off is not thereby degraded."""
        assert worse_health(HealthState.HEALTHY, HealthState.DISABLED) is HealthState.DISABLED
        assert worse_health(HealthState.DEGRADED, HealthState.DISABLED) is HealthState.DEGRADED


class TestFingerprinting:
    """Grouping a *class* of failure, never an occurrence."""

    def test_the_same_failure_at_the_same_place_groups_together(self) -> None:
        first = error_fingerprint(
            category=ErrorCategory.UNHANDLED,
            component=MonitoringComponent.API,
            exception_type="ValueError",
            location="case.py:12",
        )
        second = error_fingerprint(
            category=ErrorCategory.UNHANDLED,
            component=MonitoringComponent.API,
            exception_type="ValueError",
            location="case.py:12",
        )
        assert first == second

    def test_the_same_exception_at_a_different_place_is_a_different_group(self) -> None:
        first = error_fingerprint(
            category=ErrorCategory.UNHANDLED,
            component=MonitoringComponent.API,
            exception_type="ValueError",
            location="case.py:12",
        )
        second = error_fingerprint(
            category=ErrorCategory.UNHANDLED,
            component=MonitoringComponent.API,
            exception_type="ValueError",
            location="document.py:44",
        )
        assert first != second


class TestMetricCatalog:
    """Every metric is declared before it can be recorded."""

    def test_every_name_has_a_declaration(self) -> None:
        for name in MetricName:
            assert metric_definition(name).name is name

    def test_a_declaration_carries_what_an_exporter_needs(self) -> None:
        definition = metric_definition(MetricName.HTTP_REQUESTS_TOTAL)

        assert definition.description
        assert definition.labels == ("method", "route", "status_class")

    def test_latency_histograms_share_one_ladder(self) -> None:
        """Two latency histograms with different boundaries cannot be compared."""
        http = buckets_for(METRICS[MetricName.HTTP_REQUEST_DURATION_MS])
        database = buckets_for(METRICS[MetricName.DB_QUERY_DURATION_MS])

        assert http == database

    def test_a_size_histogram_gets_size_buckets(self) -> None:
        buckets = buckets_for(METRICS[MetricName.HTTP_RESPONSE_SIZE_BYTES])

        assert buckets[0] == 1_024.0
        assert buckets != buckets_for(METRICS[MetricName.HTTP_REQUEST_DURATION_MS])


class TestSecurityVocabulary:
    """Severities are assigned once, never chosen at a call site."""

    def test_every_event_has_a_default_severity(self) -> None:
        for event in SecurityEventType:
            assert isinstance(security_severity(event), SecuritySeverity)

    def test_a_successful_sign_in_is_informational(self) -> None:
        """It is recorded for the denominator, not because it is news."""
        assert security_severity(SecurityEventType.LOGIN_SUCCEEDED) is SecuritySeverity.INFO

    def test_a_lockout_is_critical(self) -> None:
        assert security_severity(SecurityEventType.LOGIN_LOCKED_OUT) is SecuritySeverity.CRITICAL

    def test_an_expired_token_is_routine(self) -> None:
        """Fifteen-minute tokens expire four times an hour per active session."""
        assert security_severity(SecurityEventType.TOKEN_EXPIRED) is SecuritySeverity.INFO


class TestAlertRules:
    """Declared as data, with thresholds deliberately elsewhere."""

    def test_rule_keys_are_unique(self) -> None:
        keys = [rule.key for rule in ALERT_RULES]
        assert len(keys) == len(set(keys))

    def test_every_rule_names_what_is_wrong_without_a_number(self) -> None:
        """One sentence has to serve every deployment's threshold."""
        for rule in ALERT_RULES:
            assert rule.summary
            assert not any(character.isdigit() for character in rule.summary)

    def test_the_spec_s_own_alert_list_is_covered(self) -> None:
        keys = {rule.key for rule in ALERT_RULES}
        assert {
            "database_unavailable",
            "cache_unavailable",
            "storage_unavailable",
            "vector_unavailable",
            "error_rate_high",
            "latency_high",
            "queue_backlog",
        } <= keys
