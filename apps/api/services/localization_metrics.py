"""Localization observability.

``21-localization.md``'s Monitoring section names four figures: **active
languages, translation loading failures, missing translations, and language
distribution.** All four are here, in the shape :mod:`services.search_metrics`,
:mod:`services.rag_metrics`, :mod:`services.notification_metrics`,
:mod:`services.dashboard_metrics`, and :mod:`services.settings_metrics`
established — a protocol, an in-memory implementation, a null implementation, and
a frozen snapshot.

**The figures come from two places, and the endpoint says which**, exactly as
Notifications', Email's, and Settings' do. *Language distribution* is a property
of stored rows and is a SQL aggregate
(:meth:`~repositories.localization.LocalizationRepository.language_distribution`),
because a number counted in one API instance's memory would reset on deploy and
be wrong across replicas. *Active languages, load failures, and missing
translations* cannot be aggregates — nothing records a page render as a row — so
they accumulate here and carry ``since``.

**Two of the four are observable only in a browser**, and that is the one genuinely
new thing about this module. A translation catalogue is loaded by the web
application and a key is missed while React renders, so neither event happens on
this side of the network at all. The platform's other client-side facts — a
notification arriving, a socket reconnecting — are deliberately *not* reported,
because they are the client's own concern; these two are different, because
``21-localization.md`` names them as things an operator must be able to see, and a
missing translation is invisible to everybody except the person reading a screen
in the wrong language. So the API accepts a **report** of them, and the shape of
what it accepts is the whole of this module's privacy story.

**What a report may contain is a key, and nothing else.** A translation key
(``cases.filters.status``) is a name this repository already contains, checked
into version control and readable by anyone with the source; the *text* it renders
to may name a case, a person, or a court, and the value it was interpolated with
certainly may. So the recorder counts by key, has no parameter for a value, and
holds a bounded set of distinct keys — which is also what keeps a client looping
over a missing key from growing this process's memory. That is the same line
:mod:`services.notification_metrics` draws when it counts by rule rather than by
recipient.

**Counted by language and by key, never by person.** There is deliberately no
per-account figure here and no method that could produce one: *"eleven renders
missed ``cases.empty.title``"* is a defect an operator can fix, while *"this
lawyer reads in Arabic"* is a fact about a person that a monitoring view has no
business assembling. The distribution beside it is a **count per language** for the
same reason.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

from core.localization import SUPPORTED_LANGUAGES

#: Most distinct missing keys kept at once.
#:
#: A ceiling rather than an unbounded set, because the reporter is a browser and a
#: client rendering a broken catalogue would otherwise add one entry per key per
#: page. Past it the *count* still rises and new keys stop being named, which
#: degrades the report from "which keys" to "how many" rather than from "which
#: keys" to an out-of-memory error — and by the time a deployment is past two
#: hundred distinct missing keys, the answer is "the catalogue is broken" rather
#: than a list.
MAX_TRACKED_KEYS: Final[int] = 200

#: Most distinct catalogue names kept, for the same reason. There is one per
#: language per namespace, so this is generous by an order of magnitude.
MAX_TRACKED_CATALOGUES: Final[int] = 50


class TranslationFailureReason(StrEnum):
    """Why a translation catalogue could not be used.

    A closed vocabulary rather than free text, for the reason every other
    ``*_metrics`` module here uses one: these are grouped in a monitoring view,
    and a message interpolated from an exception produces a breakdown with one
    bucket per occurrence.
    """

    #: The catalogue could not be fetched — offline, a 404 after a deploy, a
    #: chunk the CDN has not got yet.
    LOAD_FAILED = "load_failed"
    #: It arrived and could not be parsed. A different fault from the above and a
    #: different fix: this one is a build problem, that one is a delivery problem.
    PARSE_FAILED = "parse_failed"
    #: A locale nobody serves was requested. The spec's *"unsupported locale
    #: requests"*, and the one entry here that is usually a client bug rather than
    #: an outage.
    UNSUPPORTED_LOCALE = "unsupported_locale"
    #: Anything unanticipated. Always worth investigating.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LocalizationMetricsSnapshot:
    """The counters at one instant, as the monitoring endpoint reports them.

    Frozen rather than a live view of the recorder, for the reason every other
    snapshot here is: reading separately-updated counters while other threads
    increment them produces a report whose numbers contradict each other.
    """

    #: When this process started counting.
    since: datetime

    #: Languages the platform serves, in display order. A constant rather than a
    #: measurement, and reported anyway: an operator reading *"language
    #: distribution"* needs to know which columns are missing because nobody chose
    #: them and which are missing because the platform does not offer them.
    supported_languages: tuple[str, ...]

    #: Requests that resolved to each language, counted where a language is
    #: actually decided. The spec's *"active languages"*: which of the supported
    #: set is being **used**, as opposed to which is stored on an account.
    resolutions_by_language: dict[str, int]

    #: Requests naming a language the platform does not serve. Each was answered in
    #: the default rather than refused — see
    #: :func:`~core.localization.resolve_language` — so this is a client-quality
    #: figure, not an error rate.
    unsupported_locale_requests: int

    #: Catalogues a client could not load, by cause.
    translation_failures: int
    failures_by_reason: dict[str, int]
    #: Which catalogues failed, bounded by :data:`MAX_TRACKED_CATALOGUES`. A name
    #: like ``ar`` or ``ar/cases`` — never a URL, which would carry a deployment's
    #: host and path.
    failing_catalogues: tuple[str, ...]

    #: Renders that fell back because a key was absent from the active catalogue.
    missing_translations: int
    #: How many of those were *distinct* keys. The number that says whether one
    #: string is missing everywhere or a whole namespace failed to ship.
    distinct_missing_keys: int
    #: The keys themselves, bounded by :data:`MAX_TRACKED_KEYS` — the actionable
    #: half, and the only content this module ever holds.
    missing_keys: tuple[str, ...]

    @property
    def active_languages(self) -> tuple[str, ...]:
        """Supported languages that something has actually been rendered in.

        *"Active languages"* read the way an operator means it: not the catalogue
        of what is offered — that is :attr:`supported_languages` — but which of
        them this deployment is really serving.
        """
        return tuple(
            language
            for language in self.supported_languages
            if self.resolutions_by_language.get(language, 0) > 0
        )


class LocalizationMetricsRecorder(Protocol):
    """What the localization surfaces require of a metrics backend."""

    def record_resolution(self, language: str, *, count: int = 1) -> None:
        """Record that something was rendered in ``language``."""
        ...

    def record_unsupported_locale(self, *, count: int = 1) -> None:
        """Record a request naming a language the platform does not serve."""
        ...

    def record_translation_failure(
        self, reason: TranslationFailureReason, *, catalogue: str | None = None
    ) -> None:
        """Record a catalogue a client could not use."""
        ...

    def record_missing_translations(self, keys: Iterable[str]) -> None:
        """Record keys that were absent from the catalogue that was in force."""
        ...

    def snapshot(self) -> LocalizationMetricsSnapshot:
        """Read the counters as one consistent value."""
        ...


class InMemoryLocalizationMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a snapshot has to be internally consistent:
    counters read without one can report more distinct missing keys than missing
    translations. The critical sections are a handful of additions and a bounded
    set insertion, on paths that are already doing network work.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._by_language: dict[str, int] = {}
        self._unsupported = 0
        self._failures = 0
        self._failures_by_reason: dict[str, int] = {}
        self._catalogues: set[str] = set()
        self._missing = 0
        self._missing_keys: set[str] = set()

    def record_resolution(self, language: str, *, count: int = 1) -> None:
        """Record that something was rendered in ``language``."""
        if count <= 0:
            return
        with self._lock:
            self._by_language[language] = self._by_language.get(language, 0) + count

    def record_unsupported_locale(self, *, count: int = 1) -> None:
        """Record a request naming a language the platform does not serve."""
        if count <= 0:
            return
        with self._lock:
            self._unsupported += count

    def record_translation_failure(
        self, reason: TranslationFailureReason, *, catalogue: str | None = None
    ) -> None:
        """Record a catalogue a client could not use."""
        with self._lock:
            self._failures += 1
            self._failures_by_reason[reason.value] = (
                self._failures_by_reason.get(reason.value, 0) + 1
            )
            if catalogue and len(self._catalogues) < MAX_TRACKED_CATALOGUES:
                self._catalogues.add(catalogue)

    def record_missing_translations(self, keys: Iterable[str]) -> None:
        """Record keys that were absent from the catalogue that was in force."""
        with self._lock:
            for key in keys:
                self._missing += 1
                if len(self._missing_keys) < MAX_TRACKED_KEYS:
                    self._missing_keys.add(key)

    def snapshot(self) -> LocalizationMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return LocalizationMetricsSnapshot(
                since=self._since,
                supported_languages=SUPPORTED_LANGUAGES,
                resolutions_by_language=dict(self._by_language),
                unsupported_locale_requests=self._unsupported,
                translation_failures=self._failures,
                failures_by_reason=dict(self._failures_by_reason),
                failing_catalogues=tuple(sorted(self._catalogues)),
                missing_translations=self._missing,
                distinct_missing_keys=len(self._missing_keys),
                missing_keys=tuple(sorted(self._missing_keys)),
            )

    def reset(self) -> None:
        """Discard every counter. For tests, and for an operator wanting a fresh window."""
        with self._lock:
            self._since = datetime.now(UTC)
            self._by_language.clear()
            self._unsupported = 0
            self._failures = 0
            self._failures_by_reason.clear()
            self._catalogues.clear()
            self._missing = 0
            self._missing_keys.clear()


class NullLocalizationMetrics:
    """A recorder that counts nothing.

    The default for a surface constructed without observability — a script, or a
    unit test that is not about metrics. Same role and reasoning as
    :class:`~services.settings_metrics.NullSettingsMetrics`.
    """

    def record_resolution(self, language: str, *, count: int = 1) -> None:
        """Discard the observation."""

    def record_unsupported_locale(self, *, count: int = 1) -> None:
        """Discard the observation."""

    def record_translation_failure(
        self, reason: TranslationFailureReason, *, catalogue: str | None = None
    ) -> None:
        """Discard the observation."""

    def record_missing_translations(self, keys: Iterable[str]) -> None:
        """Discard the observation."""

    def snapshot(self) -> LocalizationMetricsSnapshot:
        """Report an empty window."""
        return LocalizationMetricsSnapshot(
            since=datetime.now(UTC),
            supported_languages=SUPPORTED_LANGUAGES,
            resolutions_by_language={},
            unsupported_locale_requests=0,
            translation_failures=0,
            failures_by_reason={},
            failing_catalogues=(),
            missing_translations=0,
            distinct_missing_keys=0,
            missing_keys=(),
        )


def distribution_with_zeros(measured: Mapping[str, int]) -> dict[str, int]:
    """Fill in the languages nobody chose, as measured zeros.

    ``19-dashboard-analytics.md``'s rule about empty buckets, applied to the one
    breakdown this feature owns: a distribution that omitted Arabic until somebody
    switched to it would hide exactly the number a deployment deciding whether to
    invest in Arabic needs. Values for languages the platform no longer serves are
    kept rather than dropped — a stored preference naming one is a real row, and a
    figure that silently excluded it would make the columns not add up.
    """
    filled = {language: 0 for language in SUPPORTED_LANGUAGES}
    for language, count in measured.items():
        filled[language] = filled.get(language, 0) + count
    return filled


#: The one recorder the process shares.
#:
#: Module-level for the reason every other recorder here is: a counter rebuilt per
#: request counts to one — and here more strongly than most, because the reports
#: it receives arrive on requests that have nothing else to do with each other.
_shared = InMemoryLocalizationMetrics()


def get_localization_metrics() -> LocalizationMetricsRecorder:
    """Return the process-wide localization metrics recorder."""
    return _shared


def reset_localization_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "MAX_TRACKED_CATALOGUES",
    "MAX_TRACKED_KEYS",
    "InMemoryLocalizationMetrics",
    "LocalizationMetricsRecorder",
    "LocalizationMetricsSnapshot",
    "NullLocalizationMetrics",
    "TranslationFailureReason",
    "distribution_with_zeros",
    "get_localization_metrics",
    "reset_localization_metrics",
]
