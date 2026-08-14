"""Settings endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.settings.SettingsService`, and shape the HTTP response
(including the refresh cookie, where a call re-issues one). No business logic
lives here.

**Every route is about the caller, and none of them takes a user identifier.**
``20-settings.md``'s *"users may modify only their own settings"* is enforced by
that absence rather than by a policy module: there is no path, query parameter,
or body field naming an account, so a caller cannot ask for somebody else's
settings because there is nowhere to put the request. That is why there is no
``settings_access.py`` — the question a per-resource policy would answer cannot
be asked.

**What is *not* here is the ownership rule made visible.** There is no route for
notification or communication preferences: those are the Notification Service's,
served from ``GET|PUT /notifications/preferences``, and ``GET /settings`` names
them in its section list rather than embedding a copy. A second endpoint serving
one stored thing is how two answers to one question start to disagree.

Authorization is layered, and the layers answer different questions:

* ``settings:view`` and ``settings:update`` are the caller's **own** settings.
  Both are in :data:`~core.roles.BASE_PERMISSIONS`, because a role that could not
  change its own language would be a role the platform is unusable in;
* ``settings:manage`` is the **platform's** configuration — a different table
  reached by different routes, held by administrators only. It is not a wider
  form of ``settings:update``, and holding every user-settings permission grants
  none of it;
* ``settings:monitor`` is the operational view, administrative like every other
  ``*:monitor``.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Cookie, Depends, Response, status

from api.authorization import require_permission
from api.deps import AccessTokenPayload, SettingsServiceDep
from api.v1.auth.cookies import set_refresh_cookie
from core.config import settings as app_settings
from core.permissions import Permission
from core.roles import permissions_for_role
from core.settings import (
    PLATFORM_SETTINGS,
    USER_SETTINGS,
    PlatformSettingKey,
    SettingDescriptor,
    SettingValueType,
    UserSettingKey,
)
from models.user import User
from schemas.auth import ChangePasswordRequest, ChangePasswordResponse
from schemas.errors import ErrorResponse
from schemas.settings import (
    MaintenanceStatusRead,
    PlatformSettingsRead,
    PlatformSettingsUpdate,
    ProfileRead,
    ProfileUpdate,
    SessionListRead,
    SessionRead,
    SessionRevocationResponse,
    SettingDefinitionRead,
    SettingRead,
    SettingsMetricsRead,
    SettingsOverviewRead,
    SettingsRead,
    SettingsSectionRead,
    SettingsUpdate,
)
from schemas.user import UserRead
from services.settings import ResolvedSetting, SettingsService

logger = structlog.get_logger(__name__)

#: Mounted under ``/settings``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

SettingsReader = Annotated[User, Depends(require_permission(Permission.SETTINGS_VIEW))]
SettingsEditor = Annotated[User, Depends(require_permission(Permission.SETTINGS_UPDATE))]
PlatformAdministrator = Annotated[
    User, Depends(require_permission(Permission.SETTINGS_MANAGE))
]
SettingsMonitor = Annotated[User, Depends(require_permission(Permission.SETTINGS_MONITOR))]

#: The httpOnly cookie carrying the refresh token.
#:
#: Read here the same way ``api/v1/auth/router.py`` reads it, because two routes
#: below **retire** the caller's outgoing refresh token and issue a replacement.
#: They would still be correct without it — the ``session_generation`` bump
#: invalidates it either way — but denylisting the token actually being retired is
#: what keeps password change, "sign out everywhere else", and logout behaving
#: identically toward the credential they are all replacing.
RefreshCookie = Annotated[
    str | None,
    Cookie(alias=app_settings.REFRESH_COOKIE_NAME, include_in_schema=False),
]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "The account is disabled or lacks the required permission.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "The request named a setting the platform does not define, or a value the "
            "setting does not accept. **Nothing was written** — a batch is validated in "
            "full before any of it is persisted."
        ),
    }
}


# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #


def _definition(key: str, descriptor: SettingDescriptor) -> SettingDefinitionRead:
    """Describe one setting's control for a client to render.

    ``max_length`` and ``max_items`` are reported as ``None`` where they do not
    apply rather than as ``0``: zero is a *limit of nothing*, which is a different
    statement from "this constraint does not exist here" — the same distinction
    the dashboard draws between a measured zero and an absent average.
    """
    return SettingDefinitionRead(
        key=key,
        section=descriptor.section,
        value_type=descriptor.value_type,
        choices=list(descriptor.choices),
        max_length=(
            descriptor.max_length
            if descriptor.value_type is SettingValueType.TEXT
            else None
        ),
        max_items=(
            descriptor.max_items
            if descriptor.value_type is SettingValueType.STRING_LIST
            else None
        ),
    )


def _to_user_settings(values: dict[UserSettingKey, ResolvedSetting]) -> SettingsRead:
    """Project the service's answer onto the response shape.

    Ordered by the platform's own declaration order rather than by the mapping's
    iteration order, so a settings page's rows do not move between requests — the
    same reason ``/notifications/preferences`` orders its own.
    """
    return SettingsRead(
        settings=[
            SettingRead(
                key=key.value,
                section=USER_SETTINGS[key].section,
                value=values[key].value,
                is_default=values[key].is_default,
            )
            for key in UserSettingKey
            if key in values
        ],
        definitions=[
            _definition(key.value, descriptor) for key, descriptor in USER_SETTINGS.items()
        ],
    )


def _to_platform_settings(
    values: dict[PlatformSettingKey, ResolvedSetting],
) -> PlatformSettingsRead:
    """Project the deployment's configuration onto the response shape."""
    return PlatformSettingsRead(
        settings=[
            SettingRead(
                key=key.value,
                section=PLATFORM_SETTINGS[key].section,
                value=values[key].value,
                is_default=values[key].is_default,
            )
            for key in PlatformSettingKey
            if key in values
        ],
        definitions=[
            _definition(key.value, descriptor)
            for key, descriptor in PLATFORM_SETTINGS.items()
        ],
    )


def _can_manage(actor: User) -> bool:
    """Whether this caller holds the administrator settings capability.

    Read from the role policy rather than from a role comparison, so a future
    supervising role granted ``settings:manage`` is served the Administration
    section without an edit here — the reason :mod:`core.permissions` names
    capabilities rather than roles.
    """
    return Permission.SETTINGS_MANAGE in permissions_for_role(actor.role)


# --------------------------------------------------------------------------- #
# The unified view
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=SettingsOverviewRead,
    status_code=status.HTTP_200_OK,
    summary="Everything the Settings page needs on first load",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_settings(
    actor: SettingsReader, service: SettingsServiceDep
) -> SettingsOverviewRead:
    """Return the caller's sections, profile, settings, and the platform's posture.

    **One request rather than four**, which is the argument `GET /dashboard` makes
    for its aggregated endpoint — and it stops at the same line. Notification and
    communication preferences are deliberately **not** in this response: the
    Notification Service owns them and serves them from
    `GET /notifications/preferences`. The section list says where they live, and
    the client fetches them from the feature that owns them, which is the spec's
    *"each feature should own its configuration"* visible in the shape of the API.

    `sections` is **server-described**, in the shape the dashboard's widget
    catalog established: a client renders its navigation from this rather than
    from a list of its own, so a tenth section reaches a browser nobody
    redeployed. An administrative section is **omitted** for a caller who may not
    manage it rather than served disabled — showing it would tell every lawyer
    which platform settings exist and that somebody else controls them.

    `maintenance` is the one piece of administrator configuration everybody may
    read, and the asymmetry is the point: the switch is administrative, the
    announcement is not. A maintenance notice only administrators can see is a
    notice nobody needed.
    """
    can_manage = _can_manage(actor)
    return SettingsOverviewRead(
        sections=[
            SettingsSectionRead(
                section=descriptor.section,
                storage=descriptor.storage,
                editable=descriptor.editable,
                administrative=descriptor.administrative,
            )
            for descriptor in service.sections(actor=actor, can_manage=can_manage)
        ],
        profile=ProfileRead.model_validate(actor),
        settings=_to_user_settings(service.user_settings(actor=actor)),
        maintenance=_maintenance(service),
    )


def _maintenance(service: SettingsService) -> MaintenanceStatusRead:
    status_value = service.maintenance_status()
    return MaintenanceStatusRead(
        maintenance_mode=status_value.maintenance_mode, message=status_value.message
    )


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


@router.get(
    "/profile",
    response_model=ProfileRead,
    status_code=status.HTTP_200_OK,
    summary="Your own profile",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_profile(actor: SettingsReader) -> ProfileRead:
    """Return the caller's own profile.

    Read straight off the authenticated user — there is no lookup to perform,
    because the account is the one that presented the token. That is also why
    there is no `GET /settings/profile/{user_id}`: reading somebody else's profile
    is User Management's `GET /users/{id}`, which requires `users:view`.
    """
    return ProfileRead.model_validate(actor)


@router.patch(
    "/profile",
    response_model=ProfileRead,
    status_code=status.HTTP_200_OK,
    summary="Update your own profile",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def update_profile(
    actor: SettingsEditor, service: SettingsServiceDep, payload: ProfileUpdate
) -> ProfileRead:
    """Change the caller's own name, phone, avatar, or job title.

    **Four fields, and deliberately not a fifth.** There is no `email`, no `role`,
    and no `status`: the spec puts email changes out of scope unless the
    authentication system already supports them (it does not — email is the login
    identifier), and role and status are administrative decisions about an account
    rather than preferences its owner holds. A self-service endpoint accepting a
    `role` field would be a privilege-escalation door however carefully the
    service behind it was written, so the field does not exist.

    A field the request omits is left alone; an explicit `null` (or a blank
    string) clears an optional one, which is how somebody removes a phone number
    they no longer want the platform to hold. Fields already holding the requested
    value are not rewritten, so a form that posts everything on every save
    produces no write when nothing changed.
    """
    updated = service.update_profile(payload.provided_fields(), actor=actor)
    return ProfileRead.model_validate(updated)


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


@router.get(
    "/preferences",
    response_model=SettingsRead,
    status_code=status.HTTP_200_OK,
    summary="Your appearance, language, AI, and dashboard settings",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_preferences(actor: SettingsReader, service: SettingsServiceDep) -> SettingsRead:
    """Return every setting the platform offers, with the caller's answer to each.

    The **complete** set rather than only the choices they have made, so a
    settings page renders from one response and a setting added later appears
    automatically at its default. `is_default` says which is which — an account
    that has expressed no opinion has no stored row at all, and showing a value
    without saying it is the platform's choice would imply somebody made it.

    `definitions` describes each control — its type, its permitted identifiers,
    its bounds — so the client renders a setting it has never heard of. It carries
    **no words**: the labels for those identifiers live in the client's own
    translation catalogue, because an API response is a place a translation cannot
    live.

    Notification and communication preferences are **not** here. They belong to
    the Notification Service and are read from `GET /notifications/preferences`.
    """
    return _to_user_settings(service.user_settings(actor=actor))


@router.put(
    "/preferences",
    response_model=SettingsRead,
    status_code=status.HTTP_200_OK,
    summary="Update your own settings",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def update_preferences(
    actor: SettingsEditor, service: SettingsServiceDep, payload: SettingsUpdate
) -> SettingsRead:
    """Set some of the caller's settings and return the complete set.

    **A list of changes rather than the whole set**, so two settings panels open
    at once cannot silently revert each other's saves, and so a setting added
    later does not make an older client's payload invalid. Anything omitted keeps
    its current value — or keeps having no stored row, and therefore keeps
    following the platform default an administrator set.

    **The whole batch is validated before any of it is written.** A request with
    one bad value answers 422 naming every offending key and changes nothing at
    all, which is the spec's *"invalid configuration should never corrupt stored
    preferences"*. A value equal to what is already stored produces no write, so
    saving a form nobody edited costs one query and no statement.
    """
    return _to_user_settings(
        service.update_user_settings(
            [(entry.setting_key, entry.value) for entry in payload.settings],
            actor=actor,
        )
    )


# --------------------------------------------------------------------------- #
# Account & security
# --------------------------------------------------------------------------- #


@router.get(
    "/sessions",
    response_model=SessionListRead,
    status_code=status.HTTP_200_OK,
    summary="Your active sessions",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def list_sessions(
    actor: SettingsReader,
    access_payload: AccessTokenPayload,
    service: SettingsServiceDep,
) -> SessionListRead:
    """List every sign-in that can still be resumed for the caller's account.

    Only the caller's. There is no route that lists anybody else's, and
    `settings:manage` does not grant one: where and when somebody signs in is a
    detailed statement about their working life, and an administrator does not
    need it to run the platform.

    A session is a **sign-in**, not a credential: the identifier is stable across
    the token rotations a browser performs every fifteen minutes, so one laptop
    appears once rather than as dozens of devices by the end of a day.
    `is_current` marks the one making this request, identified by the token
    presenting itself rather than by anything the client asserts.

    `available: false` means the session registry could not be read — the list is
    *unavailable*, not empty. Revocation is unaffected either way: what ends a
    session is `users.session_generation` in PostgreSQL, which this list does not
    consult.
    """
    listing = service.active_sessions(
        actor=actor, current_session_id=access_payload.session_id
    )
    return SessionListRead(
        sessions=[
            SessionRead(
                session_id=view.record.session_id,
                is_current=view.is_current,
                created_at=view.record.created_at,
                last_seen_at=view.record.last_seen_at,
                expires_at=view.record.expires_at,
                ip_address=view.record.ip_address,
                user_agent=view.record.user_agent,
            )
            for view in listing.sessions
        ],
        available=listing.available,
    )


@router.delete(
    "/sessions",
    response_model=SessionRevocationResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign out of every other session",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def revoke_other_sessions(
    response: Response,
    actor: SettingsEditor,
    access_payload: AccessTokenPayload,
    service: SettingsServiceDep,
    refresh_cookie: RefreshCookie = None,
) -> SessionRevocationResponse:
    """End every session for the caller's account except this one.

    **The same durable mechanism a password change uses**, deliberately rather
    than a second one: one write to `users.session_generation` invalidates every
    token minted under the previous generation, so a device the session registry
    never heard of is signed out exactly like one it did. There is no enumeration
    involved and nothing depends on Redis being reachable.

    The response carries a **replacement token pair** (and a new refresh cookie)
    so the device making the request stays signed in — clients must swap in the
    returned access token, exactly as after a password change.

    `DELETE` on the collection rather than on one member, because there is no
    endpoint that ends *another specific* session. Ending them all but this one is
    the control somebody reaches for when they think an account is compromised;
    picking one to keep alive is not.
    """
    tokens = service.revoke_other_sessions(
        actor=actor,
        current_access=access_payload,
        current_refresh_token=refresh_cookie,
    )
    set_refresh_cookie(response, tokens.refresh.token)
    return SessionRevocationResponse(
        message="Every other session has been signed out.",
        access_token=tokens.access.token,
        refresh_token=tokens.refresh.token,
        expires_in=tokens.access_expires_in,
    )


@router.post(
    "/password",
    response_model=ChangePasswordResponse,
    status_code=status.HTTP_200_OK,
    summary="Change your password",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "model": ErrorResponse,
            "description": "The current password is incorrect.",
        },
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_VALIDATION,
    },
)
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    actor: SettingsEditor,
    access_payload: AccessTokenPayload,
    service: SettingsServiceDep,
    refresh_cookie: RefreshCookie = None,
) -> ChangePasswordResponse:
    """Replace the caller's password and end every other session.

    **Delegated whole to the authentication system**, which is what the spec asks
    for — *"changing the password should integrate with the existing
    authentication system"*. `AuthService.change_password` requires the current
    password, clears `must_change_password`, and invalidates every other session
    by bumping `users.session_generation` while handing this device a replacement
    pair. None of that is re-implemented here.

    `POST /auth/change-password` remains the **authentication** surface — it is
    what the forced-change flow calls before the application shell has loaded —
    and this is the **settings** surface. Two doors, one service method, and no
    duplicated rule: the difference is which page the user is on, not what
    happens.
    """
    tokens = service.change_password(
        payload.current_password,
        payload.new_password,
        actor=actor,
        current_access=access_payload,
        current_refresh_token=refresh_cookie,
    )
    set_refresh_cookie(response, tokens.refresh.token)
    return ChangePasswordResponse(
        message="Password changed successfully. Other devices have been signed out.",
        access_token=tokens.access.token,
        refresh_token=tokens.refresh.token,
        expires_in=tokens.access_expires_in,
        user=UserRead.model_validate(actor),
    )


# --------------------------------------------------------------------------- #
# Administration
# --------------------------------------------------------------------------- #


@router.get(
    "/administration",
    response_model=PlatformSettingsRead,
    status_code=status.HTTP_200_OK,
    summary="The platform's own configuration",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_platform_settings(
    actor: PlatformAdministrator, service: SettingsServiceDep
) -> PlatformSettingsRead:
    """Return the deployment's configuration.

    **A different table behind a different permission**, which is the spec's
    *"administrator settings should remain isolated from regular user settings"*
    made structural: no key appears in both registries and no query reads both, so
    the isolation is not a rule anybody has to remember.

    Every `default_*` setting here is the fallback for the matching user setting,
    which is what makes these do something rather than merely be stored: an
    account that has expressed no opinion follows the platform's answer, and
    changing one reaches every such account at once — with no backfill, because
    there is nothing stored to back-fill.
    """
    del actor
    return _to_platform_settings(service.platform_settings())


@router.put(
    "/administration",
    response_model=PlatformSettingsRead,
    status_code=status.HTTP_200_OK,
    summary="Update the platform's configuration",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def update_platform_settings(
    actor: PlatformAdministrator,
    service: SettingsServiceDep,
    payload: PlatformSettingsUpdate,
) -> PlatformSettingsRead:
    """Set some of the deployment's configuration and return all of it.

    A list of changes rather than the whole set, and validated as one batch before
    anything is written — the same rules `PUT /settings/preferences` follows, and
    they matter more here: a half-applied change to platform defaults reaches
    every account that has expressed no opinion.

    Who made the change is recorded **on the row**, not only in the log: *"who
    turned maintenance mode on?"* is a question asked days later, from a database,
    by somebody who does not have the application's logs.

    **Maintenance mode announces; it does not block.** Turning it on puts a notice
    on every authenticated client (see `GET /settings/maintenance`) and refuses no
    request — refusing traffic would be a platform-wide behaviour change this spec
    does not describe, and a switch on a settings page should not be able to take
    the API down without a deployment having chosen that deliberately.
    """
    return _to_platform_settings(
        service.update_platform_settings(
            [(entry.setting_key, entry.value) for entry in payload.settings],
            actor=actor,
        )
    )


@router.get(
    "/maintenance",
    response_model=MaintenanceStatusRead,
    status_code=status.HTTP_200_OK,
    summary="Whether the platform is in maintenance",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_maintenance_status(
    actor: SettingsReader, service: SettingsServiceDep
) -> MaintenanceStatusRead:
    """Return the platform's maintenance posture.

    **The one administrator setting every authenticated caller may read**, because
    a maintenance notice only administrators can see is a notice nobody needed —
    the same shape a system announcement has, where one person decides and
    everybody is told. The *switch* stays administrative; only its announcement is
    public.

    A message is served only while maintenance mode is **on**. A message typed and
    not yet switched on is a draft, and serving it would put a stale notice on
    everybody's screen.
    """
    del actor
    return _maintenance(service)


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


@router.get(
    "/metrics",
    response_model=SettingsMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Settings metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_settings_metrics(
    actor: SettingsMonitor, service: SettingsServiceDep
) -> SettingsMetricsRead:
    """Return platform-wide settings health.

    The four figures the spec's Monitoring section names — **settings updated,
    failed updates, profile changes, and password changes** — plus the stored
    counts that say how much of the platform has been customised at all.

    **The figures come from two places, and the response says which.** Counts of
    rows are SQL aggregates: exact, surviving a restart, and the same on every API
    instance. Updates, failures, and the rest accumulate in *this* process and
    carry `since` — the same split `/notifications/metrics` and
    `/assistant/metrics` make.

    Broken down **by section, never by setting and never by person**. *"The
    appearance section is being changed"* is a throughput figure an operator can
    act on; *"theme: dark, 41"* is a statement about what people chose, and a
    breakdown keyed by account would be a live index of who configures what.

    An operational view, so it is gated on `settings:monitor`, administrative like
    every other `*:monitor`.
    """
    del actor
    metrics = service.metrics()
    statistics = metrics.statistics
    counters = metrics.counters

    return SettingsMetricsRead(
        since=counters.since,
        stored_user_settings=statistics.stored_user_settings,
        customised_users=statistics.customised_users,
        stored_platform_settings=statistics.stored_platform_settings,
        updated=counters.updated,
        failed=counters.failed,
        success_rate=counters.success_rate,
        profile_changes=counters.profile_changes,
        password_changes=counters.password_changes,
        session_revocations=counters.session_revocations,
        updated_by_section=counters.updated_by_section,
        failures_by_reason=counters.failures_by_reason,
    )


__all__ = ["router"]
