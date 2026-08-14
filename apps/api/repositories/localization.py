"""Language-preference persistence.

Data access only, and deliberately **one question wide**: *which language does
this account read in?* — plus the aggregate the monitoring view needs to answer
*"how is the platform's user base distributed across languages?"*

**Why this is not a method on :class:`~repositories.settings.SettingsRepository`**,
which owns the table it reads. That repository serves the *Settings page*: its
reads are keyed by one account, return every stored key, and are consumed by a
service that has already authorized the caller as the owner of those rows. The
readers here are different in every respect — an email batch resolving a hundred
recipients at once, a WhatsApp batch doing the same, and a metrics endpoint
counting everybody — and none of them is a person looking at their own settings.
Borrowing ``user_settings_for`` would mean one query per recipient inside a
delivery batch, which is the shape
:meth:`~repositories.email.EmailDeliveryRepository.recipient_profiles` exists to
avoid one column over.

**It reads and never writes.** A language preference is written exactly one way —
through the Settings API, by its owner, validated against
:mod:`core.settings` — and this repository having no write method is what keeps a
delivery channel or a metrics endpoint from acquiring the ability to change
somebody's preference while resolving it.

**Nothing here decides anything.** The rows it returns are candidates;
:mod:`core.localization` turns them into a language and
:mod:`services.localization` decides the order they are tried in. That split is
what makes *"localization must never affect authorization, RBAC, routing,
database schema, business rules, or workflow execution"* structural for this
module: it has no scope to widen, because every method takes the identifiers it
is given and returns a string per identifier.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.settings import PlatformSettingKey, UserSettingKey
from models.settings import PlatformSetting, UserSetting

#: The stored key a language preference lives under, in both registries.
#:
#: Read from :mod:`core.settings` rather than spelled as a literal, so a key
#: renamed there cannot leave this module silently reading a column that is always
#: empty — which would present as *"everybody suddenly gets English"* rather than
#: as an error.
_USER_LANGUAGE_KEY = UserSettingKey.LANGUAGE.value
_PLATFORM_LANGUAGE_KEY = PlatformSettingKey.DEFAULT_LANGUAGE.value


class LocalizationRepository:
    """Reads stored language preferences, one account or one platform at a time."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ---------------------------------------------------------- preferences #

    def stored_languages(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """The language each of these accounts has actually chosen, in one query.

        **Only accounts with a stored row appear.** An account that has never
        opened the Settings page has no row at all — the representation
        ``20-settings.md`` chose for *"has not chosen"* — and is absent from the
        result rather than present with a default. That distinction is the whole
        reason this method exists in this shape: the caller needs to tell *"this
        person wants Arabic"* apart from *"this person has said nothing"*, because
        the second falls through to the platform's answer and the first does not.

        Values are returned **as stored**, not validated. A row written by a later
        version of the platform naming a language this one does not serve travels
        out intact and is discarded by
        :func:`~core.localization.normalize_language` — the same tolerance
        :meth:`~repositories.settings.SettingsRepository.user_settings_for`
        records, and for the same reason.
        """
        if not user_ids:
            return {}

        rows = self._session.execute(
            select(UserSetting.user_id, UserSetting.value).where(
                UserSetting.user_id.in_(list(user_ids)),
                UserSetting.setting_key == _USER_LANGUAGE_KEY,
            )
        ).all()

        return {
            user_id: value
            for user_id, value in rows
            # A JSON column can hold anything the registry admits; a language is a
            # string, and a row holding a list or a boolean is a corrupt row rather
            # than a preference. Skipping it lets the account fall through to the
            # platform default, which is the readable outcome.
            if isinstance(value, str)
        }

    def platform_default_language(self) -> str | None:
        """The default language an administrator has configured, if any.

        ``None`` when nothing is stored, which means the deployment's
        ``DEFAULT_LANGUAGE`` applies — the same "no row means the built-in answer"
        rule every platform setting follows, kept here rather than resolved so the
        caller can put it in its own candidate order.
        """
        value = self._session.execute(
            select(PlatformSetting.value).where(
                PlatformSetting.setting_key == _PLATFORM_LANGUAGE_KEY
            )
        ).scalar_one_or_none()
        return value if isinstance(value, str) else None

    # ------------------------------------------------------------- metrics #

    def language_distribution(self) -> dict[str, int]:
        """How many **active** accounts have explicitly chosen each language.

        ``21-localization.md`` asks for *"language distribution"* under Monitoring,
        and this is the half of it that is a property of the database rather than
        of a process — the same split :mod:`services.notification_metrics` and
        :mod:`services.settings_metrics` already make.

        Three decisions worth stating, because each was one:

        * **Explicit choices only.** Accounts with no stored row are reported
          separately (see :meth:`accounts_following_default`) rather than folded
          into whichever language is currently the default, because *"how many
          people chose Arabic"* and *"how many people have never opened the
          settings page"* are different questions and an administrator deciding
          whether to translate something needs both.
        * **Active accounts only.** A distribution that counted deactivated
          accounts would describe a user base the platform no longer has, and it
          is the same restriction
          :meth:`~repositories.email.EmailDeliveryRepository.recipient_profiles`
          applies for a related reason.
        * **Counts, never identities.** There is deliberately no method here that
          returns *who* reads in Arabic. ``code-standards.md`` forbids a
          per-account breakdown of a preference for the reason a per-recipient
          notification metric is forbidden: it would be a live index of who is
          who, assembled by accident.
        """
        from models.user import User, UserStatus

        rows = self._session.execute(
            select(UserSetting.value, func.count())
            .join(User, User.id == UserSetting.user_id)
            .where(
                UserSetting.setting_key == _USER_LANGUAGE_KEY,
                User.status == UserStatus.ACTIVE,
            )
            .group_by(UserSetting.value)
        ).all()

        distribution: dict[str, int] = {}
        for value, count in rows:
            if isinstance(value, str):
                distribution[value] = distribution.get(value, 0) + int(count)
        return distribution

    def accounts_following_default(self) -> int:
        """Active accounts that have expressed no language preference at all.

        The complement of :meth:`language_distribution`, and the figure that says
        what changing the platform default would actually reach — which is the
        operational question *"we are switching the default to Arabic; who does
        that affect?"*
        """
        from models.user import User, UserStatus

        chosen = select(UserSetting.user_id).where(
            UserSetting.setting_key == _USER_LANGUAGE_KEY
        )
        return int(
            self._session.execute(
                select(func.count())
                .select_from(User)
                .where(User.status == UserStatus.ACTIVE, User.id.not_in(chosen))
            ).scalar()
            or 0
        )


__all__ = ["LocalizationRepository"]
