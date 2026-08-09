"""Unit tests for ``core/email.py`` — the email channel's pure vocabulary.

No database, no SMTP connection, and no template file: everything here is a
statement about the table that decides what travels by email, the partition that
decides what is worth retrying, and the three small functions that keep an
address, a header, and a link honest.

The most valuable tests in this file are the **negative** ones. That the seven
supported email types are marked for delivery is easy to get right and easy to
notice; that the spec's *"Events That Must NOT Generate Emails"* list is still
absent from the table is exactly the kind of thing a later feature adds by
accident, so it is asserted rather than reviewed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core.email import (
    EMAIL_CHROME,
    EMAIL_RULES,
    STATUS_TRANSITIONS,
    TEMPLATE_NOTIFICATION,
    TEMPLATE_SECURITY,
    TRANSIENT_FAILURE_CODES,
    EmailDeliveryStatus,
    EmailFailureCode,
    InvalidEmailRecipientError,
    build_email_context,
    can_transition,
    chrome_for,
    email_rule_for,
    failure_from_value,
    is_email_eligible,
    is_transient,
    next_attempt_at,
    normalize_email,
    resolve_email_language,
    retry_delay_seconds,
    sanitize_header_value,
    subject_for,
    target_url,
    text_direction,
)
from core.events import DomainEventType
from core.notifications import (
    ANNOUNCEMENT_RULES,
    EVENT_RULES,
    RULES_BY_KEY,
    AnnouncementKind,
    NotificationTargetType,
)

# --------------------------------------------------------------------------- #
# Which notifications travel by email
# --------------------------------------------------------------------------- #


class TestEmailRules:
    def test_every_supported_email_type_is_marked(self) -> None:
        """The spec's "Supported Email Types" list, in full."""
        expected = {
            # Authentication
            EVENT_RULES[DomainEventType.USER_PASSWORD_RESET].key,
            EVENT_RULES[DomainEventType.USER_ACTIVATED].key,
            # Case management
            "case.assigned",
            # Court
            "hearing.updated",
            "hearing.scheduled",
            # Reports
            EVENT_RULES[DomainEventType.REPORT_GENERATED].key,
            # System
            ANNOUNCEMENT_RULES[AnnouncementKind.MAINTENANCE].key,
        }
        assert set(EMAIL_RULES) == expected

    @pytest.mark.parametrize(
        "rule_key",
        [
            "document.uploaded",
            "document.replaced",
            "document.deleted",
            "ocr.completed",
            "ocr.failed",
            "case.created",
            "case.updated",
            "case.status_changed",
            "case.priority_changed",
            "case.archived",
            "case.restored",
            "case.unassigned",
            "report.failed",
            "user.role_changed",
            "system.announcement",
        ],
    )
    def test_excluded_notifications_are_never_emailed(self, rule_key: str) -> None:
        """`17-email-delivery-channel.md`'s "Events That Must NOT Generate Emails",
        plus every notification the spec's supported list does not name.

        Asserted rather than reviewed: adding an entry to `EMAIL_RULES` is one
        line, and this is what makes doing it accidentally a failing build. Note
        that `system.announcement` is here while `system.maintenance` is not — the
        spec asks for a *critical* system announcement, and mailing every routine
        one to every account is how a platform's mail starts being filtered.
        """
        assert not is_email_eligible(rule_key)
        assert email_rule_for(rule_key) is None

    def test_every_marked_rule_is_a_real_notification_rule(self) -> None:
        """A key with no notification behind it would match nothing, silently."""
        assert set(EMAIL_RULES) <= set(RULES_BY_KEY)

    def test_the_ai_pipeline_is_absent_in_full(self) -> None:
        """OCR, indexing, and the assistant are in-app only, whatever they do."""
        assert not any(
            key.startswith(("ocr.", "indexing.", "document.", "timeline.", "presence."))
            for key in EMAIL_RULES
        )

    def test_account_security_uses_its_own_template(self) -> None:
        """A message about a password offers no link — see `security.v1`."""
        reset = email_rule_for(EVENT_RULES[DomainEventType.USER_PASSWORD_RESET].key)
        assert reset is not None
        assert reset.template == TEMPLATE_SECURITY

    def test_case_news_uses_the_general_template(self) -> None:
        assigned = email_rule_for("case.assigned")
        assert assigned is not None
        assert assigned.template == TEMPLATE_NOTIFICATION


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


class TestTransitions:
    def test_the_happy_path(self) -> None:
        assert can_transition(EmailDeliveryStatus.PENDING, EmailDeliveryStatus.SENDING)
        assert can_transition(EmailDeliveryStatus.SENDING, EmailDeliveryStatus.SENT)

    def test_a_transient_failure_returns_to_the_queue(self) -> None:
        assert can_transition(EmailDeliveryStatus.SENDING, EmailDeliveryStatus.PENDING)

    def test_a_failure_can_be_retried_by_an_operator(self) -> None:
        assert can_transition(EmailDeliveryStatus.FAILED, EmailDeliveryStatus.PENDING)

    def test_a_sent_message_is_terminal(self) -> None:
        """The one mistake this feature must not make: sending it twice."""
        assert STATUS_TRANSITIONS[EmailDeliveryStatus.SENT] == frozenset()
        for target in EmailDeliveryStatus:
            assert not can_transition(EmailDeliveryStatus.SENT, target)

    def test_every_state_has_an_entry(self) -> None:
        assert set(STATUS_TRANSITIONS) == set(EmailDeliveryStatus)


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class TestFailureClassification:
    @pytest.mark.parametrize(
        "code",
        [
            EmailFailureCode.TIMEOUT,
            EmailFailureCode.CONNECTION_FAILED,
            EmailFailureCode.THROTTLED,
            EmailFailureCode.PROVIDER_UNAVAILABLE,
            EmailFailureCode.UNKNOWN,
        ],
    )
    def test_temporary_failures_are_retried(self, code: EmailFailureCode) -> None:
        """The spec names timeout, temporary SMTP failure, and network
        interruption; `UNKNOWN` joins them because discarding mail over an
        unrecognised error is the worse mistake."""
        assert is_transient(code)

    @pytest.mark.parametrize(
        "code",
        [
            EmailFailureCode.AUTHENTICATION_FAILED,
            EmailFailureCode.RECIPIENT_REFUSED,
            EmailFailureCode.INVALID_RECIPIENT,
            EmailFailureCode.MESSAGE_REFUSED,
            EmailFailureCode.TEMPLATE_FAILURE,
        ],
    )
    def test_permanent_failures_are_not(self, code: EmailFailureCode) -> None:
        """Each will fail identically on every attempt — and retrying a rejected
        credential is how an account gets locked."""
        assert not is_transient(code)

    def test_every_code_is_classified(self) -> None:
        permanent = set(EmailFailureCode) - TRANSIENT_FAILURE_CODES
        assert permanent and TRANSIENT_FAILURE_CODES
        assert permanent | TRANSIENT_FAILURE_CODES == set(EmailFailureCode)

    def test_an_unknown_stored_code_is_read_tolerantly(self) -> None:
        """A code written by a later version must not make a delivery unreadable."""
        assert failure_from_value("relay_moved_to_mars") is None
        assert failure_from_value(None) is None
        assert failure_from_value("timeout") is EmailFailureCode.TIMEOUT


# --------------------------------------------------------------------------- #
# Retry schedule
# --------------------------------------------------------------------------- #


class TestRetrySchedule:
    def test_the_delay_doubles(self) -> None:
        delays = [retry_delay_seconds(n, base=30.0, cap=3600.0) for n in range(4)]
        assert delays == [30.0, 60.0, 120.0, 240.0]

    def test_the_ceiling_holds(self) -> None:
        """Without it, a tenth attempt is hours away — long enough that a hearing
        reminder arrives after the hearing."""
        assert retry_delay_seconds(20, base=30.0, cap=3600.0) == 3600.0
        assert retry_delay_seconds(500, base=30.0, cap=3600.0) == 3600.0

    def test_the_first_failure_waits_the_base(self) -> None:
        assert retry_delay_seconds(0, base=15.0, cap=3600.0) == 15.0

    def test_a_base_above_the_cap_is_clamped(self) -> None:
        assert retry_delay_seconds(0, base=99.0, cap=10.0) == 10.0

    def test_the_next_attempt_is_in_the_future(self) -> None:
        now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
        assert next_attempt_at(0, base=30.0, cap=3600.0, now=now) == datetime(
            2026, 8, 9, 12, 0, 30, tzinfo=UTC
        )


# --------------------------------------------------------------------------- #
# Recipients and headers
# --------------------------------------------------------------------------- #


class TestAddresses:
    def test_a_usable_address_is_normalized(self) -> None:
        assert normalize_email("  Amina@Firm.Example  ") == "amina@firm.example"

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "not-an-address", "@firm.example", "amina@", "amina@firm"],
    )
    def test_an_unusable_address_is_refused(self, value: str | None) -> None:
        assert normalize_email(value) is None

    def test_an_address_carrying_a_line_break_is_refused(self) -> None:
        """Header injection: a CR or LF ends the header and starts a new one,
        which is how an injected `Bcc:` reaches a message the platform composed."""
        assert normalize_email("amina@firm.example\r\nBcc: attacker@evil.example") is None

    def test_an_over_long_address_is_refused(self) -> None:
        assert normalize_email(f"{'a' * 400}@firm.example") is None


class TestHeaders:
    def test_a_safe_value_passes_through(self) -> None:
        assert sanitize_header_value("Dossier CASE-2026-0001") == "Dossier CASE-2026-0001"

    @pytest.mark.parametrize("payload", ["a\rb", "a\nb", "a\x00b"])
    def test_a_line_break_is_refused_rather_than_stripped(self, payload: str) -> None:
        """A name silently rewritten is a name that was attacked and nobody
        noticed."""
        with pytest.raises(InvalidEmailRecipientError):
            sanitize_header_value(payload)

    def test_a_long_value_is_clipped(self) -> None:
        assert len(sanitize_header_value("x" * 500, limit=100)) == 100

    def test_a_subject_is_the_notification_title(self) -> None:
        assert subject_for("Audience mise à jour") == "Audience mise à jour"

    def test_a_prefix_is_applied_when_configured(self) -> None:
        assert subject_for("Rapport prêt", prefix="[staging]") == "[staging] Rapport prêt"


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


class TestTargetUrl:
    def test_a_case_becomes_a_deep_link(self) -> None:
        case_id = uuid.uuid4()
        assert target_url(
            "https://legal.example", target_type="case", target_id=case_id
        ) == f"https://legal.example/cases/{case_id}"

    def test_a_trailing_slash_does_not_double(self) -> None:
        case_id = uuid.uuid4()
        assert target_url(
            "https://legal.example/", target_type="case", target_id=case_id
        ) == f"https://legal.example/cases/{case_id}"

    def test_an_account_target_points_at_settings(self) -> None:
        assert (
            target_url("https://legal.example", target_type="account", target_id=None)
            == "https://legal.example/settings"
        )

    def test_no_target_sends_the_reader_to_their_feed(self) -> None:
        assert (
            target_url("https://legal.example", target_type=None, target_id=None)
            == "https://legal.example/notifications"
        )

    def test_a_case_target_with_no_identifier_falls_back_to_the_feed(self) -> None:
        """Rather than producing `/cases/None`."""
        assert (
            target_url("https://legal.example", target_type="case", target_id=None)
            == "https://legal.example/notifications"
        )

    def test_an_unknown_target_type_falls_back_rather_than_raising(self) -> None:
        assert (
            target_url("https://legal.example", target_type="hearing", target_id=None)
            == "https://legal.example/notifications"
        )

    @pytest.mark.parametrize("base", [None, "", "   "])
    def test_no_base_url_produces_no_link_at_all(self, base: str | None) -> None:
        """Linkless but correct mail, rather than a broken `/cases/…` with no
        host."""
        assert target_url(base, target_type="case", target_id=uuid.uuid4()) is None

    def test_every_target_type_has_a_path(self) -> None:
        for member in NotificationTargetType:
            assert (
                target_url("https://legal.example", target_type=member.value, target_id=uuid.uuid4())
                is not None
            )


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #


class TestLanguage:
    @pytest.mark.parametrize(
        ("language", "direction"), [("ar", "rtl"), ("fr", "ltr"), ("en", "ltr")]
    )
    def test_arabic_is_right_to_left(self, language: str, direction: str) -> None:
        """The direction travels *in* the document: a mail client has no idea
        which language this is and no access to the application's stylesheet."""
        assert text_direction(language) == direction

    def test_an_unknown_language_reads_left_to_right(self) -> None:
        assert text_direction("de") == "ltr"

    def test_french_is_the_fallback(self) -> None:
        assert resolve_email_language(None) == "fr"
        assert resolve_email_language("de") == "fr"
        assert resolve_email_language("AR") == "ar"

    def test_every_language_has_chrome(self) -> None:
        for language in ("ar", "fr", "en"):
            assert language in EMAIL_CHROME

    def test_the_greeting_is_interpolated(self) -> None:
        chrome = chrome_for("fr", recipient_name="Amina Benali")
        assert chrome["greeting"] == "Bonjour Amina Benali,"

    def test_an_unknown_language_falls_back_to_french_chrome(self) -> None:
        assert chrome_for("de", recipient_name="X")["action_label"] == (
            EMAIL_CHROME["fr"].action_label
        )


# --------------------------------------------------------------------------- #
# The template context
# --------------------------------------------------------------------------- #


class TestEmailContext:
    def _context(self, *, language: str = "fr"):  # type: ignore[no-untyped-def]
        case_id = uuid.uuid4()
        return build_email_context(
            rule_key="case.assigned",
            category="case",
            priority="high",
            context={"case_number": "CASE-2026-0001"},
            recipient_name="Amina Benali",
            language=language,
            base_url="https://legal.example",
            target_type="case",
            target_id=case_id,
            platform_name="Legal Platform",
        )

    def test_the_wording_comes_from_the_notification_module(self) -> None:
        """Not restated here — which is what `code-standards.md`'s "notification
        logic must never be duplicated" means across channels."""
        context = self._context()
        assert context.title == "Dossier attribué"
        assert "CASE-2026-0001" in context.message

    def test_arabic_renders_arabic_and_reads_right_to_left(self) -> None:
        context = self._context(language="ar")
        assert context.title == "تم إسناد ملف إليك"
        assert context.direction == "rtl"

    def test_only_the_notification_and_its_own_values_are_in_scope(self) -> None:
        """The security property: an email's body is, by construction, the
        notification the recipient can already open."""
        rendered = self._context().as_mapping()
        assert set(rendered) == {
            "title",
            "message",
            "recipient_name",
            "language",
            "direction",
            "category",
            "priority",
            "action_url",
            "values",
            "platform_name",
            "chrome",
        }
        assert rendered["values"] == {"case_number": "CASE-2026-0001"}

    def test_a_missing_context_value_does_not_break_the_render(self) -> None:
        context = build_email_context(
            rule_key="case.assigned",
            category="case",
            priority="high",
            context=None,
            recipient_name="Amina",
            language="fr",
            base_url=None,
            target_type=None,
            target_id=None,
            platform_name="Legal Platform",
        )
        assert context.title
        assert context.action_url is None
