"""Tests for the settings vocabulary and its validation.

:mod:`core.settings` is pure data plus one validator, so these tests need no
database, no session, and no user — the same shape
``tests/unit/test_dashboard_utils.py`` and ``tests/unit/test_notification_utils.py``
have.

What is asserted here is the part of ``20-settings.md`` that has to be true
*before* anything is persisted: that the registries are internally consistent,
that a section cannot go missing, that administrator settings and user settings
share no key, and that :func:`~core.settings.validate_setting` refuses everything
the spec's Validation section says it must.
"""

from __future__ import annotations

import pytest

from core.dashboard import WidgetKey
from core.settings import (
    PLATFORM_DEFAULT_FOR,
    PLATFORM_SETTINGS,
    SECTIONS,
    USER_SETTINGS,
    DateFormatPreference,
    InvalidSettingError,
    PlatformSettingKey,
    SettingsSection,
    SettingValueType,
    ThemePreference,
    UserSettingKey,
    default_platform_value,
    default_user_value,
    platform_setting_from_value,
    user_setting_from_value,
    validate_setting,
)


class TestRegistries:
    def test_every_user_setting_has_a_descriptor(self) -> None:
        # A key with no descriptor could be written with no validation and read
        # with no default, which is the one way this feature could corrupt a
        # preference. Keyed by an enum so the check is total.
        assert set(USER_SETTINGS) == set(UserSettingKey)

    def test_every_platform_setting_has_a_descriptor(self) -> None:
        assert set(PLATFORM_SETTINGS) == set(PlatformSettingKey)

    def test_every_section_says_where_it_stores_things(self) -> None:
        # A section with no entry would silently vanish from every client's
        # navigation, because the API builds it from this map.
        assert set(SECTIONS) == set(SettingsSection)

    def test_the_two_registries_share_no_key(self) -> None:
        """``20-settings.md``: administrator settings stay isolated from user ones.

        The strongest form of that is a key appearing in exactly one registry, so
        no lookup can resolve a platform value as somebody's preference by taking
        the wrong map.
        """
        user_keys = {key.value for key in UserSettingKey}
        platform_keys = {key.value for key in PlatformSettingKey}
        assert user_keys & platform_keys == set()

    def test_every_setting_belongs_to_a_section_that_stores_it(self) -> None:
        for key, descriptor in USER_SETTINGS.items():
            assert SECTIONS[descriptor.section].value == "user_settings", key
        for key, descriptor in PLATFORM_SETTINGS.items():
            assert SECTIONS[descriptor.section].value == "platform_settings", key

    def test_every_default_is_itself_valid(self) -> None:
        """A default that would be refused on write is a default nobody can re-set.

        The failure this catches is real and quiet: a vocabulary narrowed in a
        later release leaves the old default in place, and the settings page shows
        a value the API will not accept back.
        """
        for key, descriptor in USER_SETTINGS.items():
            assert validate_setting(descriptor, descriptor.default) == descriptor.default, key
        for key, descriptor in PLATFORM_SETTINGS.items():
            assert validate_setting(descriptor, descriptor.default) == descriptor.default, key

    def test_every_enum_setting_offers_choices(self) -> None:
        for key, descriptor in {**USER_SETTINGS, **PLATFORM_SETTINGS}.items():
            if descriptor.value_type is SettingValueType.ENUM:
                assert descriptor.choices, key

    def test_platform_defaults_map_onto_real_user_settings(self) -> None:
        for user_key, platform_key in PLATFORM_DEFAULT_FOR.items():
            assert user_key in USER_SETTINGS
            assert platform_key in PLATFORM_SETTINGS

    def test_a_platform_default_shares_its_vocabulary_with_what_it_defaults(self) -> None:
        """The two halves of a default must accept the same values.

        Otherwise an administrator could set a platform default that no user
        setting can hold, and every untouched account would silently fall back to
        the built-in one — a setting that appears to work and does nothing.
        """
        for user_key, platform_key in PLATFORM_DEFAULT_FOR.items():
            assert (
                USER_SETTINGS[user_key].value_type == PLATFORM_SETTINGS[platform_key].value_type
            )
            assert USER_SETTINGS[user_key].choices == PLATFORM_SETTINGS[platform_key].choices

    def test_dashboard_widget_choices_track_the_dashboard(self) -> None:
        # Derived from `WidgetKey` rather than restated, so a widget added to the
        # dashboard becomes selectable with no edit — and one removed there stops
        # validating here, which is the direction that matters.
        assert set(USER_SETTINGS[UserSettingKey.DASHBOARD_WIDGETS].choices) == {
            widget.value for widget in WidgetKey
        }


class TestTolerantLookups:
    def test_an_unknown_stored_key_resolves_to_none(self) -> None:
        # An open registry read tolerantly: a row written by a later version of
        # the platform must not make an earlier one unable to load somebody's
        # settings.
        assert user_setting_from_value("something_added_next_year") is None
        assert platform_setting_from_value("something_added_next_year") is None

    def test_a_known_key_resolves(self) -> None:
        assert user_setting_from_value("theme") is UserSettingKey.THEME
        assert (
            platform_setting_from_value("maintenance_mode")
            is PlatformSettingKey.MAINTENANCE_MODE
        )


class TestDefaults:
    def test_an_untouched_account_takes_the_built_in_default(self) -> None:
        assert default_user_value(UserSettingKey.THEME) == ThemePreference.DARK.value

    def test_the_platform_default_wins_when_one_is_configured(self) -> None:
        """The whole reason administrator defaults exist rather than merely being stored."""
        resolved = default_user_value(
            UserSettingKey.THEME,
            platform={PlatformSettingKey.DEFAULT_THEME: ThemePreference.LIGHT.value},
        )
        assert resolved == ThemePreference.LIGHT.value

    def test_an_invalid_platform_default_falls_back_rather_than_raising(self) -> None:
        """A settings page that will not load is worse than one showing a default.

        The same trade `render_notification` makes for a withdrawn rule.
        """
        resolved = default_user_value(
            UserSettingKey.THEME,
            platform={PlatformSettingKey.DEFAULT_THEME: "puce"},
        )
        assert resolved == ThemePreference.DARK.value

    def test_a_platform_setting_has_its_own_default(self) -> None:
        assert default_platform_value(PlatformSettingKey.MAINTENANCE_MODE) is False


class TestValidation:
    def test_a_boolean_accepts_only_a_boolean(self) -> None:
        descriptor = USER_SETTINGS[UserSettingKey.AI_STREAMING]
        assert validate_setting(descriptor, False) is False

        # **Not coerced.** JSON has a boolean; a client that sent a string got the
        # type wrong, and quietly repairing it would hide the bug until the day
        # the string was "false".
        with pytest.raises(InvalidSettingError):
            validate_setting(descriptor, "true")

    def test_an_enum_refuses_a_value_outside_its_vocabulary(self) -> None:
        descriptor = USER_SETTINGS[UserSettingKey.THEME]
        assert validate_setting(descriptor, "light") == "light"

        with pytest.raises(InvalidSettingError):
            validate_setting(descriptor, "puce")

    def test_a_date_format_accepts_every_style_it_declares(self) -> None:
        descriptor = USER_SETTINGS[UserSettingKey.DATE_FORMAT]
        for style in DateFormatPreference:
            assert validate_setting(descriptor, style.value) == style.value

    def test_text_is_trimmed_and_bounded(self) -> None:
        descriptor = PLATFORM_SETTINGS[PlatformSettingKey.MAINTENANCE_MESSAGE]
        assert validate_setting(descriptor, "  Back at 18:00  ") == "Back at 18:00"

        with pytest.raises(InvalidSettingError):
            validate_setting(descriptor, "x" * (descriptor.max_length + 1))

    def test_a_time_zone_is_checked_against_the_system_database(self) -> None:
        descriptor = USER_SETTINGS[UserSettingKey.TIMEZONE]
        assert validate_setting(descriptor, "Europe/Paris") == "Europe/Paris"

        # Checked against the standard library's tz database rather than a list in
        # the source, which would go stale every time a country changed its rules.
        with pytest.raises(InvalidSettingError):
            validate_setting(descriptor, "Middle/Earth")

        with pytest.raises(InvalidSettingError):
            validate_setting(descriptor, "   ")

    def test_a_list_refuses_an_unknown_entry(self) -> None:
        descriptor = USER_SETTINGS[UserSettingKey.DASHBOARD_WIDGETS]
        assert validate_setting(descriptor, ["my_cases", "recent_cases"]) == [
            "my_cases",
            "recent_cases",
        ]

        with pytest.raises(InvalidSettingError):
            validate_setting(descriptor, ["my_cases", "a_widget_that_does_not_exist"])

    def test_a_list_keeps_its_order_and_drops_duplicates(self) -> None:
        # Order is kept because a list of widgets is a list somebody arranged;
        # duplicates are dropped because two of the same widget is not a thing a
        # dashboard can render, and refusing the whole save over a repeated entry
        # would be pedantry rather than validation.
        descriptor = USER_SETTINGS[UserSettingKey.DASHBOARD_WIDGETS]
        assert validate_setting(
            descriptor, ["recent_cases", "my_cases", "recent_cases"]
        ) == ["recent_cases", "my_cases"]

    def test_a_list_of_non_strings_is_refused(self) -> None:
        descriptor = USER_SETTINGS[UserSettingKey.DASHBOARD_WIDGETS]
        with pytest.raises(InvalidSettingError):
            validate_setting(descriptor, [1, 2])
        with pytest.raises(InvalidSettingError):
            validate_setting(descriptor, "my_cases")

    def test_the_error_names_a_reason_and_never_the_value(self) -> None:
        """A rejected value is the user's own text and must not travel in an error.

        The message is a *reason*, so it stays renderable in the reader's language
        and carries nothing about the person who typed it.
        """
        descriptor = USER_SETTINGS[UserSettingKey.TIMEZONE]
        with pytest.raises(InvalidSettingError) as raised:
            validate_setting(descriptor, "Atlantis/Capital")

        assert "Atlantis" not in raised.value.reason
        assert raised.value.reason
