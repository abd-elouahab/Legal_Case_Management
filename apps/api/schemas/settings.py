"""Settings request and response schemas.

Two responsibilities, both required by the code standards: **validate every
request** before business logic runs, and **return a standardized structure**.
``20-settings.md`` adds a third that is specific to this feature — *"validate all
settings before persistence"* and *"invalid configuration should never corrupt
stored preferences"* — and the split between what is checked here and what is
checked in :mod:`core.settings` is where that is honoured.

**This layer checks the envelope; the registry checks the value.** Pydantic
rejects a payload that is not a list of ``{key, value}`` pairs, one that is empty,
and one longer than a settings form could plausibly be. It deliberately does
**not** know that ``theme`` accepts three strings, because that knowledge lives in
:data:`~core.settings.USER_SETTINGS` where the API schema, the client's rendering,
and the write path all read it from one declaration. A ``Literal`` union
enumerated here would be a second copy of every vocabulary, and the day the two
disagreed the schema would win silently.

**Updates are a list of changes, never the whole set** — the shape
:class:`~schemas.notification.NotificationPreferencesUpdate` established, for the
reasons it records and one more that is specific to this feature. Two settings
panels open at once cannot silently revert each other's saves; a client written
before a setting existed cannot reset it by omission; and — the new one — an
administrator's platform defaults are not overwritten by a page that happened to
render them, because a value nobody touched is never sent.

**Every value on the wire is a key, not a sentence.** A theme is ``"dark"``, a
date format is ``"day_month_year"``: the words a person reads live in the
client's own catalogue, exactly as widget labels and notification prose do. See
:mod:`core.settings` for why.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.settings import (
    PlatformSettingKey,
    SettingsSection,
    SettingsStorage,
    SettingValueType,
    UserSettingKey,
)
from core.users import (
    MAX_NAME_LENGTH,
    MAX_PHONE_LENGTH,
    InvalidPhoneNumberError,
    normalize_name,
    normalize_phone,
)
from schemas.user import (
    MAX_PROFILE_IMAGE_LENGTH,
    OptionalPhone,
    OptionalProfileImage,
    UserRead,
)

#: Longest ``job_title`` accepted, matching ``users.job_title``.
MAX_JOB_TITLE_LENGTH = 120

#: Most entries one settings update may carry.
#:
#: Comfortably more than the registry defines, so a client saving a whole page is
#: never refused, and bounded anyway — an unbounded list is an unbounded number of
#: validations and writes from one request. The same reasoning
#: ``NotificationReadRequest`` bounds its identifier list with.
MAX_SETTINGS_PER_REQUEST = 50


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


class ProfileUpdate(BaseModel):
    """The four fields ``20-settings.md``'s Profile section names.

    **And deliberately not a fifth.** There is no ``email``, no ``role``, and no
    ``status`` here: the spec puts email changes out of scope *"unless already
    supported by the authentication system"* (they are not — email is the login
    identifier), and role and status are administrative decisions about an
    account rather than preferences its owner holds. A self-service endpoint that
    accepted a ``role`` field would be a privilege-escalation door however
    carefully the service behind it was written, so the field does not exist.

    Every field is optional and **omission means "leave it alone"**, while an
    explicit ``null`` (or a blank string) clears an optional one — which is what
    lets somebody remove a phone number they no longer want the platform to hold.
    The distinction is carried by ``exclude_unset`` in :meth:`provided_fields`,
    the same mechanism :class:`~schemas.user.UserUpdate` uses.
    """

    model_config = ConfigDict(extra="forbid")

    first_name: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH, description="Given name."),
    ] = None
    last_name: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH, description="Family name."),
    ] = None
    phone: OptionalPhone = None
    profile_image: OptionalProfileImage = None
    job_title: Annotated[
        str | None,
        Field(
            default=None,
            max_length=MAX_JOB_TITLE_LENGTH,
            description="Self-described position, e.g. 'Senior Associate'.",
        ),
    ] = None

    # The normalizers are `core.users`' own, called rather than re-implemented,
    # so a name typed into the Settings page and a name typed into the
    # administrative user form are stored identically. A second set of rules here
    # is how the same person ends up as two different spellings.
    @field_validator("first_name", "last_name")
    @classmethod
    def _normalize_name_parts(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_name(value)
        if not normalized:
            raise ValueError("Name must not be blank.")
        return normalized

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            # An empty field in a form means "no phone", not "phone of length
            # zero" — which is also how somebody removes a number the platform
            # holds about them.
            return None
        try:
            return normalize_phone(value)
        except InvalidPhoneNumberError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("profile_image", "job_title")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    def provided_fields(self) -> dict[str, Any]:
        """Only the fields the request actually carried.

        ``exclude_unset`` rather than ``exclude_none``: a client clearing its
        phone number sends ``null`` and means it, while a client that omitted the
        field means nothing at all. Collapsing the two would make "remove my
        number" impossible to express.
        """
        return self.model_dump(exclude_unset=True)


class ProfileRead(BaseModel):
    """The caller's own profile, as the Settings page renders it.

    A **projection of** :class:`~schemas.user.UserRead` rather than a second user
    shape: it carries the identity fields a person edits plus the read-only ones
    they need to see beside them (email, role, when they last signed in). The
    permissions list and the audit columns are deliberately absent — a profile
    form is not the place to learn what one may do.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique user identifier.")
    email: str = Field(description="Login email address. Read-only — see `ProfileUpdate`.")
    first_name: str = Field(description="Given name.")
    last_name: str = Field(description="Family name.")
    full_name: str = Field(description="Display name, composed from the name parts.")
    phone: str | None = Field(default=None, description="Contact phone number, if provided.")
    profile_image: str | None = Field(default=None, description="Avatar location, if set.")
    job_title: str | None = Field(default=None, description="Self-described position, if set.")
    role: str = Field(description="Platform role. Read-only; changed by an administrator.")
    status: str = Field(description="Account lifecycle state. Read-only.")
    must_change_password: bool = Field(
        default=False,
        description="Whether a password change is required before continuing.",
    )
    last_login_at: datetime | None = Field(
        default=None, description="Timestamp of the previous successful sign-in."
    )
    created_at: datetime = Field(description="When the account was created.")
    updated_at: datetime = Field(description="When the account was last modified.")


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


class SessionRead(BaseModel):
    """One live sign-in, as the Account & Security section lists it.

    Carries **no credential** — no token, no ``jti``, not even the account it
    belongs to, which the caller already knows because it is theirs. The
    identifier is opaque and grants nothing; it exists so a client can say *"this
    one is you"*.
    """

    session_id: str = Field(description="Opaque identifier for this sign-in. Grants nothing.")
    is_current: bool = Field(
        description="Whether this is the session making the request."
    )
    created_at: datetime = Field(description="When the sign-in happened.")
    last_seen_at: datetime = Field(
        description="When a request on this session was last seen (a browser refreshes periodically)."
    )
    expires_at: datetime = Field(
        description="When the session can no longer be resumed without signing in again."
    )
    ip_address: str | None = Field(
        default=None, description="Client address as the API resolved it, if available."
    )
    user_agent: str | None = Field(
        default=None,
        description="Client's `User-Agent`, truncated. Display only — never parsed.",
    )


class SessionListRead(BaseModel):
    """Every live sign-in for the caller's account."""

    sessions: list[SessionRead] = Field(
        default_factory=list, description="Live sessions, newest sign-in first."
    )
    available: bool = Field(
        description=(
            "Whether the session registry could be read. `false` means the list is "
            "unavailable rather than empty — revocation still works, because it does "
            "not depend on this record."
        )
    )


class SessionRevocationResponse(BaseModel):
    """The result of ending every other session.

    Carries a **replacement token pair**, exactly as
    :class:`~schemas.auth.ChangePasswordResponse` does and for the same reason:
    the revocation invalidates every token for the account including the one that
    made the request, so the calling device is handed a new pair minted under the
    new generation. Clients must swap in the returned access token.
    """

    message: str = Field(description="Human-readable confirmation.")
    access_token: str = Field(description="Replacement access token for this session.")
    refresh_token: str = Field(description="Replacement refresh token (also set as a cookie).")
    token_type: str = Field(default="bearer", description="Always `bearer`.")
    expires_in: int = Field(description="Access token lifetime in seconds.")


# --------------------------------------------------------------------------- #
# Settings values
# --------------------------------------------------------------------------- #


class SettingDefinitionRead(BaseModel):
    """What a client needs to render one setting's control.

    **Served rather than hard-coded in the browser**, which is what makes the
    spec's *"support future sections without redesign"* true on the client as well
    as on the server: a tenth setting appears with its own control in a build
    nobody redeployed — the same property ``19-dashboard-analytics.md`` gave the
    widget catalog.

    It carries the vocabulary and **not the words**: ``choices`` is a list of
    stable identifiers, and their labels live in the client's translation
    catalogue. An API response is a place a translation cannot live.
    """

    key: str = Field(description="Stable setting identifier.")
    section: SettingsSection = Field(description="Which part of the page this belongs to.")
    value_type: SettingValueType = Field(description="How the value is carried and checked.")
    choices: list[str] = Field(
        default_factory=list,
        description="Permitted identifiers, for `enum` and `string_list` settings.",
    )
    max_length: int | None = Field(
        default=None, description="Longest accepted text, for `text` settings."
    )
    max_items: int | None = Field(
        default=None, description="Most accepted entries, for `string_list` settings."
    )


class SettingRead(BaseModel):
    """One setting, with this caller's answer to it."""

    key: str = Field(description="Stable setting identifier.")
    section: SettingsSection = Field(description="Which part of the page this belongs to.")
    value: Any = Field(description="The effective value: the caller's choice, or the default.")
    is_default: bool = Field(
        description=(
            "Whether this is the platform's answer rather than a choice the caller made. "
            "An account that has expressed no opinion has no stored row at all."
        )
    )


class SettingUpdate(BaseModel):
    """One change to one setting.

    The **key is validated as an enum member and the value is not**, which is the
    split the module docstring explains: an unknown key is refused here with a
    per-field message, while the value is checked against the key's descriptor in
    :func:`~core.settings.validate_setting` — the one place that knows what
    ``theme`` accepts.
    """

    model_config = ConfigDict(extra="forbid")

    setting_key: UserSettingKey = Field(description="Which setting to change.")
    value: Any = Field(description="The new value, of the shape the setting's definition declares.")


class SettingsUpdate(BaseModel):
    """A batch of changes to the caller's own settings."""

    model_config = ConfigDict(extra="forbid")

    settings: list[SettingUpdate] = Field(
        min_length=1,
        max_length=MAX_SETTINGS_PER_REQUEST,
        description="The settings to change. Anything omitted keeps its current value.",
    )

    @field_validator("settings")
    @classmethod
    def _reject_duplicate_keys(cls, value: list[SettingUpdate]) -> list[SettingUpdate]:
        """Refuse a payload naming the same setting twice.

        Two entries for one key have no defensible resolution: taking the last
        would make the outcome depend on JSON ordering, and merging them is
        meaningless for a scalar. Refusing says so, and it catches the client bug
        — a form serialising a field twice — at the boundary rather than after a
        write.
        """
        keys = [entry.setting_key for entry in value]
        if len(set(keys)) != len(keys):
            raise ValueError("Each setting may appear only once per request.")
        return value


class SettingsRead(BaseModel):
    """Every setting the platform offers, with the caller's answer to each.

    The **complete** set rather than only the choices they have made, exactly as
    ``GET /notifications/preferences`` returns: a settings page renders from one
    response, a setting added later appears automatically at its default, and no
    client ever has to know that "no stored row" means "the default".
    """

    settings: list[SettingRead] = Field(
        default_factory=list, description="Every user setting, in the platform's own order."
    )
    definitions: list[SettingDefinitionRead] = Field(
        default_factory=list,
        description="How to render each setting's control. Served, never hard-coded in the client.",
    )


# --------------------------------------------------------------------------- #
# Administration
# --------------------------------------------------------------------------- #


class PlatformSettingUpdate(BaseModel):
    """One change to one platform setting."""

    model_config = ConfigDict(extra="forbid")

    setting_key: PlatformSettingKey = Field(description="Which platform setting to change.")
    value: Any = Field(description="The new value, of the shape the setting's definition declares.")


class PlatformSettingsUpdate(BaseModel):
    """A batch of changes to the deployment's configuration."""

    model_config = ConfigDict(extra="forbid")

    settings: list[PlatformSettingUpdate] = Field(
        min_length=1,
        max_length=MAX_SETTINGS_PER_REQUEST,
        description="The platform settings to change. Anything omitted keeps its current value.",
    )

    @field_validator("settings")
    @classmethod
    def _reject_duplicate_keys(
        cls, value: list[PlatformSettingUpdate]
    ) -> list[PlatformSettingUpdate]:
        """Refuse a payload naming the same setting twice — see :class:`SettingsUpdate`."""
        keys = [entry.setting_key for entry in value]
        if len(set(keys)) != len(keys):
            raise ValueError("Each setting may appear only once per request.")
        return value


class PlatformSettingsRead(BaseModel):
    """The deployment's configuration, as an administrator sees it."""

    settings: list[SettingRead] = Field(
        default_factory=list, description="Every platform setting, in the platform's own order."
    )
    definitions: list[SettingDefinitionRead] = Field(
        default_factory=list, description="How to render each control."
    )


class MaintenanceStatusRead(BaseModel):
    """The one piece of administrator configuration everybody may read.

    ``20-settings.md`` puts maintenance mode under Administration, which is
    visible only to administrators — but a maintenance *notice* that only
    administrators can see is a notice nobody needed. So the **switch** is
    administrative and the **announcement** is public to authenticated callers,
    which is the same shape the platform already uses for a system announcement:
    an administrator decides, everybody is told.

    The message is an administrator's own words, so it travels as text and is
    escaped by whatever renders it — the client's usual defence, and the reason
    :mod:`services.email_templates` keeps two Jinja environments for the same
    input.
    """

    maintenance_mode: bool = Field(description="Whether the platform is in maintenance.")
    message: str | None = Field(
        default=None, description="What an administrator wants everyone to know, if anything."
    )


# --------------------------------------------------------------------------- #
# The unified view
# --------------------------------------------------------------------------- #


class SettingsSectionRead(BaseModel):
    """One section of the Settings page, as the API describes it.

    **The navigation is server-described**, in the shape the dashboard's widget
    catalog established. A client renders its section list from this rather than
    from a list of its own, so a tenth section reaches a browser nobody
    redeployed — which is what the spec's *"the implementation should support
    future sections without redesign"* has to mean if it is to mean anything on
    the client as well.
    """

    section: SettingsSection = Field(description="Stable section identifier.")
    storage: SettingsStorage = Field(
        description=(
            "Where this section's values live, and therefore which endpoint writes them. "
            "Three sections store nothing here: the features that own them do."
        )
    )
    editable: bool = Field(description="Whether this caller may change anything in it.")
    administrative: bool = Field(
        description="Whether this section is administrator-only. Never served to anybody else."
    )


class SettingsOverviewRead(BaseModel):
    """Everything the Settings page needs on first load, in one response.

    One request rather than six, which is the same argument ``GET /dashboard``
    makes for its aggregated endpoint — and it stops short at the same line.
    **Notification and communication preferences are deliberately not here**: the
    Notification Service owns them and serves them from
    ``GET /notifications/preferences``, so embedding a copy would create a second
    source for one stored thing and a second place for it to go stale. The section
    descriptor says where they live, and the client fetches them from the feature
    that owns them — which is the spec's *"each feature should own its
    configuration"* visible in the shape of the API rather than only in a
    docstring.
    """

    sections: list[SettingsSectionRead] = Field(
        description="The sections this caller may see, in the platform's own order."
    )
    profile: ProfileRead = Field(description="The caller's own profile.")
    settings: SettingsRead = Field(description="Every user setting, with the caller's answer.")
    maintenance: MaintenanceStatusRead = Field(
        description="The platform's maintenance posture, which every authenticated caller may read."
    )


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class SettingsMetricsRead(BaseModel):
    """Platform-wide settings health.

    **Two sources, and the response says which**, exactly as
    ``/notifications/metrics`` does. The three ``stored_*`` figures are SQL
    aggregates over rows: exact, surviving a restart, and the same on every API
    instance. Everything else accumulates in *this* process and is qualified by
    `since`.
    """

    since: datetime = Field(description="When this process started counting the rate figures.")

    stored_user_settings: int = Field(description="Individual choices people have stored.")
    customised_users: int = Field(
        description="Accounts with at least one stored setting. Everybody else follows the defaults."
    )
    stored_platform_settings: int = Field(
        description="How much of the deployment has been configured away from its built-in answer."
    )

    updated: int = Field(description="Settings that took a new value, this process.")
    failed: int = Field(description="Attempted changes that did not take effect, this process.")
    success_rate: float = Field(description="Share of attempted changes that took effect (%).")
    profile_changes: int = Field(description="Profile fields changed, this process.")
    password_changes: int = Field(
        description="Passwords changed through the Settings API, this process."
    )
    session_revocations: int = Field(description="'Log out other sessions' actions, this process.")

    updated_by_section: dict[str, int] = Field(
        default_factory=dict,
        description="Changes per section. Never per setting, and never per person.",
    )
    failures_by_reason: dict[str, int] = Field(
        default_factory=dict, description="Failures per cause."
    )


__all__ = [
    "MAX_JOB_TITLE_LENGTH",
    "MAX_PHONE_LENGTH",
    "MAX_PROFILE_IMAGE_LENGTH",
    "MAX_SETTINGS_PER_REQUEST",
    "MaintenanceStatusRead",
    "PlatformSettingUpdate",
    "PlatformSettingsRead",
    "PlatformSettingsUpdate",
    "ProfileRead",
    "ProfileUpdate",
    "SessionListRead",
    "SessionRead",
    "SessionRevocationResponse",
    "SettingDefinitionRead",
    "SettingRead",
    "SettingUpdate",
    "SettingsMetricsRead",
    "SettingsOverviewRead",
    "SettingsRead",
    "SettingsSectionRead",
    "SettingsUpdate",
    "UserRead",
]
