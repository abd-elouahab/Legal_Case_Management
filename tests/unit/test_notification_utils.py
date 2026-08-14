"""Unit tests for the notification vocabulary and its rule table.

Everything in :mod:`core.notifications` is pure: no database, no session, no
event dispatcher. So these tests are about the four derivations the module
exists for — which event becomes which notification, what it opens, what it
says, and when two of them are the same thing — and they can assert about all
four without any infrastructure at all.
"""

from __future__ import annotations

import uuid

import pytest

from core.events import DomainEvent, DomainEventType, case_topic
from core.indexing import LANGUAGE_ARABIC, LANGUAGE_ENGLISH, LANGUAGE_FRENCH
from core.localization import default_language
from core.notifications import (
    ANNOUNCEMENT_RULES,
    DEFAULT_PREFERENCES,
    EVENT_RULES,
    MAX_CONTEXT_VALUE_LENGTH,
    MAX_MESSAGE_LENGTH,
    MISSING_VALUE,
    RULE_CASE_ASSIGNED,
    RULE_CASE_UNASSIGNED,
    RULE_HEARING_SCHEDULED,
    RULE_HEARING_UPDATED,
    RULES_BY_KEY,
    AnnouncementKind,
    NotificationCategory,
    NotificationPreferenceKey,
    NotificationPriority,
    NotificationTargetType,
    NotificationType,
    build_context,
    dedupe_key,
    normalize_context,
    preference_from_value,
    render_notification,
    resolve_notification_language,
    rule_for,
    target_for,
)

LANGUAGES = (LANGUAGE_FRENCH, LANGUAGE_ARABIC, LANGUAGE_ENGLISH)


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


class TestRuleTable:
    def test_every_rule_is_renderable_by_key(self) -> None:
        """A rule that exists must be renderable, or it is a row created today
        and unreadable tomorrow."""
        for rule in EVENT_RULES.values():
            assert RULES_BY_KEY[rule.key] is rule

        for rule in ANNOUNCEMENT_RULES.values():
            assert RULES_BY_KEY[rule.key] is rule

    def test_every_rule_has_all_three_languages(self) -> None:
        """`project-overview.md` names Arabic and French; English is the API's own."""
        for rule in RULES_BY_KEY.values():
            for language in LANGUAGES:
                assert rule.template.titles[language].strip()
                assert rule.template.messages[language].strip()

    def test_rule_keys_are_unique(self) -> None:
        """Two rules under one key would make one of them unrenderable."""
        keys = [rule.key for rule in RULES_BY_KEY.values()]
        assert len(keys) == len(set(keys))

    def test_every_preference_key_is_used_by_at_least_one_rule(self) -> None:
        """A preference nobody can act on is a switch that does nothing."""
        used = {rule.preference for rule in RULES_BY_KEY.values()}
        assert used == set(NotificationPreferenceKey)

    def test_every_preference_defaults_to_on(self) -> None:
        """`architecture.md` invariant 3 is false for every untouched account
        otherwise."""
        assert set(DEFAULT_PREFERENCES) == set(NotificationPreferenceKey)
        assert all(DEFAULT_PREFERENCES.values())

    @pytest.mark.parametrize(
        "event_type",
        [
            DomainEventType.DOCUMENT_UPDATED,
            DomainEventType.OCR_STARTED,
            DomainEventType.INDEXING_STARTED,
            DomainEventType.INDEXING_COMPLETED,
            DomainEventType.INDEXING_FAILED,
            DomainEventType.REPORT_STARTED,
            DomainEventType.REPORT_PROGRESS,
            DomainEventType.TIMELINE_UPDATED,
            DomainEventType.PRESENCE_CHANGED,
            DomainEventType.USER_DEACTIVATED,
        ],
    )
    def test_deliberately_unsubscribed_events_produce_nothing(
        self, event_type: DomainEventType
    ) -> None:
        """Each of these omissions is a decision recorded in `EVENT_RULES`."""
        assert rule_for(event_type) is None

    def test_the_feature_never_reacts_to_its_own_events(self) -> None:
        """The absence of a rule is what makes a feedback loop impossible."""
        assert rule_for(DomainEventType.NOTIFICATION_CREATED) is None
        assert rule_for(DomainEventType.NOTIFICATION_READ) is None

    def test_only_a_password_reset_is_critical(self) -> None:
        """Priority influences presentation; reserving the top of it for one rule
        is what keeps that meaningful."""
        critical = {
            rule.key
            for rule in RULES_BY_KEY.values()
            if rule.priority is NotificationPriority.CRITICAL
        }
        assert critical == {"user.password_reset"}


# --------------------------------------------------------------------------- #
# Refinement
# --------------------------------------------------------------------------- #


def _event(
    event_type: DomainEventType, **payload: object
) -> DomainEvent:
    return DomainEvent.create(
        event_type=event_type,
        topic=case_topic(uuid.uuid4()),
        sequence=1,
        payload=payload,
    )


class TestRuleRefinement:
    def test_an_assignment_becomes_assigned_or_unassigned(self) -> None:
        assert (
            rule_for(DomainEventType.CASE_ASSIGNMENT_CHANGED, {"assigned": True})
            is RULE_CASE_ASSIGNED
        )
        assert (
            rule_for(DomainEventType.CASE_ASSIGNMENT_CHANGED, {"assigned": False})
            is RULE_CASE_UNASSIGNED
        )

    def test_an_assignment_with_no_flag_falls_back_to_assigned(self) -> None:
        """The table's default entry, for an event that does not say."""
        assert rule_for(DomainEventType.CASE_ASSIGNMENT_CHANGED, {}) is RULE_CASE_ASSIGNED

    def test_a_court_field_update_becomes_hearing_news(self) -> None:
        rule = rule_for(DomainEventType.CASE_UPDATED, {"fields": ["next hearing date"]})
        assert rule is RULE_HEARING_UPDATED
        assert rule.category is NotificationCategory.HEARING
        assert rule.preference is NotificationPreferenceKey.HEARING_UPDATES

    def test_an_ordinary_update_stays_case_news(self) -> None:
        rule = rule_for(DomainEventType.CASE_UPDATED, {"fields": ["title", "description"]})
        assert rule is not None
        assert rule.category is NotificationCategory.CASE
        assert rule.preference is NotificationPreferenceKey.CASE_UPDATES

    def test_a_renamed_field_label_degrades_to_case_news(self) -> None:
        """The refinement reads another module's wording, so it must fail soft:
        a less specific notification, never a missing one."""
        rule = rule_for(DomainEventType.CASE_UPDATED, {"fields": ["prochaine audience"]})
        assert rule is not None
        assert rule.category is NotificationCategory.CASE

    def test_moving_to_waiting_for_hearing_becomes_hearing_news(self) -> None:
        rule = rule_for(
            DomainEventType.CASE_STATUS_CHANGED, {"status": "waiting_for_hearing"}
        )
        assert rule is RULE_HEARING_SCHEDULED
        assert rule.priority is NotificationPriority.HIGH

    def test_any_other_status_change_stays_case_news(self) -> None:
        rule = rule_for(DomainEventType.CASE_STATUS_CHANGED, {"status": "closed"})
        assert rule is not None
        assert rule.category is NotificationCategory.CASE


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #


class TestTargets:
    def test_a_case_rule_opens_its_case(self) -> None:
        case_id = uuid.uuid4()
        rule = EVENT_RULES[DomainEventType.CASE_CREATED]
        target = target_for(rule, case_id=case_id)
        assert target is not None
        assert target.target_type is NotificationTargetType.CASE
        assert target.target_id == case_id

    def test_a_document_rule_opens_the_document_its_payload_names(self) -> None:
        document_id = uuid.uuid4()
        rule = EVENT_RULES[DomainEventType.DOCUMENT_UPLOADED]
        target = target_for(
            rule, case_id=uuid.uuid4(), payload={"document_id": str(document_id)}
        )
        assert target is not None
        assert target.target_type is NotificationTargetType.DOCUMENT
        assert target.target_id == document_id

    def test_an_account_rule_names_no_identifier(self) -> None:
        """An account target names the reader, so an identifier would be
        redundant — and would be a second place for it to be wrong."""
        rule = EVENT_RULES[DomainEventType.USER_ACTIVATED]
        target = target_for(rule, case_id=None)
        assert target is not None
        assert target.target_type is NotificationTargetType.ACCOUNT
        assert target.target_id is None

    def test_a_withdrawn_document_offers_nothing_to_open(self) -> None:
        """Offering to open a deleted document would offer a 404."""
        rule = EVENT_RULES[DomainEventType.DOCUMENT_DELETED]
        assert target_for(rule, case_id=uuid.uuid4()) is None

    def test_being_unassigned_offers_nothing_to_open(self) -> None:
        """By the time this is read the case is no longer theirs."""
        assert target_for(RULE_CASE_UNASSIGNED, case_id=uuid.uuid4()) is None

    def test_a_missing_identifier_produces_no_target(self) -> None:
        rule = EVENT_RULES[DomainEventType.DOCUMENT_UPLOADED]
        assert target_for(rule, case_id=uuid.uuid4(), payload={}) is None
        assert (
            target_for(rule, case_id=uuid.uuid4(), payload={"document_id": "not-a-uuid"})
            is None
        )

    def test_no_target_ever_carries_a_url(self) -> None:
        """`16-notifications.md`: navigation must stay independent of frontend
        routing. A resource pair is what makes that structural."""
        for rule in RULES_BY_KEY.values():
            assert rule.target_type is None or isinstance(
                rule.target_type, NotificationTargetType
            )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestRendering:
    def test_a_notification_renders_in_each_language(self) -> None:
        for language in LANGUAGES:
            rendered = render_notification(
                rule_key="case.created",
                category=NotificationCategory.CASE,
                context={"case_number": "CASE-2026-0007"},
                language=language,
            )
            assert "CASE-2026-0007" in rendered.message
            assert rendered.title.strip()

    def test_the_same_row_renders_differently_per_language(self) -> None:
        """The whole reason no prose is stored: a reader who switches to Arabic
        sees their *history* in Arabic, not only what arrives afterwards."""
        french = render_notification(
            rule_key="case.created",
            category=NotificationCategory.CASE,
            context={"case_number": "CASE-2026-0007"},
            language=LANGUAGE_FRENCH,
        )
        arabic = render_notification(
            rule_key="case.created",
            category=NotificationCategory.CASE,
            context={"case_number": "CASE-2026-0007"},
            language=LANGUAGE_ARABIC,
        )
        assert french.title != arabic.title

    def test_a_status_is_translated_rather_than_interpolated_raw(self) -> None:
        rendered = render_notification(
            rule_key="case.status_changed",
            category=NotificationCategory.CASE,
            context={
                "case_number": "CASE-2026-0007",
                "status": "in_progress",
                "previous_status": "open",
            },
            language=LANGUAGE_FRENCH,
        )
        assert "en cours" in rendered.message
        assert "in_progress" not in rendered.message

    def test_an_unmapped_status_renders_as_itself(self) -> None:
        """A status added to the case module and not here produces a slightly raw
        notification, never a broken one."""
        rendered = render_notification(
            rule_key="case.status_changed",
            category=NotificationCategory.CASE,
            context={
                "case_number": "CASE-2026-0007",
                "status": "under_appeal",
                "previous_status": "closed",
            },
            language=LANGUAGE_FRENCH,
        )
        assert "under_appeal" in rendered.message

    def test_a_missing_context_value_renders_as_a_dash(self) -> None:
        rendered = render_notification(
            rule_key="case.created", category=NotificationCategory.CASE, context={}
        )
        assert MISSING_VALUE in rendered.message

    def test_an_unknown_rule_falls_back_to_its_category(self) -> None:
        """A row written by a later version of the platform must not take a
        history page down with it."""
        rendered = render_notification(
            rule_key="case.subpoenaed", category=NotificationCategory.CASE
        )
        assert rendered.title.strip()
        assert rendered.message.strip()

    def test_an_unknown_category_still_renders(self) -> None:
        rendered = render_notification(rule_key="mystery", category="deposition")
        assert rendered.title.strip()

    def test_every_rule_renders_without_a_context(self) -> None:
        """No template may raise on a payload that lost a field."""
        for rule in RULES_BY_KEY.values():
            for language in LANGUAGES:
                rendered = render_notification(
                    rule_key=rule.key, category=rule.category, language=language
                )
                assert rendered.title.strip()
                assert rendered.message.strip()

    def test_a_long_announcement_is_clipped_rather_than_overflowing(self) -> None:
        rendered = render_notification(
            rule_key="system.announcement",
            category=NotificationCategory.SYSTEM,
            context={"message": "x" * (MAX_MESSAGE_LENGTH * 2)},
        )
        assert len(rendered.message) <= MAX_MESSAGE_LENGTH

    def test_an_unsupported_language_falls_back_to_the_application_default(
        self,
    ) -> None:
        assert resolve_notification_language("de") == default_language()
        assert resolve_notification_language(None) == default_language()
        assert resolve_notification_language("  AR ") == LANGUAGE_ARABIC


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


class TestContext:
    def test_only_the_keys_a_rule_names_are_carried(self) -> None:
        """A context is *stored*, so 'payloads should remain lightweight' applies
        at least as strongly to it."""
        rule = EVENT_RULES[DomainEventType.CASE_CREATED]
        context = build_context(
            rule, {"case_number": "CASE-2026-0007", "title": "Benali v. Atlas"}
        )
        assert context == {"case_number": "CASE-2026-0007"}

    def test_identifiers_and_times_are_rendered_json_safe(self) -> None:
        identifier = uuid.uuid4()
        context = normalize_context({"id": identifier, "count": 3, "flag": True})
        assert context == {"id": str(identifier), "count": 3, "flag": True}

    def test_collections_are_dropped_rather_than_stringified(self) -> None:
        """`str(["a", "b"])` in the middle of a sentence is a bug shown to a user."""
        assert normalize_context({"fields": ["a", "b"], "nested": {"x": 1}}) == {}

    def test_long_values_are_clipped_rather_than_refused(self) -> None:
        """The opposite of the dispatcher's choice, and deliberate: an event
        nobody can build is an event nobody misses, while a notification nobody
        can build is a person not being told."""
        context = normalize_context({"case_number": "C" * (MAX_CONTEXT_VALUE_LENGTH * 2)})
        assert len(context["case_number"]) == MAX_CONTEXT_VALUE_LENGTH

    def test_an_absent_payload_produces_an_empty_context(self) -> None:
        assert normalize_context(None) == {}
        assert build_context(EVENT_RULES[DomainEventType.CASE_CREATED], None) == {}


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


class TestDedupeKey:
    def test_the_same_notification_produces_the_same_key(self) -> None:
        case_id = uuid.uuid4()
        first = dedupe_key(rule_key="case.updated", case_id=case_id)
        second = dedupe_key(rule_key="case.updated", case_id=case_id)
        assert first == second

    def test_a_different_case_produces_a_different_key(self) -> None:
        assert dedupe_key(rule_key="case.updated", case_id=uuid.uuid4()) != dedupe_key(
            rule_key="case.updated", case_id=uuid.uuid4()
        )

    def test_a_discriminator_separates_otherwise_identical_notifications(self) -> None:
        """What makes two announcements of the same wording two announcements."""
        assert dedupe_key(rule_key="system.announcement", discriminator="a") != dedupe_key(
            rule_key="system.announcement", discriminator="b"
        )

    def test_the_key_carries_no_identifier_in_plain_text(self) -> None:
        """A key in a log line or an index dump must not be a case identifier."""
        case_id = uuid.uuid4()
        key = dedupe_key(rule_key="case.updated", case_id=case_id)
        assert str(case_id) not in key
        assert key.startswith("case.updated|")

    def test_the_key_is_bounded(self) -> None:
        """The column is `VARCHAR(200)`; a hash is what keeps that true whatever
        a rule key grows to."""
        assert len(dedupe_key(rule_key="a" * 100, case_id=uuid.uuid4())) <= 200


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class TestPreferences:
    def test_a_known_key_resolves(self) -> None:
        assert preference_from_value("case_updates") is NotificationPreferenceKey.CASE_UPDATES

    def test_an_unknown_key_is_dropped_rather_than_raising(self) -> None:
        """The registry is open: a key written by a later version must not make an
        earlier one unable to load somebody's settings."""
        assert preference_from_value("hearing_reminders") is None


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #


class TestAnnouncements:
    def test_maintenance_is_a_high_priority_warning(self) -> None:
        rule = ANNOUNCEMENT_RULES[AnnouncementKind.MAINTENANCE]
        assert rule.notification_type is NotificationType.WARNING
        assert rule.priority is NotificationPriority.HIGH

    def test_an_announcement_renders_its_message_verbatim(self) -> None:
        rendered = render_notification(
            rule_key="system.announcement",
            category=NotificationCategory.SYSTEM,
            context={"message": "The platform will be read-only on Sunday."},
            language=LANGUAGE_ENGLISH,
        )
        assert rendered.message == "The platform will be read-only on Sunday."
