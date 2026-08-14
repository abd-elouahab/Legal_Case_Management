"""Settings business logic.

``20-settings.md``'s governing rule is that *"each feature should own its
configuration"* and that the Settings module *"simply presents and manages those
configurations through a unified interface"*. This service is that interface, and
almost everything interesting about it is a consequence of taking the rule
literally:

* **Profile** is a write to the ``users`` row — through
  :class:`~repositories.user.UserRepository`, the same repository User Management
  uses, and deliberately **not** through
  :meth:`~services.user.UserService.update_user`. That method is an
  *administrator* editing *somebody else's* account: it takes a user id, checks
  ``users:update``, can change a role, a status, and an email, and publishes a
  ``user.updated`` event about a third party. None of that is a person editing
  their own name, and reusing it would have meant either widening its
  authorization or handing this service a capability it must never have. What is
  reused is the layer below, where reuse is safe.
* **Account & Security** is delegated to :class:`~services.auth.AuthService`
  whole. This service verifies no password, mints no token, and touches
  ``session_generation`` never. It calls one method and records that it happened.
* **Notification and communication preferences appear in no method here at all.**
  They are the Notification Service's, served from its own endpoint. The only
  thing this module contributes is a *section descriptor* saying where they live
  — which is the ownership rule expressed as an absence, the strongest form it
  can take.
* **Appearance, Language & Region, AI, and Dashboard** are what no feature owned,
  so this service stores them. That is the whole of what it owns.

**Validation happens to the entire batch before anything is written.**
``20-settings.md``: *"Invalid configuration should never corrupt stored
preferences."* A save with one bad field therefore leaves every other field
exactly as it was, rather than half-applied — enforced by ordering rather than by
a transaction alone, because a rejected entry means no statement was issued for
any of them. It is the same all-or-nothing rule
:mod:`services.case` applies to a request touching a field the caller cannot
reach.

**There is no per-resource access module, and there is deliberately no
``settings_access.py``.** *"Users may modify only their own settings"* is not
enforced by a policy here; it is enforced by the **absence of a parameter**. No
method on this service takes a user identifier, and no endpoint in front of it
does either — the account is always ``actor``, resolved from the access token. A
caller cannot ask for somebody else's settings because there is nowhere to put
the request. That is the same shape :mod:`repositories.conversation` and
:mod:`repositories.notification` use, one step further: they scope a query, this
has no query to scope.

**Administrator settings are isolated by three things at once**: a separate
table, a separate registry, and :attr:`~core.permissions.Permission.SETTINGS_MANAGE`
— which no role but administrator holds and which is *not* a wider form of
``settings:update``. A caller holding every user-settings permission still cannot
reach :meth:`update_platform_settings`, because the two act on different stores
through different methods behind different guards.

**Nothing here logs a value.** The spec's Logging section asks for *what changed*
— profile updated, password changed, preferences changed — and forbids passwords,
secrets, and tokens. This goes further and logs no setting *value* either: which
theme somebody chose is not a secret, but a log line that carries their time zone,
their language, and their working hours is a small profile of a person assembled
by accident. The logs carry identifiers, section names, setting **keys**, and
counts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.exc import SQLAlchemyError

from core import security
from core.exceptions import (
    InvalidPasswordError,
    InvalidSettingValueError,
    UnknownSettingError,
)
from core.settings import (
    PLATFORM_SETTINGS,
    SECTIONS,
    USER_SETTINGS,
    InvalidSettingError,
    PlatformSettingKey,
    SectionDescriptor,
    SettingsSection,
    SettingsStorage,
    UserSettingKey,
    default_platform_value,
    default_user_value,
    platform_setting_from_value,
    validate_setting,
)
from models.user import User
from repositories.settings import SettingsRepository, SettingsStatistics
from repositories.user import UserRepository
from schemas.errors import ErrorDetail
from services.auth import AuthService, TokenPair
from services.session_registry import SessionRecord
from services.settings_metrics import (
    NullSettingsMetrics,
    SettingsFailureReason,
    SettingsMetricsRecorder,
    SettingsMetricsSnapshot,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """One setting's effective value, and whether anybody chose it.

    ``is_default`` is reported rather than inferred by the client, for the reason
    :class:`~core.notifications.ChannelPreference` reports it: an account that has
    expressed no opinion has **no stored row**, and showing a value without saying
    it is the platform's choice would imply somebody made it.
    """

    value: Any
    is_default: bool


@dataclass(frozen=True, slots=True)
class MaintenanceStatus:
    """What the platform is telling everybody about its own availability."""

    maintenance_mode: bool
    message: str | None


@dataclass(frozen=True, slots=True)
class SessionView:
    """One live sign-in, with the one thing the registry cannot know.

    :attr:`is_current` is resolved here rather than stored, because "which of
    these is you" is a property of *the request being served* rather than of the
    session. The registry records sign-ins; only the token in front of it knows
    which one is asking.
    """

    record: SessionRecord
    is_current: bool


@dataclass(frozen=True, slots=True)
class SessionListing:
    """The caller's sessions, and whether the list could be built at all.

    ``available`` exists so a client can tell *"you have one session"* from *"the
    registry is unreachable"* — two states an empty list would otherwise conflate,
    and the second of which deserves a different sentence on screen. See
    :mod:`services.session_registry` for why the registry fails soft.
    """

    sessions: list[SessionView]
    available: bool


@dataclass(frozen=True, slots=True)
class SettingsMetrics:
    """Everything the monitoring endpoint reports, from both of its sources."""

    statistics: SettingsStatistics
    counters: SettingsMetricsSnapshot


class SettingsService:
    """Presents and manages the platform's configuration."""

    def __init__(
        self,
        settings_repository: SettingsRepository,
        users: UserRepository,
        auth: AuthService,
        *,
        metrics: SettingsMetricsRecorder | None = None,
    ) -> None:
        self._settings = settings_repository
        self._users = users
        self._auth = auth
        self._metrics: SettingsMetricsRecorder = metrics or NullSettingsMetrics()

    # ------------------------------------------------------------- sections #

    def sections(self, *, actor: User, can_manage: bool) -> list[SectionDescriptor]:
        """Which sections this caller sees, in the platform's own order.

        **Served rather than hard-coded in the client**, the shape
        ``19-dashboard-analytics.md`` established for widgets: a tenth section
        appears in a browser nobody redeployed, which is what the spec's *"support
        future sections without redesign"* has to mean on the client as well as on
        the server.

        An administrative section is **omitted entirely** for a caller who may not
        manage it, rather than served as read-only. Serving it disabled would tell
        every lawyer which platform settings exist and that somebody else controls
        them; omitting it says nothing. That is the same reasoning
        :class:`~core.exceptions.DashboardWidgetNotFoundError` records for a
        widget the caller lacks the capability for.
        """
        del actor  # Sections do not vary by identity, only by capability.
        descriptors: list[SectionDescriptor] = []

        for section in SettingsSection:
            storage = SECTIONS[section]
            administrative = storage is SettingsStorage.PLATFORM_SETTINGS

            if administrative and not can_manage:
                continue

            descriptors.append(
                SectionDescriptor(
                    section=section,
                    storage=storage,
                    # Every section a caller can see is one they can act in: the
                    # permissions that gate reading and writing settings are both
                    # in `BASE_PERMISSIONS`, and an administrative section is not
                    # served at all unless it can be managed. The field is kept
                    # because a future read-only section — a compliance policy, a
                    # licence — is a plausible thing and this is where it would
                    # say so.
                    editable=True,
                    administrative=administrative,
                )
            )

        return descriptors

    # -------------------------------------------------------------- profile #

    def update_profile(self, changes: Mapping[str, Any], *, actor: User) -> User:
        """Apply the caller's own profile changes.

        Writes through :class:`~repositories.user.UserRepository` rather than
        through :class:`~services.user.UserService` — see the module docstring for
        why the administrative path is the wrong one to reuse here.

        **Fields that already hold the requested value are not written**, which is
        the spec's *"minimize unnecessary updates"* and *"avoid duplicate
        persistence"*: a form that posts every field on every save produces no
        write at all when nothing changed, and ``updated_at`` keeps meaning *when
        this profile last changed*.

        Only the four fields ``ProfileUpdate`` defines can arrive here, and none of
        them is ``email``, ``role``, or ``status``. That is enforced by the schema
        rather than by a filter in this method, so a field added to the schema
        without being considered here cannot silently become self-editable — the
        schema is the smaller, more reviewable surface.
        """
        applied: list[str] = []
        for field, value in changes.items():
            if getattr(actor, field) != value:
                setattr(actor, field, value)
                applied.append(field)

        if applied:
            try:
                self._users.save(actor)
            except SQLAlchemyError:
                self._metrics.record_failure(SettingsFailureReason.PERSISTENCE_FAILED)
                raise

        self._metrics.record_profile_change(fields=len(applied))
        logger.info(
            "profile_updated",
            user_id=str(actor.id),
            # The **field names**, never their values. A log line carrying
            # somebody's phone number is a log line carrying somebody's phone
            # number, whatever the reason it was written.
            fields=sorted(applied),
            changed=len(applied),
        )
        return actor

    # ------------------------------------------------------- user settings #

    def user_settings(self, *, actor: User) -> dict[UserSettingKey, ResolvedSetting]:
        """The caller's answer to every setting the platform offers.

        The **complete** set rather than only their stored rows, so a settings page
        renders from one response and a setting added later appears automatically
        at its default — exactly as ``GET /notifications/preferences`` behaves, and
        for the same reason: no client should have to know that "no row" means
        "the default".

        A stored key this version does not define is **ignored** rather than
        raised on (see :func:`~core.settings.user_setting_from_value`), so an
        instance running behind a newer one can still show somebody their
        settings.
        """
        stored = self._settings.user_settings_for(actor.id)
        platform = self._platform_values()

        resolved: dict[UserSettingKey, ResolvedSetting] = {}
        for key in UserSettingKey:
            if key.value in stored:
                resolved[key] = ResolvedSetting(value=stored[key.value], is_default=False)
            else:
                resolved[key] = ResolvedSetting(
                    value=default_user_value(key, platform=platform), is_default=True
                )
        return resolved

    def update_user_settings(
        self, changes: Sequence[tuple[UserSettingKey, Any]], *, actor: User
    ) -> dict[UserSettingKey, ResolvedSetting]:
        """Store the caller's choices and return the complete set.

        **Everything is validated before anything is written.** The loop below
        completes, or raises, before a single statement reaches the database —
        which is the spec's *"invalid configuration should never corrupt stored
        preferences"*. A form with one bad time zone leaves the other five
        settings exactly as they were.

        Only the keys supplied are written; anything omitted keeps its current
        value, or keeps having no row and therefore keeps following the platform
        default. See
        :meth:`~repositories.settings.SettingsRepository.set_user_settings`.
        """
        validated = self._validate(changes, USER_SETTINGS)

        try:
            self._settings.set_user_settings(
                actor.id, {key.value: value for key, value in validated.items()}
            )
        except SQLAlchemyError:
            self._metrics.record_failure(SettingsFailureReason.PERSISTENCE_FAILED)
            logger.warning("settings_update_failed", user_id=str(actor.id), reason="persistence")
            raise

        for section, count in _count_by_section(validated, USER_SETTINGS).items():
            self._metrics.record_update(section, count=count)

        logger.info(
            "user_settings_updated",
            user_id=str(actor.id),
            # Keys and sections, never values — see the module docstring.
            keys=sorted(key.value for key in validated),
            sections=sorted({USER_SETTINGS[key].section.value for key in validated}),
        )
        return self.user_settings(actor=actor)

    # --------------------------------------------------- platform settings #

    def platform_settings(self) -> dict[PlatformSettingKey, ResolvedSetting]:
        """The deployment's configuration, complete, with what is stored marked.

        Callers reach this only through :attr:`~core.permissions.Permission.SETTINGS_MANAGE`,
        which is checked by the route. There is no scope to apply here and no
        per-resource question to ask: there is one platform.
        """
        stored = self._settings.platform_settings()

        resolved: dict[PlatformSettingKey, ResolvedSetting] = {}
        for key in PlatformSettingKey:
            if key.value in stored:
                resolved[key] = ResolvedSetting(value=stored[key.value], is_default=False)
            else:
                resolved[key] = ResolvedSetting(
                    value=default_platform_value(key), is_default=True
                )
        return resolved

    def update_platform_settings(
        self, changes: Sequence[tuple[PlatformSettingKey, Any]], *, actor: User
    ) -> dict[PlatformSettingKey, ResolvedSetting]:
        """Store the deployment's configuration.

        Validated as a whole batch before anything is written, exactly as
        :meth:`update_user_settings` is — and it matters more here, because a
        half-applied change to platform defaults reaches every account that has
        expressed no opinion.

        ``updated_by`` records who made the change, on the row itself: *"who
        turned maintenance mode on?"* is a question asked days later, from a
        database, by somebody who does not have the application's logs.
        """
        validated = self._validate(changes, PLATFORM_SETTINGS)

        try:
            self._settings.set_platform_settings(
                {key.value: value for key, value in validated.items()},
                updated_by=actor.id,
            )
        except SQLAlchemyError:
            self._metrics.record_failure(SettingsFailureReason.PERSISTENCE_FAILED)
            logger.warning(
                "platform_settings_update_failed", actor_id=str(actor.id), reason="persistence"
            )
            raise

        self._metrics.record_update(SettingsSection.ADMINISTRATION, count=len(validated))
        logger.info(
            "platform_settings_updated",
            actor_id=str(actor.id),
            actor_role=actor.role.value,
            keys=sorted(key.value for key in validated),
            changed=len(validated),
        )
        return self.platform_settings()

    def maintenance_status(self) -> MaintenanceStatus:
        """What the platform is telling everybody about its own availability.

        **The one piece of administrator configuration every authenticated caller
        may read**, and the asymmetry is deliberate: the *switch* is
        administrative, the *announcement* is not. A maintenance notice only
        administrators can see is a notice nobody needed — the same shape a system
        announcement has, where one person decides and everybody is told.

        Note what this does and does not do. It **announces**; it does not refuse
        requests. Blocking traffic would be a platform-wide behaviour change owned
        by no feature in this spec, and a switch that silently turned the API off
        is not something a Settings page should be able to do without the
        deployment having chosen it deliberately. See ``progress-tracker.md`` for
        the open question that records this.
        """
        stored = self._platform_values()
        enabled = bool(stored.get(PlatformSettingKey.MAINTENANCE_MODE, False))
        message = stored.get(PlatformSettingKey.MAINTENANCE_MESSAGE) or None
        return MaintenanceStatus(
            maintenance_mode=enabled,
            # A message with the mode off is a draft, not an announcement. Serving
            # it would put a stale notice on everybody's screen the moment
            # somebody typed one and had not yet switched it on.
            message=message if enabled else None,
        )

    # --------------------------------------------------- account & security #

    def change_password(
        self,
        current_password: str,
        new_password: str,
        *,
        actor: User,
        current_access: security.TokenPayload,
        current_refresh_token: str | None,
    ) -> TokenPair:
        """Change the caller's password, through the authentication system.

        One line of delegation and one of bookkeeping, which is the whole point.
        :meth:`~services.auth.AuthService.change_password` already does everything
        ``20-settings.md``'s Password Change Policy asks for and has since
        Authentication shipped: it requires the current password, clears
        ``must_change_password``, and invalidates every other session by bumping
        ``users.session_generation`` while handing this device a replacement pair.
        Re-implementing any of that here would be a second password workflow to
        keep correct.

        ``POST /auth/change-password`` remains the **authentication** surface —
        it is what the forced-change flow calls before the application shell has
        loaded — and this is the **settings** surface. Two doors, one
        implementation, and no duplicated rule: the difference between them is
        which page the user is on, not what happens.
        """
        try:
            tokens = self._auth.change_password(
                actor,
                current_password,
                new_password,
                current_access=current_access,
                current_refresh_token=current_refresh_token,
            )
        except InvalidPasswordError:
            # Counted as the **security event** it is rather than as a form error:
            # an operator watching this rise across many accounts is watching
            # something very different from somebody mistyping a time zone. Never
            # swallowed — the caller must still see the refusal.
            self._metrics.record_failure(SettingsFailureReason.BAD_CURRENT_PASSWORD)
            raise
        except Exception:
            self._metrics.record_failure(SettingsFailureReason.UNKNOWN)
            raise

        self._metrics.record_password_change()
        # No password, no hash, no token — `20-settings.md`'s "never log
        # passwords, secrets, access tokens", and `AuthService` has already logged
        # the change itself with the generation counters.
        logger.info("settings_password_changed", user_id=str(actor.id))
        return tokens

    def active_sessions(
        self, *, actor: User, current_session_id: str | None
    ) -> SessionListing:
        """Every sign-in that can still be resumed for the caller's account.

        ``current_session_id`` comes from the ``sid`` claim on the access token
        that made this request, so *"this device"* is identified by the credential
        presenting itself rather than by anything the client asserts.

        A registry that is unreachable, or a deployment running without one,
        reports ``available=False`` and an empty list rather than failing. That is
        a stated limitation of a **view**: what makes a session usable is the
        signature, the denylist, and ``users.session_generation``, none of which
        is in this list — so the revocation control beside it keeps working
        exactly as well.
        """
        records = self._auth.active_sessions(actor)
        return SessionListing(
            sessions=[
                SessionView(record=record, is_current=record.session_id == current_session_id)
                for record in records
            ],
            # An account genuinely has at least one live session — the one asking
            # — so an empty list means the registry could not be read or is not
            # recording. Inferring it this way rather than plumbing a flag out of
            # Redis keeps the failure mode in one place.
            available=bool(records),
        )

    def revoke_other_sessions(
        self,
        *,
        actor: User,
        current_access: security.TokenPayload,
        current_refresh_token: str | None,
    ) -> TokenPair:
        """End every session for the caller's account except this one.

        Delegated whole to :meth:`~services.auth.AuthService.revoke_other_sessions`,
        which uses the **same** durable mechanism a password change does — one
        write to ``users.session_generation`` — so a device the session registry
        never heard of is signed out exactly like one it did. This method adds a
        counter and a log line.
        """
        tokens = self._auth.revoke_other_sessions(
            actor,
            current_access=current_access,
            current_refresh_token=current_refresh_token,
        )
        self._metrics.record_session_revocation()
        logger.info("settings_sessions_revoked", user_id=str(actor.id))
        return tokens

    # -------------------------------------------------------------- metrics #

    def metrics(self) -> SettingsMetrics:
        """Platform-wide settings health, from both of its sources.

        Row counts are SQL aggregates and rates are process counters — see
        :mod:`services.settings_metrics` for why the split exists and why the
        response says which is which.
        """
        return SettingsMetrics(
            statistics=self._settings.statistics(),
            counters=self._metrics.snapshot(),
        )

    # -------------------------------------------------------------- helpers #

    def _platform_values(self) -> dict[PlatformSettingKey, Any]:
        """Stored platform settings, keyed by the enum and tolerant of unknowns.

        Used to resolve *defaults*, so a key this version does not define is
        dropped rather than raised on: an administrator's configuration must never
        be able to make somebody's settings page fail to load.
        """
        resolved: dict[PlatformSettingKey, Any] = {}
        for raw_key, value in self._settings.platform_settings().items():
            key = platform_setting_from_value(raw_key)
            if key is not None:
                resolved[key] = value
        return resolved

    def _validate(
        self,
        changes: Sequence[tuple[Any, Any]],
        registry: Mapping[Any, Any],
    ) -> dict[Any, Any]:
        """Check every supplied value against its descriptor, or raise.

        **The whole batch, before the caller writes any of it.** Both failures
        collect *every* offending entry rather than stopping at the first, because
        a settings form should be able to mark all of its bad fields at once
        rather than one save at a time.

        Raises:
            UnknownSettingError: a key this version does not define.
            InvalidSettingValueError: a value of the wrong type, outside the
                permitted set, too long, or not a time zone this system knows.
        """
        unknown: list[ErrorDetail] = []
        invalid: list[ErrorDetail] = []
        validated: dict[Any, Any] = {}

        for key, value in changes:
            descriptor = registry.get(key)
            if descriptor is None:
                unknown.append(
                    ErrorDetail(field=str(key), message="Unknown setting.")
                )
                continue
            try:
                validated[key] = validate_setting(descriptor, value)
            except InvalidSettingError as exc:
                # The **reason**, never the value: a rejected time zone is the
                # user's own text and has no business in an error envelope that
                # may be logged by whatever renders it.
                invalid.append(ErrorDetail(field=str(key), message=exc.reason))

        if unknown:
            self._metrics.record_failure(SettingsFailureReason.UNKNOWN_SETTING)
            raise UnknownSettingError(details=unknown)
        if invalid:
            self._metrics.record_failure(SettingsFailureReason.INVALID_VALUE)
            raise InvalidSettingValueError(details=invalid)

        return validated


def _count_by_section(
    validated: Mapping[Any, Any], registry: Mapping[Any, Any]
) -> dict[SettingsSection, int]:
    """Group a validated batch by the section its settings belong to.

    So the metrics answer *"the appearance section is being changed"* rather than
    *"settings are being changed"* — which is the only form of that sentence a
    product decision can be made from, and as far as this platform is willing to
    break the figure down. See :mod:`services.settings_metrics`.
    """
    counts: dict[SettingsSection, int] = {}
    for key in validated:
        section = registry[key].section
        counts[section] = counts.get(section, 0) + 1
    return counts


def resolve_current_session_id(payload: security.TokenPayload) -> str | None:
    """The session the presented access token belongs to, if it says.

    A one-line accessor with a docstring, because the ``None`` case is the
    interesting one: a token minted before the ``sid`` claim existed has none, so
    every session in the list comes back with ``is_current: false`` until the
    caller's next refresh. Signing out of "all other sessions" is still correct in
    that window — the device making the request is the one that gets the
    replacement pair, whatever the list said.
    """
    return payload.session_id


__all__ = [
    "MaintenanceStatus",
    "ResolvedSetting",
    "SessionListing",
    "SessionView",
    "SettingsMetrics",
    "SettingsService",
    "resolve_current_session_id",
]
