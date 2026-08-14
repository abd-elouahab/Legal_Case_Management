"""The platform's language vocabulary, and the rules that resolve one.

``21-localization.md`` asks for a platform that *"supports multiple languages
while keeping business logic, authorization, and application behaviour completely
independent from localization"*, and for an implementation that *"makes it easy
to add new languages without modifying application logic"*. This module is the
half of that with no I/O in it: which languages exist, which way each is written,
which one is used when nothing else answers, and how a request's, an account's,
or a browser's preference is turned into one of them.

**It is a vocabulary, never a translator.** Nothing here holds a sentence. The
platform's own wording lives where the feature that says it lives —
:mod:`core.notifications` for a notification, :mod:`core.email` for the chrome
around one, :mod:`core.reports` for a report's headings, :mod:`core.rag` for the
"no supporting evidence" line — and the interface's words live in
``apps/web/messages``. What this module contributes is the *code* those tables are
keyed by, so there is exactly one answer to "which language is this?" on the
platform.

Four things live here, and each is a requirement of the spec made mechanical:

* **Which languages the platform serves** — :data:`SUPPORTED_LANGUAGES`, the
  three ``21-localization.md`` names, English first because it is the shipped
  application default. Adding a fourth is one entry here plus one message
  catalogue, which is what *"allow future languages without redesign"* means
  concretely.
* **The application default** — :func:`default_language`, read from the
  deployment's ``DEFAULT_LANGUAGE`` rather than frozen into the code, because it
  is step 3 of the spec's selection chain *and* the last step of its fallback
  strategy, and a deployment serving one country should be able to move it
  without a release.
* **How a preference becomes a language** — :func:`resolve_language`, which takes
  the candidates in priority order and returns the first one the platform serves.
  Every resolver on the platform (a notification's, an email's, a WhatsApp
  message's, a report's, an answer's) is a call to this, so *"failures should
  gracefully fall back to the default language"* is one function rather than six
  agreements.
* **Which way it is written** — :func:`text_direction`, because Arabic needs
  ``rtl`` everywhere a direction is declared: the ``dir`` attribute of an email,
  of the web application's document element, and of a PDF export.

**Nothing here can affect a decision.** There is no permission, no scope, no
identifier, and no query in this module, which is the structural half of the
spec's *"localization must never affect authorization, RBAC, routing, database
schema, business rules, or workflow execution"*. A language is chosen after every
one of those has already been decided, and choosing a different one changes only
which words are used to describe the same answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

from core.config import settings
from core.indexing import LANGUAGE_ARABIC, LANGUAGE_ENGLISH, LANGUAGE_FRENCH

# --------------------------------------------------------------------------- #
# The languages
# --------------------------------------------------------------------------- #

#: Every language the platform serves, in the order ``21-localization.md`` lists
#: them.
#:
#: **English first, and that is the spec's own word rather than a preference.**
#: *"Implement support for: English (default), French, Arabic."* The order is not
#: decorative: it is the order a language selector offers, and the first member is
#: what :func:`default_language` falls back to when a deployment has configured
#: nothing.
#:
#: Codes are ISO 639-1 and are the same three
#: :func:`~core.indexing.detect_language` labels a passage with — imported rather
#: than re-spelled, so a document's detected language and a reader's chosen one
#: are comparable values rather than two vocabularies that happen to agree.
SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = (
    LANGUAGE_ENGLISH,
    LANGUAGE_FRENCH,
    LANGUAGE_ARABIC,
)

#: The same set, for membership tests that should not scan a tuple.
SUPPORTED_LANGUAGE_SET: Final[frozenset[str]] = frozenset(SUPPORTED_LANGUAGES)

#: The language used when nothing else resolves and the deployment has not said.
#:
#: Separate from :func:`default_language` so that a configuration error — an
#: unsupported ``DEFAULT_LANGUAGE`` reaching a running process somehow — still
#: has an answer rather than a ``KeyError`` on a page load. Configuration is
#: validated at import, so this is defence in depth rather than the ordinary path.
FALLBACK_LANGUAGE: Final[str] = LANGUAGE_ENGLISH

#: Which way each language is written, for the one attribute that has to say so.
#:
#: Every surface that declares a direction reads this table: the ``dir`` of an
#: email's ``<html>``, of the web application's document element, and the
#: right-to-left shaping an Arabic PDF export needs. A language absent from it is
#: ``ltr``, which is the safe default for a Latin-script language nobody has
#: classified yet — an Arabic-script one would be added here in the same commit
#: that added it to :data:`SUPPORTED_LANGUAGES`.
TEXT_DIRECTIONS: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_ENGLISH: "ltr",
        LANGUAGE_FRENCH: "ltr",
        LANGUAGE_ARABIC: "rtl",
    }
)

#: The BCP-47 tag each language is *formatted* with.
#:
#: A language code says which words; a locale tag says which conventions — the
#: order of a date's parts, the separator inside a number, and which digits are
#: drawn. **Regional tags rather than bare codes, deliberately**: bare ``ar``
#: leaves the formatter to choose a region and some choose Eastern Arabic
#: numerals, which would render a case number's year unreadable to a French
#: colleague on the same matter. The web application pins the same three, and a
#: test asserts the two tables agree — two halves of one platform formatting the
#: same instant differently is a defect a reader would report as a bug in the
#: data.
LOCALE_TAGS: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_ENGLISH: "en-GB",
        LANGUAGE_FRENCH: "fr-FR",
        LANGUAGE_ARABIC: "ar-MA",
    }
)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def default_language() -> str:
    """The application's default language, as this deployment has configured it.

    Step 3 of ``21-localization.md``'s selection chain (*"application default
    language"*) and the last step of its fallback strategy (*"use the default
    language"*), which are the same value asked for by two different questions.

    Read from :mod:`core.config` on every call rather than captured at import,
    because a test overriding the setting must not have to reload this module —
    the same reason :func:`~core.rag.question_fingerprint` reads its salt per
    call. The lookup is a attribute read on an already-constructed object, so the
    cost is not worth a cache that would need invalidating.
    """
    configured = normalize_language(settings.DEFAULT_LANGUAGE)
    return configured or FALLBACK_LANGUAGE


def normalize_language(value: str | None) -> str | None:
    """Reduce a language tag to a supported code, or ``None``.

    Accepts what the outside world actually sends — ``"fr"``, ``"FR"``,
    ``" fr-FR "``, ``"ar-MA"``, ``"en_GB"`` — and returns the platform's own code
    for it, because a browser's ``navigator.language``, an ``Accept-Language``
    header, and a stored setting are three different spellings of one idea.

    **Returns ``None`` rather than a default for anything unsupported**, and that
    is what makes it composable: :func:`resolve_language` walks a list of
    candidates and needs to tell *"this candidate did not answer"* apart from
    *"this candidate said English"*. A caller wanting a guaranteed answer uses
    :func:`resolve_language`, which is every caller on the platform.
    """
    if not value:
        return None

    primary = re.split(r"[-_]", value.strip(), maxsplit=1)[0].lower()
    return primary if primary in SUPPORTED_LANGUAGE_SET else None


def is_supported(value: str | None) -> bool:
    """Whether the platform serves this language, in any spelling of its tag."""
    return normalize_language(value) is not None


def resolve_language(*candidates: str | None) -> str:
    """The first supported language among ``candidates``, or the default.

    **The whole of the spec's Language Selection and Fallback Strategy sections,
    as one function**, because both are the same operation asked at different
    layers: *"user preference stored in Settings, browser language, application
    default"* is a caller passing three candidates, and *"if a translation is
    unavailable, use the default language"* is that caller passing none that
    resolve.

    Callers pass their candidates in **priority order** and never branch on
    emptiness themselves — an explicit request first, then the account's stored
    preference, then whatever context suggests. That is why every language
    resolver on the platform is a one-line call to this: a second implementation
    would be a second place for the priority to drift, and the reader who noticed
    would be an Arabic-speaking lawyer receiving a French email about a hearing.

    Unsupported and malformed candidates are **skipped rather than rejected**,
    which is the spec's *"handle invalid locale / unsupported language"*: a client
    sending ``"de"`` gets the platform's default rather than an error page, and
    the request it was attached to still succeeds. Counting those requests is
    :mod:`services.localization_metrics`' job, not this function's — a pure
    resolver that recorded a metric would be unusable from a template.
    """
    for candidate in candidates:
        resolved = normalize_language(candidate)
        if resolved is not None:
            return resolved
    return default_language()


def text_direction(language: str | None) -> str:
    """``"rtl"`` for Arabic, ``"ltr"`` for everything else.

    Takes the *language* rather than a resolved one so a caller cannot forget to
    resolve first and get a direction for a code the platform does not serve: an
    unknown value is ``ltr``, which is the reading that leaves a Latin-script page
    correct rather than mirrored.
    """
    if language is None:
        return "ltr"
    return TEXT_DIRECTIONS.get(language.strip().lower(), "ltr")


def locale_tag(language: str | None) -> str:
    """The BCP-47 tag ``language`` formats dates, times, and numbers with."""
    resolved = normalize_language(language) or default_language()
    return LOCALE_TAGS.get(resolved, LOCALE_TAGS[FALLBACK_LANGUAGE])


def parse_accept_language(header: str | None) -> str | None:
    """The best supported language named by an ``Accept-Language`` header.

    ``21-localization.md`` puts *browser language* second in its selection chain,
    and a browser states that preference two ways: ``navigator.language``, which
    the web application reads directly, and this header, which is what any other
    client has. Both end here.

    Quality values are honoured in the order they weight, and a tag the platform
    does not serve is skipped rather than ending the walk — ``"de, ar;q=0.8"``
    resolves to Arabic, because the reader named a language this platform speaks
    and preferring the first *listed* entry over the first *supported* one would
    ignore them. ``None`` when nothing in the header resolves, so a caller can
    fall through to the next candidate rather than being handed a default that
    hides the fact that the browser said nothing useful.
    """
    if not header:
        return None

    weighted: list[tuple[float, int, str]] = []
    for position, part in enumerate(header.split(",")):
        tag, _, parameters = part.strip().partition(";")
        language = normalize_language(tag)
        if language is None:
            continue

        quality = 1.0
        for parameter in parameters.split(";"):
            name, _, value = parameter.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        # Position breaks ties, so a header listing two languages at the same
        # weight keeps the order the browser wrote them in.
        weighted.append((-quality, position, language))

    if not weighted:
        return None
    return min(weighted)[2]


def supported_languages() -> tuple[str, ...]:
    """Every language the platform serves, in display order.

    A function rather than the constant so the API's catalogue endpoint and a
    future deployment-scoped subset have one call site to change; today it returns
    :data:`SUPPORTED_LANGUAGES` unchanged.
    """
    return SUPPORTED_LANGUAGES


def language_distribution_keys() -> Iterable[str]:
    """Every language a distribution metric must report, including empty ones.

    A metric that omitted the languages nobody chose would show a deployment its
    Arabic column only once somebody had already switched, which is exactly when
    it stops being the interesting number. Reported as measured zeros instead —
    the same rule ``19-dashboard-analytics.md`` states for a breakdown's empty
    buckets.
    """
    return SUPPORTED_LANGUAGES


__all__ = [
    "FALLBACK_LANGUAGE",
    "LOCALE_TAGS",
    "SUPPORTED_LANGUAGES",
    "SUPPORTED_LANGUAGE_SET",
    "TEXT_DIRECTIONS",
    "default_language",
    "is_supported",
    "language_distribution_keys",
    "locale_tag",
    "normalize_language",
    "parse_accept_language",
    "resolve_language",
    "supported_languages",
    "text_direction",
]
