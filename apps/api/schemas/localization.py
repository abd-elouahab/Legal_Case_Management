"""Localization request and response models.

Three shapes, and what is *absent* from them is the point:

* :class:`LanguageCatalogRead` says which languages exist, which way each is
  written, and which one this caller is being addressed in. It carries **no
  names** — not "English", not "Français", not "العربية" — because a language's
  name is user-facing text and ``code-standards.md`` is unambiguous that an API
  response is a place a translation cannot live. The client's own catalogue names
  them, which is the same rule the dashboard's widget keys and the settings
  registry's value identifiers already follow;
* :class:`LocalizationReportCreate` is what a browser may tell the platform about
  its own translation problems, and it accepts **keys and catalogue names only**.
  There is no field for a rendered string and no field for an interpolated value,
  because either would carry a case name, a person, or a court into a metrics
  process — see :mod:`services.localization_metrics`;
* :class:`LocalizationMetricsRead` is the operational view, and reports counts by
  language and by key and never by account.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.localization import (
    SUPPORTED_LANGUAGES,
    locale_tag,
    text_direction,
)
from services.localization_metrics import (
    MAX_TRACKED_KEYS,
    LocalizationMetricsSnapshot,
    TranslationFailureReason,
)

#: Longest translation key accepted in a report.
#:
#: Generous for a dotted path (``cases.dialogs.archive.description``) and short
#: enough that the field cannot be used to smuggle a sentence into a metrics
#: process — which is the thing this whole endpoint is careful about.
MAX_KEY_LENGTH = 200

#: Longest catalogue name accepted. A locale, or a locale and a namespace.
MAX_CATALOGUE_LENGTH = 64


class LanguageRead(BaseModel):
    """One language the platform serves.

    Three fields, all of them machine-readable. ``direction`` is here rather than
    derived in the client because it is a property of the *language* and the
    platform already has to know it for email and PDF export — a second table in
    the browser would be a second place to add a fourth language to.
    """

    model_config = ConfigDict(from_attributes=True)

    code: str = Field(description="ISO 639-1 code, e.g. `fr`.")
    direction: str = Field(description="`ltr` or `rtl`.")
    locale: str = Field(
        description="BCP-47 tag this language formats dates, times, and numbers with."
    )

    @classmethod
    def for_language(cls, language: str) -> LanguageRead:
        """Describe one supported language."""
        return cls(
            code=language,
            direction=text_direction(language),
            locale=locale_tag(language),
        )


class LanguageCatalogRead(BaseModel):
    """Every language the platform serves, and the one this caller reads in.

    Served to every authenticated caller with no capability of its own: a language
    selector that could not list its options would be a selector nobody could use,
    and ``21-localization.md`` requires that *"language switching cannot affect
    application permissions"* — a permission on the catalogue would be the first
    step towards it doing so.
    """

    languages: list[LanguageRead] = Field(
        description="Supported languages, in display order."
    )
    default: str = Field(
        description=(
            "The application's default language — step 3 of the selection chain, "
            "and what an unavailable translation falls back to."
        )
    )
    resolved: str = Field(
        description=(
            "The language this caller is addressed in: their stored preference, "
            "then the platform's default, then the application's."
        )
    )
    direction: str = Field(description="Text direction of `resolved`.")
    locale: str = Field(description="BCP-47 tag of `resolved`.")


class LocalizationReportCreate(BaseModel):
    """What a client noticed while rendering, in keys and catalogue names.

    ``21-localization.md``'s Monitoring section asks for *"translation loading
    failures"* and *"missing translations"*, and both are events that happen in a
    browser: a catalogue is fetched there and a key is missed while React renders.
    This is the only path by which either reaches the platform's metrics.

    **Both lists are optional and a report with neither is accepted**, so a client
    can send one shape unconditionally at the end of a render pass without
    branching. It is counted, never stored: there is no table behind this
    endpoint, for the same reason there is none behind the RAG pipeline — a row
    per render would be write amplification derived from a page load.
    """

    missing_keys: list[str] = Field(
        default_factory=list,
        max_length=MAX_TRACKED_KEYS,
        description=(
            "Translation keys that were absent from the catalogue in force. Keys "
            "only — never the text they would have rendered to, and never the "
            "values they would have interpolated."
        ),
    )
    failures: list[TranslationFailureReason] = Field(
        default_factory=list,
        max_length=20,
        description="Why a catalogue could not be used, one entry per occurrence.",
    )
    catalogue: str | None = Field(
        default=None,
        max_length=MAX_CATALOGUE_LENGTH,
        description=(
            "Which catalogue the failures refer to — a locale, or a locale and a "
            "namespace. Never a URL: that would carry a deployment's host and path "
            "into a metric."
        ),
    )
    language: str | None = Field(
        default=None,
        max_length=16,
        description="The language in force when this was observed, if known.",
    )

    @field_validator("missing_keys", mode="after")
    @classmethod
    def _bound_keys(cls, value: list[str]) -> list[str]:
        """Keep keys that look like keys, and discard the rest.

        A translation key is a dotted identifier this repository already contains.
        Anything longer than :data:`MAX_KEY_LENGTH`, or carrying whitespace, is
        not one — it is a sentence, and a sentence is exactly what must not reach
        a metrics process from a browser. Discarded rather than refused, because
        the report is a courtesy and failing it would teach clients to stop
        sending one.
        """
        cleaned: list[str] = []
        for key in value:
            candidate = key.strip()
            if not candidate or len(candidate) > MAX_KEY_LENGTH:
                continue
            if any(character.isspace() for character in candidate):
                continue
            cleaned.append(candidate)
        return cleaned

    @field_validator("catalogue", "language", mode="after")
    @classmethod
    def _bound_identifier(cls, value: str | None) -> str | None:
        """Discard anything that is not a short, whitespace-free identifier."""
        if value is None:
            return None
        candidate = value.strip()
        if not candidate or any(character.isspace() for character in candidate):
            return None
        return candidate


class LocalizationMetricsRead(BaseModel):
    """Platform-wide localization health, as the monitoring endpoint reports it.

    Two provenances in one response, and the fields say which — the shape
    Notifications, Email, and Settings established. ``since`` qualifies the
    **process** counters above it; ``distribution`` and
    ``accounts_following_default`` are SQL aggregates and carry no such caveat,
    because they are properties of stored rows.
    """

    since: datetime = Field(
        description="When this process started counting. Applies to the counters only."
    )
    supported_languages: list[str] = Field(
        description="Every language the platform serves, in display order."
    )
    default_language: str = Field(description="The application's default language.")
    active_languages: list[str] = Field(
        description="Supported languages something has actually been rendered in."
    )
    resolutions_by_language: dict[str, int] = Field(
        description="How many times each language was resolved for a request."
    )
    unsupported_locale_requests: int = Field(
        description=(
            "Requests naming a language the platform does not serve. Each was "
            "answered in the default rather than refused, so this is a "
            "client-quality figure and not an error rate."
        )
    )
    translation_failures: int = Field(
        description="Catalogues a client could not load or parse."
    )
    failures_by_reason: dict[str, int] = Field(description="Those failures, by cause.")
    failing_catalogues: list[str] = Field(
        description="Which catalogues failed. Bounded; names only, never URLs."
    )
    missing_translations: int = Field(
        description="Renders that fell back because a key was absent."
    )
    distinct_missing_keys: int = Field(
        description=(
            "How many of those were distinct keys — the figure that says whether "
            "one string is missing or a whole namespace failed to ship."
        )
    )
    missing_keys: list[str] = Field(
        description="The keys themselves, bounded. The actionable half."
    )
    distribution: dict[str, int] = Field(
        description=(
            "Active accounts that have explicitly chosen each language, including "
            "the languages nobody chose as measured zeros. A SQL aggregate."
        )
    )
    accounts_following_default: int = Field(
        description=(
            "Active accounts that have expressed no preference and follow the "
            "platform default. A SQL aggregate."
        )
    )
    reporting_enabled: bool = Field(
        description="Whether clients may report missing keys and load failures."
    )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: LocalizationMetricsSnapshot,
        *,
        default_language: str,
        distribution: dict[str, int],
        accounts_following_default: int,
        reporting_enabled: bool,
    ) -> LocalizationMetricsRead:
        """Build the response from the two halves it is assembled out of."""
        return cls(
            since=snapshot.since,
            supported_languages=list(snapshot.supported_languages),
            default_language=default_language,
            active_languages=list(snapshot.active_languages),
            resolutions_by_language=snapshot.resolutions_by_language,
            unsupported_locale_requests=snapshot.unsupported_locale_requests,
            translation_failures=snapshot.translation_failures,
            failures_by_reason=snapshot.failures_by_reason,
            failing_catalogues=list(snapshot.failing_catalogues),
            missing_translations=snapshot.missing_translations,
            distinct_missing_keys=snapshot.distinct_missing_keys,
            missing_keys=list(snapshot.missing_keys),
            distribution=distribution,
            accounts_following_default=accounts_following_default,
            reporting_enabled=reporting_enabled,
        )


def supported_language_catalog() -> list[LanguageRead]:
    """Describe every supported language, in display order."""
    return [LanguageRead.for_language(language) for language in SUPPORTED_LANGUAGES]


__all__ = [
    "MAX_CATALOGUE_LENGTH",
    "MAX_KEY_LENGTH",
    "LanguageCatalogRead",
    "LanguageRead",
    "LocalizationMetricsRead",
    "LocalizationReportCreate",
    "supported_language_catalog",
]
