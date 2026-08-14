"""Unit tests for the language directory.

The seam ``21-localization.md`` added and ``progress-tracker.md`` had been
recording the absence of since the email channel shipped: *which language does
this account read in?* The tests below cover the three things that were decisions
rather than mechanics —

* the **candidate order** (the account's own choice, then the platform's default,
  then the channel's, then the application's);
* the difference between :meth:`language_for` and :meth:`chosen_language_for`,
  which is what keeps question-language detection alive for an account that has
  chosen nothing;
* that a **failed lookup resolves rather than raises**, because a notification
  that did not go out because a settings query timed out is a far worse outcome
  than one that went out in the default language.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from core.localization import default_language
from core.settings import PlatformSettingKey, UserSettingKey
from repositories.localization import LocalizationRepository
from repositories.settings import SettingsRepository
from services.localization import (
    SettingsLanguageDirectory,
    StaticLanguageDirectory,
    build_language_directory,
    chosen_language,
    resolve_actor_language,
)


@pytest.fixture
def directory(db_session: Session) -> SettingsLanguageDirectory:
    return SettingsLanguageDirectory(LocalizationRepository(db_session))


@pytest.fixture
def store(db_session: Session) -> SettingsRepository:
    return SettingsRepository(db_session)


class TestStaticDirectory:
    def test_it_answers_with_one_language_for_everybody(self) -> None:
        static = StaticLanguageDirectory("ar")
        assert static.language_for(uuid.uuid4()) == "ar"

    def test_an_unsupported_configuration_falls_back(self) -> None:
        assert StaticLanguageDirectory("klingon").language_for(uuid.uuid4()) == (
            default_language()
        )

    def test_a_configured_constant_is_nobody_choice(self) -> None:
        """The distinction that keeps detection alive: a deployment default is not
        somebody's preference, so an AI surface asking this gets *"nobody chose"*
        and reads the question instead."""
        assert StaticLanguageDirectory("ar").chosen_language_for(uuid.uuid4()) is None


class TestStoredPreferences:
    def test_a_stored_choice_is_returned(
        self, directory: SettingsLanguageDirectory, store: SettingsRepository, make_user
    ) -> None:
        user = make_user(email="ar@firm.example")
        store.set_user_settings(user.id, {UserSettingKey.LANGUAGE.value: "ar"})

        assert directory.language_for(user.id) == "ar"
        assert directory.chosen_language_for(user.id) == "ar"

    def test_an_account_that_has_chosen_nothing_gets_the_default(
        self, directory: SettingsLanguageDirectory, make_user
    ) -> None:
        """*"No stored row"* is the platform's representation of "has not chosen",
        so an untouched account follows the default with no backfill."""
        user = make_user(email="quiet@firm.example")

        assert directory.language_for(user.id) == default_language()
        assert directory.chosen_language_for(user.id) is None

    def test_the_platform_default_outranks_the_channel_default(
        self, db_session: Session, store: SettingsRepository, make_user
    ) -> None:
        """An administrator's platform-wide choice should reach every account that
        has expressed none — which is what makes a `default_*` setting *do*
        something rather than merely be stored."""
        user = make_user(email="anyone@firm.example")
        store.set_platform_settings(
            {PlatformSettingKey.DEFAULT_LANGUAGE.value: "ar"}, updated_by=None
        )

        directory = SettingsLanguageDirectory(
            LocalizationRepository(db_session), channel_default="fr"
        )
        assert directory.language_for(user.id) == "ar"

    def test_a_personal_choice_outranks_the_platform_default(
        self, db_session: Session, store: SettingsRepository, make_user
    ) -> None:
        user = make_user(email="mine@firm.example")
        store.set_platform_settings(
            {PlatformSettingKey.DEFAULT_LANGUAGE.value: "ar"}, updated_by=None
        )
        store.set_user_settings(user.id, {UserSettingKey.LANGUAGE.value: "fr"})

        directory = SettingsLanguageDirectory(LocalizationRepository(db_session))
        assert directory.language_for(user.id) == "fr"

    def test_a_batch_is_resolved_for_every_account(
        self, directory: SettingsLanguageDirectory, store: SettingsRepository, make_user
    ) -> None:
        """A delivery channel hands over every recipient at once; each has to come
        back with an answer, including the ones that chose nothing."""
        chose = make_user(email="chose@firm.example")
        quiet = make_user(email="silent@firm.example")
        store.set_user_settings(chose.id, {UserSettingKey.LANGUAGE.value: "ar"})

        resolved = directory.languages_for([chose.id, quiet.id])

        assert resolved[chose.id] == "ar"
        assert resolved[quiet.id] == default_language()

    def test_an_unsupported_stored_value_is_discarded(
        self, directory: SettingsLanguageDirectory, store: SettingsRepository, make_user
    ) -> None:
        """A row written by a later version of the platform travels out of the
        repository intact and is discarded here rather than making a page fail to
        load."""
        user = make_user(email="future@firm.example")
        store.set_user_settings(user.id, {UserSettingKey.LANGUAGE.value: "en"})

        # Written past validation, which is the only way to produce the row a
        # *later* version of the platform would write — `validate_setting` refuses
        # it on every ordinary path.
        from sqlalchemy import select

        from models.settings import UserSetting

        setting = (
            store._session.execute(  # noqa: SLF001 - deliberately reaching past validation
                select(UserSetting).where(UserSetting.user_id == user.id)
            )
            .scalars()
            .one()
        )
        setting.value = "klingon"
        store._session.commit()  # noqa: SLF001

        assert directory.language_for(user.id) == default_language()
        assert directory.chosen_language_for(user.id) is None


class TestFailuresResolve:
    class _BrokenRepository:
        def stored_languages(self, user_ids):  # type: ignore[no-untyped-def]
            raise RuntimeError("database is unreachable")

        def platform_default_language(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("database is unreachable")

    def test_an_unreachable_database_costs_the_preference_and_not_the_message(
        self,
    ) -> None:
        """*"Failures should gracefully fall back to the default language."* The
        message still goes out, in the channel's own language."""
        directory = SettingsLanguageDirectory(
            self._BrokenRepository(),  # type: ignore[arg-type]
            channel_default="ar",
        )

        assert directory.language_for(uuid.uuid4()) == "ar"
        assert directory.chosen_language_for(uuid.uuid4()) is None


class TestHelpers:
    def test_an_explicit_request_overrides_the_stored_preference(
        self, directory: SettingsLanguageDirectory, store: SettingsRepository, make_user
    ) -> None:
        """*"An explicit request should override the default for that interaction
        only."* It is a parameter and nothing here writes anything, so the account
        is unchanged."""
        user = make_user(email="override@firm.example")
        store.set_user_settings(user.id, {UserSettingKey.LANGUAGE.value: "ar"})

        assert resolve_actor_language(directory, user.id, requested="fr") == "fr"
        assert directory.chosen_language_for(user.id) == "ar"

    def test_an_unsupported_request_falls_through_to_the_preference(
        self, directory: SettingsLanguageDirectory, store: SettingsRepository, make_user
    ) -> None:
        user = make_user(email="bad-request@firm.example")
        store.set_user_settings(user.id, {UserSettingKey.LANGUAGE.value: "ar"})

        assert resolve_actor_language(directory, user.id, requested="de") == "ar"

    def test_no_directory_at_all_resolves_rather_than_raising(self) -> None:
        """A service constructed by a script or a unit test has no directory, and
        must still be able to compose a sentence."""
        assert resolve_actor_language(None, uuid.uuid4()) == default_language()
        assert chosen_language(None, uuid.uuid4()) is None


class TestConstruction:
    def test_the_worker_thread_factory_builds_a_real_directory(
        self, db_session: Session, store: SettingsRepository, make_user
    ) -> None:
        """The counterpart of `api.deps.get_language_directory`: a background
        worker has no request to resolve a dependency from, so both paths exist and
        both have to be right."""
        user = make_user(email="worker@firm.example")
        store.set_user_settings(user.id, {UserSettingKey.LANGUAGE.value: "ar"})

        directory = build_language_directory(db_session, channel_default="fr")
        assert directory.language_for(user.id) == "ar"

    def test_a_directory_cannot_write(self) -> None:
        """The reason a delivery channel is handed this rather than a settings
        repository: it can ask which language somebody reads in and can never
        change it."""
        for name in dir(SettingsLanguageDirectory):
            assert not name.startswith("set_")
            assert not name.startswith("update_")
