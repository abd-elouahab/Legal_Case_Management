"""The platform's settings vocabulary, and the rules that validate one.

``20-settings.md`` asks for a **centralized location where users and
administrators configure their preferences and platform behaviour**, and states
the rule the whole feature turns on: *"Each feature should own its
configuration. The Settings module simply presents and manages those
configurations through a unified interface."*

This module is the half of that with no I/O in it — the sections, the two
registries, the value vocabularies, and validation. Nothing here touches a
database, a session, a socket, or a user.

**Not to be confused with** :mod:`core.config`, whose ``settings`` object is the
*deployment's* configuration: environment variables, read once at import, never
written, and the same for everybody. This module is about configuration a
**person** changes at runtime and that is stored per account. The two never meet:
a value here is never read from the environment, and a value there is never
editable through the API.

Four things live here, and each is a requirement of the spec made mechanical:

* **What the Settings page is made of** — :class:`SettingsSection`, the nine the
  spec's structure diagram names, in its order. A section is a *presentation*
  unit, not a storage one: three of the nine store nothing at all here, because
  the features that own them already do.
* **What a person can configure** — :class:`UserSettingKey` and its descriptors.
  An **open registry** in storage (one row per ``(user, key)``, exactly as
  ``notification_preferences`` is), so a tenth setting is an entry below and **no
  migration** — which is the spec's *"support future sections without redesign"*
  made structural rather than promised.
* **What an administrator can configure** — :class:`PlatformSettingKey`, in a
  **separate registry** backed by a **separate table** behind a **separate
  permission**. That separation is the spec's *"administrator settings should
  remain isolated from regular user settings"*, and it is structural: there is no
  key that appears in both registries and no query that reads both.
* **What a valid value is** — :func:`validate_setting`, which every write passes
  through before anything is persisted. *"Invalid configuration should never
  corrupt stored preferences."*

**Every value is a key, never a sentence.** A theme is ``"dark"``, a date format
is ``"dmy_slash"``, an AI response length is ``"concise"`` — the words a person
reads live in the client's own translation catalogue. That is the same rule
``19-dashboard-analytics.md`` states for widget labels and ``16-notifications.md``
states for notification prose, and it exists for the same reason: an API response
is a place a translation cannot live, and this platform serves Arabic and French
from one set of rows.

**Defaults are not stored.** An account that has never opened the Settings page
has no rows at all and follows :func:`default_user_value`, which is what keeps a
platform-wide change of defaults from needing a backfill — the argument
``models/notification.py`` records for ``notification_preferences`` and the reason
this module reuses its shape rather than inventing another.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.dashboard import WidgetKey
from core.localization import SUPPORTED_LANGUAGES as LOCALIZATION_LANGUAGES
from core.localization import default_language

# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


class SettingsSection(StrEnum):
    """One group of settings on the Settings page.

    Exactly the nine ``20-settings.md``'s structure diagram lists, in its order —
    the member order *is* the display order, so a client renders the navigation
    from the API rather than from a list of its own and a tenth section appears
    without a frontend change.

    A section is a **presentation** unit and deliberately not a storage one, which
    is the clearest expression of the spec's *"each feature should own its
    configuration"*:

    * :attr:`PROFILE` reads and writes the ``users`` row **User Management**
      owns;
    * :attr:`SECURITY` delegates to **Authentication** — the password workflow,
      the session generation counter, and the session registry;
    * :attr:`NOTIFICATIONS` and :attr:`COMMUNICATION` are two views of one stored
      thing, ``notification_preferences``, which the **Notification Service**
      owns and serves from its own endpoint;
    * :attr:`AI`, :attr:`DASHBOARD`, :attr:`APPEARANCE`, and :attr:`LANGUAGE` are
      the configuration no feature had a home for, so this module stores them;
    * :attr:`ADMINISTRATION` is platform-wide rather than personal, and is the one
      section with a table, a registry, and a permission of its own.
    """

    PROFILE = "profile"
    SECURITY = "security"
    NOTIFICATIONS = "notifications"
    COMMUNICATION = "communication"
    AI = "ai"
    DASHBOARD = "dashboard"
    APPEARANCE = "appearance"
    LANGUAGE = "language"
    ADMINISTRATION = "administration"


class SettingsStorage(StrEnum):
    """Where a section's values actually live.

    Served with every section descriptor, so a client knows which endpoint to call
    without a table of its own — and so the spec's ownership rule is legible in
    the API rather than only in this docstring. It is also what lets the Settings
    page render a section it stores nothing for.
    """

    #: This module's ``user_settings`` table.
    USER_SETTINGS = "user_settings"
    #: This module's ``platform_settings`` table.
    PLATFORM_SETTINGS = "platform_settings"
    #: The ``users`` row, through User Management.
    PROFILE = "profile"
    #: Authentication: the password, the session generation, the session registry.
    ACCOUNT = "account"
    #: ``notification_preferences``, through ``/notifications/preferences``.
    NOTIFICATION_PREFERENCES = "notification_preferences"


# --------------------------------------------------------------------------- #
# Value vocabularies
# --------------------------------------------------------------------------- #


class SettingValueType(StrEnum):
    """How a setting's value is carried and checked.

    Four, and the fourth is why this is an enum rather than a Python type: a
    :attr:`STRING_LIST` is validated member-by-member against an allowed set, and
    ``list[str]`` cannot say that.
    """

    BOOLEAN = "boolean"
    #: One of a closed set of identifiers — see :attr:`SettingDescriptor.choices`.
    ENUM = "enum"
    #: Free text, bounded by :attr:`SettingDescriptor.max_length`.
    TEXT = "text"
    #: An IANA time-zone identifier, checked against the system's own database.
    TIMEZONE = "timezone"
    #: A list of identifiers, each one of :attr:`SettingDescriptor.choices`.
    STRING_LIST = "string_list"


class ThemePreference(StrEnum):
    """The three ``20-settings.md``'s Appearance section names.

    :attr:`SYSTEM` is a real third value rather than "no choice": it means *follow
    the operating system*, which is a decision somebody made and which behaves
    differently from either of the other two as the day goes on.
    """

    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


#: The interface and AI-interaction languages.
#:
#: Re-exported from :mod:`core.localization` rather than re-spelled, since
#: ``21-localization.md`` shipped: that module is the platform's one language
#: vocabulary, so a fourth language added there becomes selectable here with no
#: edit — and, more importantly, a language this registry offered that the
#: platform could not render would be a setting somebody could save and never see
#: applied. The order is its display order, which is the order this setting's
#: choices are offered in.
SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = LOCALIZATION_LANGUAGES


class DateFormatPreference(StrEnum):
    """How a date is written.

    **Identifiers rather than format strings**, and that is deliberate. A stored
    ``"%d/%m/%Y"`` would be a `strftime` pattern travelling through an API into a
    browser that does not speak `strftime`, and an Arabic reader would get Latin
    digits from it. These name a *style*; the client renders it with
    ``Intl.DateTimeFormat`` in the reader's own locale, which is the only way
    ``ai-workflow-rules.md``'s *"locale-aware formatting for dates, numbers, and
    time"* can be true for both scripts at once.
    """

    #: 31/12/2026
    DAY_MONTH_YEAR = "day_month_year"
    #: 12/31/2026
    MONTH_DAY_YEAR = "month_day_year"
    #: 2026-12-31
    YEAR_MONTH_DAY = "year_month_day"
    #: 31 December 2026
    LONG = "long"


class TimeFormatPreference(StrEnum):
    """Whether the clock is 12- or 24-hour."""

    HOUR_24 = "hour_24"
    HOUR_12 = "hour_12"


class AiResponseLength(StrEnum):
    """How long an AI answer should be.

    ``20-settings.md``: these *"should influence presentation rather than AI
    architecture"*. Honoured literally — nothing in the RAG pipeline, the
    assistant, or the report agent reads this value; it is served to the surfaces
    that *render* an answer, which decide how much of one to show and how much to
    ask for.
    """

    CONCISE = "concise"
    BALANCED = "balanced"
    DETAILED = "detailed"


class CitationDisplay(StrEnum):
    """How an answer's sources are shown.

    :attr:`HIDDEN` hides the *list*, never the citation markers in the prose and
    never the fact that an answer was grounded — ``architecture.md`` invariant 8
    requires every AI response to reference its sources, and a preference must not
    be able to switch an invariant off. It is a display density control, and the
    difference is worth stating because the name alone does not.
    """

    INLINE = "inline"
    LIST = "list"
    HIDDEN = "hidden"


class DashboardRangePreference(StrEnum):
    """The default window the dashboard's analytics open on.

    The three *fixed* members of :class:`~core.dashboard.DashboardRange`.
    ``custom`` is deliberately absent: it is meaningless without the two dates
    that accompany it on a request, and a saved default that cannot be applied is
    a setting that silently does nothing.
    """

    TODAY = "today"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"


# --------------------------------------------------------------------------- #
# Registries
# --------------------------------------------------------------------------- #


class UserSettingKey(StrEnum):
    """Something one person can configure about their own experience.

    An **open** registry in storage — one row per ``(user, key)``, exactly as
    ``notification_preferences`` is — so a new key is a member here plus a
    descriptor below, with **no migration**. Read tolerantly by
    :func:`user_setting_from_value`, so a row written by a later version of the
    platform cannot make an earlier one unable to load somebody's settings.

    Note what is **not** here: nothing about notifications, nothing about the
    password, and nothing about the profile. Those are other features'
    configuration, and this module presents them rather than storing them.
    """

    # --- Appearance --------------------------------------------------------- #
    THEME = "theme"

    # --- Language & Region -------------------------------------------------- #
    LANGUAGE = "language"
    TIMEZONE = "timezone"
    DATE_FORMAT = "date_format"
    TIME_FORMAT = "time_format"

    # --- AI ----------------------------------------------------------------- #
    AI_RESPONSE_LENGTH = "ai_response_length"
    AI_STREAMING = "ai_streaming"
    AI_CITATIONS = "ai_citations"

    # --- Dashboard ---------------------------------------------------------- #
    DASHBOARD_RANGE = "dashboard_range"
    DASHBOARD_WIDGETS = "dashboard_widgets"


class PlatformSettingKey(StrEnum):
    """Something an administrator can configure about the whole platform.

    A **separate** registry from :class:`UserSettingKey`, in a separate table,
    behind a separate permission. ``20-settings.md``: *"Administrator settings
    should remain isolated from regular user settings."* No key appears in both
    registries and no query reads both, so the isolation is structural rather than
    a rule somebody has to remember.

    Every ``default_*`` member here is the fallback for the corresponding
    :class:`UserSettingKey` — which is what makes these settings *do something*
    rather than merely be stored. An account that has expressed no opinion follows
    the platform's answer, and an administrator changing it reaches every such
    account at once with no backfill, because there is nothing stored to back-fill.
    """

    # --- System configuration ------------------------------------------------ #
    MAINTENANCE_MODE = "maintenance_mode"
    MAINTENANCE_MESSAGE = "maintenance_message"

    # --- Default appearance, language, and region ---------------------------- #
    DEFAULT_THEME = "default_theme"
    DEFAULT_LANGUAGE = "default_language"
    DEFAULT_TIMEZONE = "default_timezone"
    DEFAULT_DATE_FORMAT = "default_date_format"
    DEFAULT_TIME_FORMAT = "default_time_format"

    # --- Default AI configuration -------------------------------------------- #
    AI_DEFAULT_RESPONSE_LENGTH = "ai_default_response_length"
    AI_DEFAULT_STREAMING = "ai_default_streaming"
    AI_DEFAULT_CITATIONS = "ai_default_citations"


@dataclass(frozen=True, slots=True)
class SettingDescriptor:
    """Everything the platform knows about one setting.

    The single place a setting's section, shape, permitted values, and default are
    stated — so validation, the API schema, and the client's rendering all read
    the same declaration and cannot disagree about what a valid value is.
    """

    #: Which part of the Settings page this belongs to.
    section: SettingsSection
    #: How the value is carried and checked.
    value_type: SettingValueType
    #: The value for somebody who has never chosen. For a platform setting, the
    #: value this deployment starts with.
    default: Any
    #: Permitted identifiers, for :attr:`SettingValueType.ENUM` and
    #: :attr:`SettingValueType.STRING_LIST`. Empty otherwise.
    choices: tuple[str, ...] = ()
    #: Longest accepted text, for :attr:`SettingValueType.TEXT`.
    max_length: int = 0
    #: Most entries accepted, for :attr:`SettingValueType.STRING_LIST`.
    max_items: int = 0
    #: The user setting this one is the fallback for, when it is a platform
    #: setting. ``None`` for a setting that is nobody's default.
    defaults_for: UserSettingKey | None = None


#: Every widget a dashboard preference may name, as identifiers.
#:
#: Derived from :class:`~core.dashboard.WidgetKey` rather than restated, so a
#: widget added to the dashboard becomes selectable here with no edit — and a
#: widget *removed* there stops validating here, which is the direction that
#: matters: a preference naming a widget that no longer exists would be a saved
#: setting nothing can apply.
_WIDGET_CHOICES: Final[tuple[str, ...]] = tuple(widget.value for widget in WidgetKey)


#: What every user setting means, is allowed to be, and defaults to.
#:
#: Read-only at runtime (``MappingProxyType``) for the reason
#: :data:`~core.roles.ROLE_PERMISSIONS` is: a bug elsewhere must not be able to
#: widen what a setting accepts by mutating the policy in place.
USER_SETTINGS: Mapping[UserSettingKey, SettingDescriptor] = MappingProxyType(
    {
        UserSettingKey.THEME: SettingDescriptor(
            section=SettingsSection.APPEARANCE,
            value_type=SettingValueType.ENUM,
            default=ThemePreference.DARK.value,
            choices=tuple(theme.value for theme in ThemePreference),
        ),
        UserSettingKey.LANGUAGE: SettingDescriptor(
            section=SettingsSection.LANGUAGE,
            value_type=SettingValueType.ENUM,
            # The *application* default rather than a literal, so a deployment
            # that moves `DEFAULT_LANGUAGE` moves this with it — an account with
            # no stored row and an account that fell back through
            # `core.localization.resolve_language` must not end up in different
            # languages, which is exactly the drift a second literal would cause.
            default=default_language(),
            choices=SUPPORTED_LANGUAGES,
        ),
        UserSettingKey.TIMEZONE: SettingDescriptor(
            section=SettingsSection.LANGUAGE,
            value_type=SettingValueType.TIMEZONE,
            default="UTC",
        ),
        UserSettingKey.DATE_FORMAT: SettingDescriptor(
            section=SettingsSection.LANGUAGE,
            value_type=SettingValueType.ENUM,
            default=DateFormatPreference.DAY_MONTH_YEAR.value,
            choices=tuple(fmt.value for fmt in DateFormatPreference),
        ),
        UserSettingKey.TIME_FORMAT: SettingDescriptor(
            section=SettingsSection.LANGUAGE,
            value_type=SettingValueType.ENUM,
            default=TimeFormatPreference.HOUR_24.value,
            choices=tuple(fmt.value for fmt in TimeFormatPreference),
        ),
        UserSettingKey.AI_RESPONSE_LENGTH: SettingDescriptor(
            section=SettingsSection.AI,
            value_type=SettingValueType.ENUM,
            default=AiResponseLength.BALANCED.value,
            choices=tuple(length.value for length in AiResponseLength),
        ),
        UserSettingKey.AI_STREAMING: SettingDescriptor(
            section=SettingsSection.AI,
            value_type=SettingValueType.BOOLEAN,
            default=True,
        ),
        UserSettingKey.AI_CITATIONS: SettingDescriptor(
            section=SettingsSection.AI,
            value_type=SettingValueType.ENUM,
            default=CitationDisplay.LIST.value,
            choices=tuple(display.value for display in CitationDisplay),
        ),
        UserSettingKey.DASHBOARD_RANGE: SettingDescriptor(
            section=SettingsSection.DASHBOARD,
            value_type=SettingValueType.ENUM,
            default=DashboardRangePreference.LAST_30_DAYS.value,
            choices=tuple(window.value for window in DashboardRangePreference),
        ),
        UserSettingKey.DASHBOARD_WIDGETS: SettingDescriptor(
            section=SettingsSection.DASHBOARD,
            value_type=SettingValueType.STRING_LIST,
            # **Empty means "every widget my role and permissions allow"**, not
            # "none". A default listing all nineteen would freeze the caller's
            # dashboard at today's catalog: a widget added next month would be
            # absent from a preference nobody edited, which is exactly the
            # redesign-on-extension `19-dashboard-analytics.md` avoids. Hiding
            # everything is expressible — it is a list of one widget, or none of
            # the ones offered — and it is not what an untouched account means.
            default=[],
            choices=_WIDGET_CHOICES,
            max_items=len(_WIDGET_CHOICES),
        ),
    }
)


#: Longest maintenance message an administrator may publish.
#:
#: The same order of magnitude as an announcement, and for the same reason: it is
#: a banner rather than a document, and something nobody reads to the end has not
#: warned anybody.
MAINTENANCE_MESSAGE_MAX_LENGTH: Final[int] = 500


#: What every platform setting means, is allowed to be, and defaults to.
PLATFORM_SETTINGS: Mapping[PlatformSettingKey, SettingDescriptor] = MappingProxyType(
    {
        PlatformSettingKey.MAINTENANCE_MODE: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.BOOLEAN,
            default=False,
        ),
        PlatformSettingKey.MAINTENANCE_MESSAGE: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.TEXT,
            default="",
            max_length=MAINTENANCE_MESSAGE_MAX_LENGTH,
        ),
        PlatformSettingKey.DEFAULT_THEME: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.ENUM,
            default=ThemePreference.DARK.value,
            choices=tuple(theme.value for theme in ThemePreference),
            defaults_for=UserSettingKey.THEME,
        ),
        PlatformSettingKey.DEFAULT_LANGUAGE: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.ENUM,
            default=default_language(),
            choices=SUPPORTED_LANGUAGES,
            defaults_for=UserSettingKey.LANGUAGE,
        ),
        PlatformSettingKey.DEFAULT_TIMEZONE: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.TIMEZONE,
            default="UTC",
            defaults_for=UserSettingKey.TIMEZONE,
        ),
        PlatformSettingKey.DEFAULT_DATE_FORMAT: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.ENUM,
            default=DateFormatPreference.DAY_MONTH_YEAR.value,
            choices=tuple(fmt.value for fmt in DateFormatPreference),
            defaults_for=UserSettingKey.DATE_FORMAT,
        ),
        PlatformSettingKey.DEFAULT_TIME_FORMAT: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.ENUM,
            default=TimeFormatPreference.HOUR_24.value,
            choices=tuple(fmt.value for fmt in TimeFormatPreference),
            defaults_for=UserSettingKey.TIME_FORMAT,
        ),
        PlatformSettingKey.AI_DEFAULT_RESPONSE_LENGTH: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.ENUM,
            default=AiResponseLength.BALANCED.value,
            choices=tuple(length.value for length in AiResponseLength),
            defaults_for=UserSettingKey.AI_RESPONSE_LENGTH,
        ),
        PlatformSettingKey.AI_DEFAULT_STREAMING: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.BOOLEAN,
            default=True,
            defaults_for=UserSettingKey.AI_STREAMING,
        ),
        PlatformSettingKey.AI_DEFAULT_CITATIONS: SettingDescriptor(
            section=SettingsSection.ADMINISTRATION,
            value_type=SettingValueType.ENUM,
            default=CitationDisplay.LIST.value,
            choices=tuple(display.value for display in CitationDisplay),
            defaults_for=UserSettingKey.AI_CITATIONS,
        ),
    }
)


#: Platform key → the user key it is the fallback for.
#:
#: Derived from the descriptors rather than written out a second time, so a new
#: default cannot be declared in one place and forgotten in the other.
PLATFORM_DEFAULT_FOR: Mapping[UserSettingKey, PlatformSettingKey] = MappingProxyType(
    {
        descriptor.defaults_for: key
        for key, descriptor in PLATFORM_SETTINGS.items()
        if descriptor.defaults_for is not None
    }
)


# --------------------------------------------------------------------------- #
# Section catalog
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SectionDescriptor:
    """One section of the Settings page, as the API describes it to a client.

    **Server-described, in the shape ``19-dashboard-analytics.md`` established for
    widgets**, and for the same reason: the spec requires that *"the
    implementation should support future sections without redesign"*, and a
    section list written in the browser would have to be edited every time one was
    added. A client renders the navigation from this, so a tenth section reaches a
    browser nobody redeployed.

    It carries **no prose**: :attr:`section` is a stable key, and the heading and
    description live in the client's own catalogue — the same rule the dashboard's
    widget descriptors follow.
    """

    section: SettingsSection
    #: Where this section's values live, and therefore which endpoint writes them.
    storage: SettingsStorage
    #: Whether this caller may change it. A section they may only read is still
    #: served, because reading which theme is in force is useful even where
    #: choosing one is not.
    editable: bool
    #: Whether this section is administrative. Exactly the callers holding
    #: ``settings:manage`` ever see one that is.
    administrative: bool = False


#: Every section, with where it stores what it shows.
#:
#: One entry per :class:`SettingsSection` member, and a test asserts that — a
#: section with no descriptor would silently vanish from every client's
#: navigation, which is the failure mode a registry keyed by an enum exists to
#: make impossible.
SECTIONS: Mapping[SettingsSection, SettingsStorage] = MappingProxyType(
    {
        SettingsSection.PROFILE: SettingsStorage.PROFILE,
        SettingsSection.SECURITY: SettingsStorage.ACCOUNT,
        SettingsSection.NOTIFICATIONS: SettingsStorage.NOTIFICATION_PREFERENCES,
        SettingsSection.COMMUNICATION: SettingsStorage.NOTIFICATION_PREFERENCES,
        SettingsSection.AI: SettingsStorage.USER_SETTINGS,
        SettingsSection.DASHBOARD: SettingsStorage.USER_SETTINGS,
        SettingsSection.APPEARANCE: SettingsStorage.USER_SETTINGS,
        SettingsSection.LANGUAGE: SettingsStorage.USER_SETTINGS,
        SettingsSection.ADMINISTRATION: SettingsStorage.PLATFORM_SETTINGS,
    }
)


# --------------------------------------------------------------------------- #
# Tolerant lookups
# --------------------------------------------------------------------------- #


def user_setting_from_value(value: str) -> UserSettingKey | None:
    """Resolve a stored user-setting key, tolerating one this version has dropped.

    ``None`` rather than an exception, for the reason
    :func:`~core.notifications.preference_from_value` returns one: the column is
    an open vocabulary, and a row written by a later version of the platform must
    not make an earlier one unable to load somebody's settings.
    """
    try:
        return UserSettingKey(value)
    except ValueError:
        return None


def platform_setting_from_value(value: str) -> PlatformSettingKey | None:
    """Resolve a stored platform-setting key, tolerating an unknown one."""
    try:
        return PlatformSettingKey(value)
    except ValueError:
        return None


def default_platform_value(key: PlatformSettingKey) -> Any:
    """The value a deployment that has configured nothing behaves as."""
    return PLATFORM_SETTINGS[key].default


def default_user_value(
    key: UserSettingKey, *, platform: Mapping[PlatformSettingKey, Any] | None = None
) -> Any:
    """The value for somebody who has never chosen.

    **The platform's answer when there is one**, falling back to the descriptor's.
    This is the single function that decides what "no stored row" means, which is
    what lets an administrator change a default for every untouched account at
    once — there is nothing stored to migrate, exactly as
    :func:`~core.notifications.default_preference` reserved for a channel whose
    defaults differ.

    A platform value that has somehow become invalid — a vocabulary narrowed by a
    later release, say — is **ignored rather than raised on**: a settings page that
    will not load is a much worse failure than one showing the built-in default,
    which is the same trade :func:`~core.notifications.render_notification` makes
    for a withdrawn rule.
    """
    descriptor = USER_SETTINGS[key]
    platform_key = PLATFORM_DEFAULT_FOR.get(key)

    if platform is not None and platform_key is not None:
        candidate = platform.get(platform_key)
        if candidate is not None:
            try:
                return validate_setting(descriptor, candidate)
            except InvalidSettingError:
                return descriptor.default

    return descriptor.default


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class InvalidSettingError(ValueError):
    """A supplied value is not acceptable for its setting.

    A plain :class:`ValueError` subclass rather than an
    :class:`~core.exceptions.AppException`, for the reason
    :class:`~core.dashboard.InvalidDashboardWindowError` is one: this module is
    pure data with no HTTP in it, and the service layer above turns this into the
    platform's standard 422. Carrying the **key** and a **reason** rather than an
    interpolated sentence keeps it renderable in the reader's language.
    """

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


def validate_setting(descriptor: SettingDescriptor, value: Any) -> Any:
    """Return ``value`` normalized for ``descriptor``, or raise.

    Every write passes through here **before anything is persisted**, and the
    service applies it to the *whole* batch before writing any of it — which is
    the spec's *"invalid configuration should never corrupt stored preferences"*.
    A partially-applied settings save is the failure that rule exists to prevent,
    and it is prevented by ordering rather than by a transaction alone: a rejected
    entry means no statement was issued for any of them.

    Normalization is deliberately narrow — trimming text, de-duplicating a list
    while keeping its order — and never coercive. A string ``"true"`` is **not**
    accepted for a boolean: JSON has a boolean, a client that sent a string got
    the type wrong, and quietly repairing it would hide the bug until the day the
    string was ``"false"``.

    Raises:
        InvalidSettingError: the value is of the wrong type, outside the permitted
            set, too long, or not a time zone this system knows.
    """
    match descriptor.value_type:
        case SettingValueType.BOOLEAN:
            if not isinstance(value, bool):
                raise InvalidSettingError("value", "expected true or false")
            return value

        case SettingValueType.ENUM:
            if not isinstance(value, str):
                raise InvalidSettingError("value", "expected one of the permitted values")
            if value not in descriptor.choices:
                raise InvalidSettingError(
                    "value", f"expected one of: {', '.join(descriptor.choices)}"
                )
            return value

        case SettingValueType.TEXT:
            if not isinstance(value, str):
                raise InvalidSettingError("value", "expected text")
            trimmed = value.strip()
            if len(trimmed) > descriptor.max_length:
                raise InvalidSettingError(
                    "value", f"must not exceed {descriptor.max_length} characters"
                )
            return trimmed

        case SettingValueType.TIMEZONE:
            if not isinstance(value, str):
                raise InvalidSettingError("value", "expected a time zone identifier")
            trimmed = value.strip()
            if not trimmed:
                raise InvalidSettingError("value", "expected a time zone identifier")
            try:
                # Checked against the system's own tz database rather than against
                # a list in this file: a hard-coded set of zones goes stale every
                # time a country changes its rules, and the standard library
                # already ships the authority.
                ZoneInfo(trimmed)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise InvalidSettingError("value", "unknown time zone") from exc
            return trimmed

        case SettingValueType.STRING_LIST:
            if not isinstance(value, list):
                raise InvalidSettingError("value", "expected a list of identifiers")
            if len(value) > descriptor.max_items:
                raise InvalidSettingError(
                    "value", f"must not contain more than {descriptor.max_items} entries"
                )
            return _validated_identifiers(value, descriptor.choices)

    # Unreachable while every member of `SettingValueType` is handled above; a new
    # member fails closed here rather than being persisted unchecked.
    raise InvalidSettingError("value", "unsupported setting type")


def _validated_identifiers(
    values: Sequence[Any], choices: tuple[str, ...]
) -> list[str]:
    """Check every entry of a list setting, de-duplicating while keeping order.

    Order is kept because a list of widgets is a list somebody arranged;
    duplicates are dropped because two of the same widget is not a thing a
    dashboard can render, and refusing the whole save over a repeated entry would
    be pedantry rather than validation.
    """
    seen: list[str] = []
    for entry in values:
        if not isinstance(entry, str):
            raise InvalidSettingError("value", "expected a list of identifiers")
        if entry not in choices:
            raise InvalidSettingError("value", f"unknown value {entry!r}")
        if entry not in seen:
            seen.append(entry)
    return seen


__all__ = [
    "MAINTENANCE_MESSAGE_MAX_LENGTH",
    "PLATFORM_DEFAULT_FOR",
    "PLATFORM_SETTINGS",
    "SECTIONS",
    "SUPPORTED_LANGUAGES",
    "USER_SETTINGS",
    "AiResponseLength",
    "CitationDisplay",
    "DashboardRangePreference",
    "DateFormatPreference",
    "InvalidSettingError",
    "PlatformSettingKey",
    "SectionDescriptor",
    "SettingDescriptor",
    "SettingValueType",
    "SettingsSection",
    "SettingsStorage",
    "ThemePreference",
    "TimeFormatPreference",
    "UserSettingKey",
    "default_platform_value",
    "default_user_value",
    "platform_setting_from_value",
    "user_setting_from_value",
    "validate_setting",
]
