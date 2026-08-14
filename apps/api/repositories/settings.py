"""Settings persistence.

Data access only: this module holds no rule about what a valid setting is, who
may change one, or what "no stored row" means. Those live in
:mod:`core.settings` (validation and defaults) and :mod:`services.settings`
(authorization and workflow), which is the layering every other repository on
this platform follows.

**Two stores, no shared query.** :class:`SettingsRepository` reads and writes
both tables, but never in one statement and never through one method that could
be handed the wrong scope — a user read takes a ``user_id`` and a platform read
takes none, so the isolation ``20-settings.md`` requires cannot be lost to a
missing argument.

**Read-then-write rather than a dialect-specific upsert**, exactly as
:meth:`~repositories.notification.NotificationRepository.set_preferences` does
and for the same reason: the platform's test database is SQLite and its
production database is PostgreSQL, and ``ON CONFLICT`` is spelled differently on
each. The race that leaves open is one person saving from two tabs at the same
moment, which the unique constraint turns into an integrity error rather than a
duplicate — the correct outcome, surfaced by the service as a retryable failure
rather than as a silently lost setting.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.settings import PlatformSetting, UserSetting


@dataclass(frozen=True, slots=True)
class SettingsStatistics:
    """What the database knows about stored settings.

    Three counts and no breakdown by key, deliberately. *"Which settings are
    people changing?"* is a legitimate product question and *"who has changed
    theme?"* is a statement about individuals; a breakdown keyed by setting is one
    ``GROUP BY`` away from the second, and the monitoring view this feeds is the
    same one ``notifications:monitor`` refuses to make into an index of people.
    """

    #: Rows in ``user_settings``: individual choices people have made.
    stored_user_settings: int
    #: Distinct accounts with at least one. Every other account follows the
    #: platform defaults, which is what makes this the interesting number.
    customised_users: int
    #: Rows in ``platform_settings``: how much of the deployment has been
    #: configured away from its built-in answer.
    stored_platform_settings: int


class SettingsRepository:
    """Reads and writes the two settings tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------- user settings #

    def user_settings_for(self, user_id: uuid.UUID) -> dict[str, Any]:
        """Every setting this person has actually expressed an opinion about.

        Keyed by the **stored string** rather than by
        :class:`~core.settings.UserSettingKey`, so a row written by a later
        version of the platform travels out of here intact and the service layer
        decides — tolerantly — what to do with a key it does not recognise. A
        repository that resolved the enum here would raise on exactly the row an
        older instance most needs to ignore.

        Returns:
            ``setting_key → value``. Empty for an account that has never changed
            anything, which is the ordinary case and is **not** an error.
        """
        rows = (
            self._session.execute(
                select(UserSetting).where(UserSetting.user_id == user_id)
            )
            .scalars()
            .all()
        )
        return {row.setting_key: row.value for row in rows}

    def set_user_settings(
        self, user_id: uuid.UUID, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Store this person's choices, creating or updating one row per key.

        **Only the keys supplied are written.** Anything omitted keeps its current
        value — or keeps having no row, and therefore keeps following the platform
        default. That is what lets two settings panels open at once avoid
        reverting each other's saves, and what stops a client written before a
        setting existed from resetting it by not mentioning it: the same
        protection ``17-email-delivery-channel.md`` bought for notification
        channels, applied here from the start rather than after a channel needed
        it.

        **A value equal to what is already stored is not rewritten.** The spec's
        Performance section asks to *"minimize unnecessary updates"* and *"avoid
        duplicate persistence"*, and this is where that is enforced: a settings
        page that saves on every keystroke, or re-sends its whole form, produces
        no statement at all when nothing actually changed — and ``updated_at``
        keeps meaning *when this setting last changed* rather than *when somebody
        last opened the page*.

        Values are expected to have been validated by the caller; this method
        writes what it is given. See :func:`~core.settings.validate_setting` and
        :class:`~services.settings.SettingsService` for where that happens and why
        it happens to the whole batch before any of it is written.

        Returns:
            The person's complete stored settings after the write.
        """
        if not values:
            return self.user_settings_for(user_id)

        existing = {
            row.setting_key: row
            for row in self._session.execute(
                select(UserSetting).where(
                    UserSetting.user_id == user_id,
                    UserSetting.setting_key.in_(list(values)),
                )
            )
            .scalars()
            .all()
        }

        changed = False
        for key, value in values.items():
            row = existing.get(key)
            if row is None:
                self._session.add(
                    UserSetting(user_id=user_id, setting_key=key, value=value)
                )
                changed = True
            elif row.value != value:
                row.value = value
                changed = True

        if changed:
            self._session.commit()

        return self.user_settings_for(user_id)

    # --------------------------------------------------- platform settings #

    def platform_settings(self) -> dict[str, Any]:
        """Every platform setting an administrator has actually configured.

        The whole table, unfiltered, because there are at most as many rows as the
        registry has keys — see the migration for why this deliberately carries no
        index beyond the unique one on the key.
        """
        rows = self._session.execute(select(PlatformSetting)).scalars().all()
        return {row.setting_key: row.value for row in rows}

    def set_platform_settings(
        self, values: Mapping[str, Any], *, updated_by: uuid.UUID | None
    ) -> dict[str, Any]:
        """Store the deployment's configuration, one row per key.

        Partial in exactly the way :meth:`set_user_settings` is, and unchanged
        values are skipped for the same reason — with one extra consequence worth
        naming: ``updated_by`` records *who changed this setting*, so rewriting a
        row with the value it already had would attribute somebody else's decision
        to whoever opened the page last.

        Returns:
            The complete stored platform configuration after the write.
        """
        if not values:
            return self.platform_settings()

        existing = {
            row.setting_key: row
            for row in self._session.execute(
                select(PlatformSetting).where(
                    PlatformSetting.setting_key.in_(list(values))
                )
            )
            .scalars()
            .all()
        }

        changed = False
        for key, value in values.items():
            row = existing.get(key)
            if row is None:
                self._session.add(
                    PlatformSetting(setting_key=key, value=value, updated_by=updated_by)
                )
                changed = True
            elif row.value != value:
                row.value = value
                row.updated_by = updated_by
                changed = True

        if changed:
            self._session.commit()

        return self.platform_settings()

    # ------------------------------------------------------------- metrics #

    def statistics(self) -> SettingsStatistics:
        """Counts of what is actually stored.

        SQL aggregates rather than process counters, for the reason
        ``19-dashboard-analytics.md`` gives about queue depths and
        :mod:`services.notification_metrics` gives about row counts: *"how many
        people have customised anything?"* is a property of the database, and a
        number counted in one API instance's memory would reset on deploy and be
        wrong across replicas. The **rates** — updates, failures — cannot be
        aggregates and are counted in the process; see
        :mod:`services.settings_metrics` for that half.
        """
        stored_user_settings = (
            self._session.execute(select(func.count()).select_from(UserSetting)).scalar()
            or 0
        )
        customised_users = (
            self._session.execute(
                select(func.count(func.distinct(UserSetting.user_id)))
            ).scalar()
            or 0
        )
        stored_platform_settings = (
            self._session.execute(
                select(func.count()).select_from(PlatformSetting)
            ).scalar()
            or 0
        )

        return SettingsStatistics(
            stored_user_settings=int(stored_user_settings),
            customised_users=int(customised_users),
            stored_platform_settings=int(stored_platform_settings),
        )


__all__ = ["SettingsRepository", "SettingsStatistics"]
