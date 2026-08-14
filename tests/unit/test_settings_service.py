"""Tests for the settings service.

The same no-Docker approach the rest of this suite uses: the **real** repository
runs against SQLite in memory, so the upsert, the partial write, and the
skip-if-unchanged behaviour are exercised as SQL rather than as a mock's
recollection. Only the metrics recorder and the session registry are doubles.

What is asserted here is the behaviour ``20-settings.md`` describes and the API
layer cannot: that nothing is written when part of a batch is invalid, that an
untouched account has no rows and follows the platform's answer, that a change to
a platform default reaches such an account with no backfill, and that a save which
changes nothing issues no statement.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from core.exceptions import InvalidSettingValueError, UnknownSettingError
from core.settings import (
    PlatformSettingKey,
    SettingsSection,
    ThemePreference,
    UserSettingKey,
)
from models.settings import PlatformSetting, UserSetting
from repositories.settings import SettingsRepository
from repositories.user import UserRepository
from services.settings import SettingsService
from services.settings_metrics import InMemorySettingsMetrics, SettingsFailureReason


@pytest.fixture
def settings_service(db_session: Session, revocations, throttle, session_registry):  # type: ignore[no-untyped-def]
    """The real service over the real repositories, with doubled collaborators."""
    from services.auth import AuthService

    users = UserRepository(db_session)
    auth = AuthService(users, revocations, throttle, session_registry)
    return SettingsService(
        SettingsRepository(db_session),
        users,
        auth,
        metrics=InMemorySettingsMetrics(),
    )


class TestUserSettings:
    def test_an_untouched_account_has_no_rows_and_takes_the_defaults(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        """`architecture.md`'s "no row means the default" invariant, for this table.

        It is what keeps a platform-wide change of defaults from needing a
        backfill, and it is the reason the migration seeds nothing.
        """
        user = make_user()

        resolved = settings_service.user_settings(actor=user)

        assert db_session.query(UserSetting).count() == 0
        assert set(resolved) == set(UserSettingKey)
        assert all(setting.is_default for setting in resolved.values())
        assert resolved[UserSettingKey.THEME].value == ThemePreference.DARK.value

    def test_storing_a_choice_marks_it_as_no_longer_a_default(
        self, settings_service: SettingsService, make_user
    ) -> None:
        user = make_user()

        resolved = settings_service.update_user_settings(
            [(UserSettingKey.THEME, ThemePreference.LIGHT.value)], actor=user
        )

        assert resolved[UserSettingKey.THEME].value == ThemePreference.LIGHT.value
        assert resolved[UserSettingKey.THEME].is_default is False
        # Everything untouched still has no row and still says so.
        assert resolved[UserSettingKey.LANGUAGE].is_default is True

    def test_only_the_supplied_keys_are_written(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        """Two settings panels open at once must not revert each other's saves."""
        user = make_user()

        settings_service.update_user_settings(
            [(UserSettingKey.THEME, "light")], actor=user
        )
        settings_service.update_user_settings(
            [(UserSettingKey.LANGUAGE, "ar")], actor=user
        )

        stored = {
            row.setting_key: row.value
            for row in db_session.query(UserSetting).filter_by(user_id=user.id).all()
        }
        assert stored == {"theme": "light", "language": "ar"}

    def test_a_second_save_updates_the_same_row(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        # The unique constraint is the upsert target: two saves of one key must
        # never leave somebody with two contradictory rows.
        user = make_user()

        settings_service.update_user_settings([(UserSettingKey.THEME, "light")], actor=user)
        settings_service.update_user_settings([(UserSettingKey.THEME, "system")], actor=user)

        rows = db_session.query(UserSetting).filter_by(user_id=user.id).all()
        assert len(rows) == 1
        assert rows[0].value == "system"

    def test_saving_an_unchanged_value_writes_nothing(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        """`20-settings.md`'s "minimize unnecessary updates" / "avoid duplicate persistence".

        A form that posts everything on every save produces no statement when
        nothing changed, so `updated_at` keeps meaning *when this setting last
        changed* rather than *when somebody last opened the page*.
        """
        user = make_user()
        settings_service.update_user_settings([(UserSettingKey.THEME, "light")], actor=user)

        row = db_session.query(UserSetting).filter_by(user_id=user.id).one()
        before = row.updated_at

        settings_service.update_user_settings([(UserSettingKey.THEME, "light")], actor=user)
        db_session.refresh(row)

        assert row.updated_at == before

    def test_a_setting_belongs_to_exactly_one_account(
        self, settings_service: SettingsService, make_user
    ) -> None:
        """There is no parameter naming somebody else, so this is structural.

        The assertion is still worth making: it is the property every read here
        rests on, and it would break silently if a query ever lost its scope.
        """
        amina = make_user(email="amina@example.com")
        karim = make_user(email="karim@example.com")

        settings_service.update_user_settings([(UserSettingKey.THEME, "light")], actor=amina)

        assert settings_service.user_settings(actor=karim)[UserSettingKey.THEME].is_default


class TestValidation:
    def test_an_invalid_value_writes_nothing_at_all(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        """`20-settings.md`: invalid configuration must never corrupt stored preferences.

        The batch is validated in full before a single statement is issued, so a
        form with one bad field leaves every other field exactly as it was rather
        than half-applied.
        """
        user = make_user()

        with pytest.raises(InvalidSettingValueError):
            settings_service.update_user_settings(
                [
                    (UserSettingKey.THEME, "light"),
                    (UserSettingKey.TIMEZONE, "Middle/Earth"),
                ],
                actor=user,
            )

        assert db_session.query(UserSetting).count() == 0

    def test_an_invalid_value_does_not_undo_what_was_already_stored(
        self, settings_service: SettingsService, make_user
    ) -> None:
        user = make_user()
        settings_service.update_user_settings([(UserSettingKey.THEME, "light")], actor=user)

        with pytest.raises(InvalidSettingValueError):
            settings_service.update_user_settings(
                [
                    (UserSettingKey.THEME, "system"),
                    (UserSettingKey.LANGUAGE, "klingon"),
                ],
                actor=user,
            )

        resolved = settings_service.user_settings(actor=user)
        assert resolved[UserSettingKey.THEME].value == "light"

    def test_every_offending_key_is_reported_at_once(
        self, settings_service: SettingsService, make_user
    ) -> None:
        # A settings form should be able to mark all of its bad fields at once
        # rather than one save at a time.
        user = make_user()

        with pytest.raises(InvalidSettingValueError) as raised:
            settings_service.update_user_settings(
                [
                    (UserSettingKey.TIMEZONE, "Middle/Earth"),
                    (UserSettingKey.THEME, "puce"),
                ],
                actor=user,
            )

        assert {detail.field for detail in raised.value.details} == {
            str(UserSettingKey.TIMEZONE),
            str(UserSettingKey.THEME),
        }

    def test_an_unknown_key_is_refused_rather_than_stored(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        """Strict on the way in, tolerant on the way out — the asymmetry is deliberate."""
        user = make_user()

        with pytest.raises(UnknownSettingError):
            settings_service.update_user_settings(
                [("a_setting_from_next_year", True)], actor=user
            )

        assert db_session.query(UserSetting).count() == 0

    def test_a_failure_is_counted_by_cause(
        self, db_session: Session, revocations, throttle, session_registry, make_user
    ) -> None:
        from services.auth import AuthService

        metrics = InMemorySettingsMetrics()
        users = UserRepository(db_session)
        service = SettingsService(
            SettingsRepository(db_session),
            users,
            AuthService(users, revocations, throttle, session_registry),
            metrics=metrics,
        )
        user = make_user()

        with pytest.raises(InvalidSettingValueError):
            service.update_user_settings([(UserSettingKey.THEME, "puce")], actor=user)

        snapshot = metrics.snapshot()
        assert snapshot.failed == 1
        assert snapshot.failures_by_reason == {
            SettingsFailureReason.INVALID_VALUE.value: 1
        }


class TestPlatformSettings:
    def test_a_platform_default_reaches_an_account_with_no_rows(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        """The whole reason administrator defaults exist rather than merely being stored.

        No backfill is involved, because there is nothing stored to back-fill:
        the account has no row, so it reads the platform's answer.
        """
        administrator = make_user(email="admin@example.com")
        lawyer = make_user(email="lawyer@example.com")

        settings_service.update_platform_settings(
            [(PlatformSettingKey.DEFAULT_THEME, ThemePreference.LIGHT.value)],
            actor=administrator,
        )

        resolved = settings_service.user_settings(actor=lawyer)
        assert resolved[UserSettingKey.THEME].value == ThemePreference.LIGHT.value
        # Still a default: the platform chose it, not this person.
        assert resolved[UserSettingKey.THEME].is_default is True
        assert db_session.query(UserSetting).count() == 0

    def test_a_personal_choice_outranks_the_platform_default(
        self, settings_service: SettingsService, make_user
    ) -> None:
        administrator = make_user(email="admin@example.com")
        lawyer = make_user(email="lawyer@example.com")

        settings_service.update_platform_settings(
            [(PlatformSettingKey.DEFAULT_THEME, ThemePreference.LIGHT.value)],
            actor=administrator,
        )
        settings_service.update_user_settings(
            [(UserSettingKey.THEME, ThemePreference.DARK.value)], actor=lawyer
        )

        resolved = settings_service.user_settings(actor=lawyer)
        assert resolved[UserSettingKey.THEME].value == ThemePreference.DARK.value
        assert resolved[UserSettingKey.THEME].is_default is False

    def test_who_changed_a_platform_setting_is_recorded_on_the_row(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        # "Who turned maintenance mode on?" is asked days later, from a database,
        # by somebody who does not have the application's logs.
        administrator = make_user(email="admin@example.com")

        settings_service.update_platform_settings(
            [(PlatformSettingKey.MAINTENANCE_MODE, True)], actor=administrator
        )

        row = db_session.query(PlatformSetting).one()
        assert row.updated_by == administrator.id

    def test_platform_settings_are_not_scoped_to_an_account(
        self, settings_service: SettingsService, make_user
    ) -> None:
        first = make_user(email="one@example.com")
        second = make_user(email="two@example.com")

        settings_service.update_platform_settings(
            [(PlatformSettingKey.MAINTENANCE_MODE, True)], actor=first
        )

        # There is one platform, so the second administrator reads what the first
        # set — and the row carries no user column for it to have been scoped by.
        resolved = settings_service.platform_settings()
        assert resolved[PlatformSettingKey.MAINTENANCE_MODE].value is True
        assert settings_service.user_settings(actor=second)  # unaffected, and loads

    def test_an_invalid_platform_value_writes_nothing(
        self, settings_service: SettingsService, db_session: Session, make_user
    ) -> None:
        administrator = make_user()

        with pytest.raises(InvalidSettingValueError):
            settings_service.update_platform_settings(
                [
                    (PlatformSettingKey.MAINTENANCE_MODE, True),
                    (PlatformSettingKey.DEFAULT_LANGUAGE, "klingon"),
                ],
                actor=administrator,
            )

        assert db_session.query(PlatformSetting).count() == 0


class TestMaintenance:
    def test_a_message_is_withheld_while_maintenance_is_off(
        self, settings_service: SettingsService, make_user
    ) -> None:
        """A notice typed and not yet switched on is a draft.

        Serving it would put a stale banner on everybody's screen.
        """
        administrator = make_user()
        settings_service.update_platform_settings(
            [(PlatformSettingKey.MAINTENANCE_MESSAGE, "Back at 18:00")],
            actor=administrator,
        )

        status = settings_service.maintenance_status()
        assert status.maintenance_mode is False
        assert status.message is None

    def test_the_message_is_served_once_maintenance_is_on(
        self, settings_service: SettingsService, make_user
    ) -> None:
        administrator = make_user()
        settings_service.update_platform_settings(
            [
                (PlatformSettingKey.MAINTENANCE_MESSAGE, "Back at 18:00"),
                (PlatformSettingKey.MAINTENANCE_MODE, True),
            ],
            actor=administrator,
        )

        status = settings_service.maintenance_status()
        assert status.maintenance_mode is True
        assert status.message == "Back at 18:00"


class TestProfile:
    def test_only_the_changed_fields_are_counted(
        self, db_session: Session, revocations, throttle, session_registry, make_user
    ) -> None:
        from services.auth import AuthService

        metrics = InMemorySettingsMetrics()
        users = UserRepository(db_session)
        service = SettingsService(
            SettingsRepository(db_session),
            users,
            AuthService(users, revocations, throttle, session_registry),
            metrics=metrics,
        )
        user = make_user(first_name="Amina", last_name="Benali")

        service.update_profile(
            {"first_name": "Amina", "last_name": "Nour"}, actor=user
        )

        # One field actually changed; the other already held the requested value.
        assert metrics.snapshot().profile_changes == 1
        assert user.last_name == "Nour"

    def test_a_profile_field_can_be_cleared(
        self, settings_service: SettingsService, make_user
    ) -> None:
        # An explicit `None` means "remove this", which is how somebody withdraws
        # a phone number they no longer want the platform to hold.
        user = make_user(phone="+212600000000")

        settings_service.update_profile({"phone": None}, actor=user)

        assert user.phone is None

    def test_a_job_title_is_stored(
        self, settings_service: SettingsService, make_user
    ) -> None:
        user = make_user()

        settings_service.update_profile({"job_title": "Senior Associate"}, actor=user)

        assert user.job_title == "Senior Associate"


class TestSections:
    def test_the_administration_section_is_omitted_without_the_capability(
        self, settings_service: SettingsService, make_user
    ) -> None:
        """Omitted rather than served read-only.

        Serving it disabled would tell every lawyer which platform settings exist
        and that somebody else controls them; omitting it says nothing.
        """
        user = make_user()

        sections = settings_service.sections(actor=user, can_manage=False)

        assert SettingsSection.ADMINISTRATION not in {
            descriptor.section for descriptor in sections
        }

    def test_an_administrator_is_offered_every_section_in_order(
        self, settings_service: SettingsService, make_user
    ) -> None:
        user = make_user()

        sections = settings_service.sections(actor=user, can_manage=True)

        # The member order *is* the display order, so a client renders its
        # navigation from the API rather than from a list of its own.
        assert [descriptor.section for descriptor in sections] == list(SettingsSection)

    def test_notification_sections_point_at_the_feature_that_owns_them(
        self, settings_service: SettingsService, make_user
    ) -> None:
        """`20-settings.md`: each feature owns its configuration.

        The Settings module stores nothing for these two, and the descriptor says
        so — which is what sends a client to `/notifications/preferences` instead
        of to a second endpoint here.
        """
        user = make_user()

        storage = {
            descriptor.section: descriptor.storage
            for descriptor in settings_service.sections(actor=user, can_manage=False)
        }

        assert storage[SettingsSection.NOTIFICATIONS].value == "notification_preferences"
        assert storage[SettingsSection.COMMUNICATION].value == "notification_preferences"
        assert storage[SettingsSection.PROFILE].value == "profile"
        assert storage[SettingsSection.SECURITY].value == "account"
