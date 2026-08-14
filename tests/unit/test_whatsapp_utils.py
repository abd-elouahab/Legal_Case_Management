"""Unit tests for ``core/whatsapp.py`` — the WhatsApp channel's vocabulary.

Pure data and six derivations, so these are pure tests: no database, no provider,
no descriptors. What they are actually protecting is the set of decisions the
spec made and this module wrote down — which notifications travel on the channel,
which failures are retried, which numbers the platform is willing to send to, and
what a template may see.

The **absence** tests matter as much as the presence ones. The spec's *"Events
That Must NOT Generate WhatsApp Messages"* list needs no code, because those rules
are simply not in :data:`~core.whatsapp.WHATSAPP_RULES` — and "needs no code" is
only safe if something fails when one is added by accident.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core.events import DomainEventType
from core.indexing import LANGUAGE_ARABIC, LANGUAGE_ENGLISH, LANGUAGE_FRENCH
from core.notifications import (
    ANNOUNCEMENT_RULES,
    EVENT_RULES,
    RULE_CASE_ASSIGNED,
    RULE_HEARING_SCHEDULED,
    RULE_HEARING_UPDATED,
    RULES_BY_KEY,
    AnnouncementKind,
    NotificationChannel,
)
from core.localization import default_language
from core.whatsapp import (
    MAX_PARAMETER_LENGTH,
    PROVIDER_LANGUAGE_CODES,
    STATUS_TRANSITIONS,
    TEMPLATE_NOTIFICATION,
    TEMPLATE_SECURITY,
    TRANSIENT_FAILURE_CODES,
    WHATSAPP_RULES,
    WhatsAppDeliveryStatus,
    WhatsAppFailureCode,
    build_whatsapp_context,
    can_transition,
    chrome_for,
    failure_from_value,
    is_transient,
    is_whatsapp_eligible,
    next_attempt_at,
    normalize_phone,
    provider_language_code,
    resolve_whatsapp_language,
    retry_delay_seconds,
    sanitize_parameter,
    target_url,
    whatsapp_rule_for,
)

# --------------------------------------------------------------------------- #
# Which notifications travel on the channel
# --------------------------------------------------------------------------- #


class TestRules:
    def test_every_rule_key_is_a_real_notification_rule(self) -> None:
        """The one invariant that makes ``WHATSAPP_RULES`` safe to key by string.

        A rule renamed or withdrawn in `core/notifications.py` would otherwise
        become an entry here that silently matches nothing — stopping a class of
        message with no error anywhere.
        """
        assert set(WHATSAPP_RULES) <= set(RULES_BY_KEY)

    def test_the_supported_types_are_exactly_the_specs_list(self) -> None:
        """`18-whatsapp-delivery-channel.md`'s "Supported Notification Types",
        with both optional entries included — see the note above
        ``WHATSAPP_RULES`` for why each was taken up."""
        assert set(WHATSAPP_RULES) == {
            # Authentication
            EVENT_RULES[DomainEventType.USER_ACTIVATED].key,
            EVENT_RULES[DomainEventType.USER_PASSWORD_RESET].key,
            # Case management
            RULE_CASE_ASSIGNED.key,
            # Court
            RULE_HEARING_UPDATED.key,
            RULE_HEARING_SCHEDULED.key,
            # Reports
            EVENT_RULES[DomainEventType.REPORT_GENERATED].key,
            # System
            ANNOUNCEMENT_RULES[AnnouncementKind.MAINTENANCE].key,
        }

    @pytest.mark.parametrize(
        "event_type",
        [
            DomainEventType.DOCUMENT_UPLOADED,
            DomainEventType.DOCUMENT_REPLACED,
            DomainEventType.DOCUMENT_DELETED,
            DomainEventType.OCR_COMPLETED,
            DomainEventType.OCR_FAILED,
        ],
    )
    def test_the_forbidden_events_are_absent(self, event_type: DomainEventType) -> None:
        """The spec's *"Events That Must NOT Generate WhatsApp Messages"*.

        Enforced here rather than by review, which is the entire reason that list
        needed no code of its own: documents, OCR, indexing, the assistant, and
        the timeline stay in the application.
        """
        assert not is_whatsapp_eligible(EVENT_RULES[event_type].key)

    def test_the_routine_announcement_is_absent(self) -> None:
        """The spec asks for a *critical* system announcement, and the platform's
        critical announcement is `system.maintenance`. Messaging every routine
        announcement to every phone number is how a business number gets blocked."""
        assert not is_whatsapp_eligible(
            ANNOUNCEMENT_RULES[AnnouncementKind.ANNOUNCEMENT].key
        )

    def test_security_notifications_use_the_link_free_template(self) -> None:
        """A link in a WhatsApp message about a password is the shape of a
        phishing message, and the sender is a number the reader may not know."""
        for event_type in (
            DomainEventType.USER_ACTIVATED,
            DomainEventType.USER_PASSWORD_RESET,
        ):
            rule = whatsapp_rule_for(EVENT_RULES[event_type].key)
            assert rule is not None
            assert rule.template == TEMPLATE_SECURITY

    def test_case_and_court_notifications_use_the_general_template(self) -> None:
        for key in (RULE_CASE_ASSIGNED.key, RULE_HEARING_UPDATED.key):
            rule = whatsapp_rule_for(key)
            assert rule is not None
            assert rule.template == TEMPLATE_NOTIFICATION

    def test_an_unlisted_rule_resolves_to_nothing(self) -> None:
        """Which is the ordinary answer for most of the platform, never an error."""
        assert whatsapp_rule_for("case.created") is None


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


class TestLifecycle:
    def test_the_four_states_are_the_specs(self) -> None:
        assert set(WhatsAppDeliveryStatus) == {
            WhatsAppDeliveryStatus.PENDING,
            WhatsAppDeliveryStatus.SENDING,
            WhatsAppDeliveryStatus.DELIVERED,
            WhatsAppDeliveryStatus.FAILED,
        }

    def test_a_transient_failure_returns_to_the_queue(self) -> None:
        assert can_transition(
            WhatsAppDeliveryStatus.SENDING, WhatsAppDeliveryStatus.PENDING
        )

    def test_a_failed_delivery_can_be_re_queued(self) -> None:
        """An operator who fixed a rate limit should be able to retry."""
        assert can_transition(
            WhatsAppDeliveryStatus.FAILED, WhatsAppDeliveryStatus.PENDING
        )

    def test_a_delivered_message_is_terminal(self) -> None:
        """Sending a second message about a hearing to somebody who already has
        one is the one mistake this feature must not make."""
        assert STATUS_TRANSITIONS[WhatsAppDeliveryStatus.DELIVERED] == frozenset()

    def test_a_pending_delivery_cannot_jump_to_delivered(self) -> None:
        """Or its duration would be a lie."""
        assert not can_transition(
            WhatsAppDeliveryStatus.PENDING, WhatsAppDeliveryStatus.DELIVERED
        )


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class TestFailures:
    @pytest.mark.parametrize(
        "code",
        [
            WhatsAppFailureCode.TIMEOUT,
            WhatsAppFailureCode.CONNECTION_FAILED,
            WhatsAppFailureCode.THROTTLED,
            WhatsAppFailureCode.PROVIDER_UNAVAILABLE,
            WhatsAppFailureCode.UNKNOWN,
        ],
    )
    def test_the_specs_temporary_failures_are_retried(
        self, code: WhatsAppFailureCode
    ) -> None:
        """*"timeout, temporary provider outage, network interruption, rate
        limiting"* — plus `unknown`, because discarding a hearing notice over an
        unrecognised error is the worse mistake."""
        assert is_transient(code)

    @pytest.mark.parametrize(
        "code",
        [
            WhatsAppFailureCode.AUTHENTICATION_FAILED,
            WhatsAppFailureCode.RECIPIENT_REFUSED,
            WhatsAppFailureCode.INVALID_RECIPIENT,
            WhatsAppFailureCode.MESSAGE_REFUSED,
            WhatsAppFailureCode.TEMPLATE_REJECTED,
            WhatsAppFailureCode.TEMPLATE_FAILURE,
        ],
    )
    def test_permanent_failures_are_not(self, code: WhatsAppFailureCode) -> None:
        """Each of these fails identically on every attempt, so retrying spends
        the platform's time and the provider's rate limit to reach the same
        outcome more slowly."""
        assert not is_transient(code)

    def test_the_partition_is_exhaustive(self) -> None:
        """Every code is on exactly one side, so no failure has undefined retry
        behaviour — and a code added later without a decision about it fails here
        rather than silently becoming permanent."""
        permanent = {
            code for code in WhatsAppFailureCode if code not in TRANSIENT_FAILURE_CODES
        }
        assert TRANSIENT_FAILURE_CODES | permanent == set(WhatsAppFailureCode)
        assert not TRANSIENT_FAILURE_CODES & permanent
        assert permanent  # a partition with an empty half would be a mistake

    def test_an_unknown_stored_code_reads_as_none(self) -> None:
        """The column is an open registry: a code written by a later version must
        not make an earlier one unable to read a delivery record."""
        assert failure_from_value("invented_by_a_later_version") is None
        assert failure_from_value(None) is None
        assert failure_from_value("throttled") is WhatsAppFailureCode.THROTTLED


# --------------------------------------------------------------------------- #
# Retry schedule
# --------------------------------------------------------------------------- #


class TestRetrySchedule:
    def test_the_first_delay_is_the_base(self) -> None:
        assert retry_delay_seconds(0, base=30.0, cap=1800.0) == 30.0

    def test_it_doubles(self) -> None:
        assert retry_delay_seconds(1, base=30.0, cap=1800.0) == 60.0
        assert retry_delay_seconds(2, base=30.0, cap=1800.0) == 120.0

    def test_the_cap_holds(self) -> None:
        """Without it a tenth attempt is hours away, which on this channel means
        a hearing update arriving after the hearing."""
        assert retry_delay_seconds(20, base=30.0, cap=1800.0) == 1800.0

    def test_a_huge_attempt_count_does_not_overflow(self) -> None:
        assert retry_delay_seconds(10_000, base=30.0, cap=1800.0) == 1800.0

    def test_the_next_attempt_is_in_the_future(self) -> None:
        now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        assert next_attempt_at(0, base=30.0, cap=1800.0, now=now) > now


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #


class TestPhoneNumbers:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("+212612345678", "212612345678"),
            ("+212 6-12-34-56-78", "212612345678"),
            ("00212612345678", "212612345678"),
            ("212612345678", "212612345678"),
            ("  +33 6 12 34 56 78 ", "33612345678"),
        ],
    )
    def test_an_international_number_normalizes_to_e164_digits(
        self, stored: str, expected: str
    ) -> None:
        """`users.phone` is a free-text display field, and the Cloud API wants
        digits with the country code and no ``+``."""
        assert normalize_phone(stored) == expected

    def test_a_national_number_needs_a_configured_country(self) -> None:
        """A leading zero is a trunk prefix: joining it to a country code produces
        a *different subscriber*, so it is dropped and the code prepended."""
        assert normalize_phone("0612345678", default_country_code="212") == "212612345678"

    def test_a_national_number_without_one_is_refused(self) -> None:
        """The safe reading of an ambiguous value. One message not sent is a
        failure this channel may have; a legal notification delivered to a
        stranger is not."""
        assert normalize_phone("0612345678") is None

    def test_a_country_code_may_be_written_with_a_plus(self) -> None:
        assert normalize_phone("0612345678", default_country_code="+212") == "212612345678"

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            "",
            "   ",
            "not a number",
            "12345",  # too short to be routable
            "1234567890123456789",  # past the E.164 ceiling
            "+0123456789012",  # no country code begins with zero
        ],
    )
    def test_an_unusable_number_is_refused(self, stored: str | None) -> None:
        assert normalize_phone(stored) is None

    def test_the_two_international_spellings_agree(self) -> None:
        """``00212…`` and ``+212…`` are the same number, and producing two
        different-looking recipients from one person would defeat every check that
        works on identity."""
        assert normalize_phone("00212612345678") == normalize_phone("+212612345678")


# --------------------------------------------------------------------------- #
# Template parameters
# --------------------------------------------------------------------------- #


class TestParameters:
    def test_whitespace_is_collapsed(self) -> None:
        """A real Cloud API constraint: it refuses a body parameter containing a
        newline, a tab, or four consecutive spaces."""
        assert sanitize_parameter("one\ntwo\tthree    four") == "one two three four"

    def test_it_collapses_rather_than_refusing(self) -> None:
        """The opposite of what `sanitize_header_value` does with a line break,
        deliberately: an administrator who pressed Enter in an announcement has
        not attacked anything, and refusing would drop their message."""
        assert sanitize_parameter("hello\nworld")

    def test_it_is_bounded(self) -> None:
        assert len(sanitize_parameter("x" * 5000)) == MAX_PARAMETER_LENGTH

    def test_a_non_string_is_accepted(self) -> None:
        """A notification's context carries page counts and versions."""
        assert sanitize_parameter(12) == "12"


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #


class TestLanguage:
    def test_every_platform_language_has_a_provider_tag(self) -> None:
        """A template approved as ``fr`` cannot be sent as ``fr_FR``, so this
        mapping is the difference between a delivered message and a
        `template_rejected`."""
        assert set(PROVIDER_LANGUAGE_CODES) == {
            LANGUAGE_FRENCH,
            LANGUAGE_ARABIC,
            LANGUAGE_ENGLISH,
        }

    def test_english_carries_a_region(self) -> None:
        assert provider_language_code(LANGUAGE_ENGLISH) == "en_US"

    def test_an_unknown_language_falls_back_to_french(self) -> None:
        assert provider_language_code("de") == "fr"

    def test_the_resolver_is_the_notification_one(self) -> None:
        """So a message and the notification it carries can never be rendered by
        two different resolvers."""
        assert resolve_whatsapp_language("ar") == LANGUAGE_ARABIC
        assert resolve_whatsapp_language(None) == default_language()
        assert resolve_whatsapp_language("klingon") == default_language()


# --------------------------------------------------------------------------- #
# What a template may see
# --------------------------------------------------------------------------- #


class TestContext:
    def test_the_wording_comes_from_the_notification_module(self) -> None:
        """Not from a copy here, and not from the approved template — which is
        the whole reason this channel sends parameters rather than letting Meta
        hold the sentences."""
        context = build_whatsapp_context(
            rule_key=RULE_CASE_ASSIGNED.key,
            category="case",
            priority="high",
            context={"case_number": "CASE-2026-0001"},
            recipient_name="Amina",
            language=LANGUAGE_FRENCH,
            platform_name="Legal",
        )
        assert context.title == "Dossier attribué"
        assert "CASE-2026-0001" in context.message

    def test_it_renders_in_the_readers_language(self) -> None:
        context = build_whatsapp_context(
            rule_key=RULE_CASE_ASSIGNED.key,
            category="case",
            priority="high",
            context={"case_number": "CASE-2026-0001"},
            recipient_name="أمينة",
            language=LANGUAGE_ARABIC,
            platform_name="Legal",
        )
        assert context.title == "تم إسناد ملف إليك"
        assert context.chrome["greeting"].startswith("مرحبًا")

    def test_it_carries_no_case_document_or_user_record(self) -> None:
        """The security property this feature has to hold is a statement about
        *this list*: there is nothing in scope that could expose something the
        recipient is not authorized to receive."""
        context = build_whatsapp_context(
            rule_key=RULE_CASE_ASSIGNED.key,
            category="case",
            priority="high",
            context={"case_number": "CASE-2026-0001"},
            recipient_name="Amina",
            language=LANGUAGE_FRENCH,
            platform_name="Legal",
        )
        # Note what is here and what is not. There is no `direction` — the email
        # context carries one because its HTML part is markup rendered in a mail
        # client, and a WhatsApp parameter is text into a slot that the approved
        # template already lays out.
        assert set(context.as_mapping()) == {
            "title",
            "message",
            "recipient_name",
            "language",
            "category",
            "priority",
            "action_url",
            "values",
            "platform_name",
            "chrome",
        }

    def test_the_specs_variables_reach_a_template(self) -> None:
        """*"user name, case title, hearing date, report name"* — the name
        directly, the rest through the notification's own bounded context."""
        context = build_whatsapp_context(
            rule_key=RULE_CASE_ASSIGNED.key,
            category="case",
            priority="high",
            context={"case_number": "CASE-2026-0001", "report_type": "summary"},
            recipient_name="Amina",
            language=LANGUAGE_FRENCH,
            platform_name="Legal",
        )
        assert context.recipient_name == "Amina"
        assert context.values["case_number"] == "CASE-2026-0001"
        assert context.values["report_type"] == "summary"


class TestLinks:
    def test_a_case_target_becomes_a_deep_link(self) -> None:
        case_id = uuid.uuid4()
        assert target_url(
            "https://legal.example", target_type="case", target_id=case_id
        ) == f"https://legal.example/cases/{case_id}"

    def test_no_base_url_means_no_link(self) -> None:
        """Linkless but correct beats a broken path with no host — and unclickable
        text in a WhatsApp thread is worse than in an email, because the reader
        cannot fix it."""
        assert target_url(None, target_type="case", target_id=uuid.uuid4()) is None

    def test_a_targetless_notification_points_at_the_feed(self) -> None:
        assert (
            target_url("https://legal.example", target_type=None, target_id=None)
            == "https://legal.example/notifications"
        )

    def test_an_unknown_target_type_points_at_the_feed(self) -> None:
        """Tolerant for the reason every stored vocabulary here is read
        tolerantly."""
        assert (
            target_url("https://legal.example", target_type="invented", target_id=None)
            == "https://legal.example/notifications"
        )


class TestChrome:
    @pytest.mark.parametrize(
        "language", [LANGUAGE_FRENCH, LANGUAGE_ARABIC, LANGUAGE_ENGLISH]
    )
    def test_every_language_has_every_piece(self, language: str) -> None:
        """The chrome lives here rather than in the approved template, so it has
        to be complete here — a missing key is a `StrictUndefined` failure per
        message."""
        chrome = chrome_for(language, recipient_name="Amina")
        assert set(chrome) == {
            "greeting",
            "action_label",
            "no_link",
            "security_note",
        }
        assert all(chrome.values())

    def test_the_greeting_carries_the_name(self) -> None:
        assert "Amina" in chrome_for(LANGUAGE_FRENCH, recipient_name="Amina")["greeting"]


class TestChannelVocabulary:
    def test_whatsapp_is_a_notification_channel(self) -> None:
        """And its value is the column name on
        `models.notification.NotificationPreference`, which is what lets one
        repository method serve every channel."""
        assert NotificationChannel.WHATSAPP.value == "whatsapp"
