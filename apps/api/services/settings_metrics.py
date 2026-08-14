"""Settings observability.

``20-settings.md``'s Monitoring section names four figures: **settings updated,
failed updates, profile changes, and password changes**. All four are here, in
the shape :mod:`services.search_metrics`, :mod:`services.rag_metrics`,
:mod:`services.notification_metrics`, and :mod:`services.dashboard_metrics`
established — a protocol, an in-memory implementation, a null implementation, and
a frozen snapshot.

**The figures come from two places, and the endpoint says which**, exactly as
Notifications' and Email's do. *How many people have customised anything* is a
property of stored rows and is a SQL aggregate
(:meth:`~repositories.settings.SettingsRepository.statistics`), because a number
counted in one API instance's memory would reset on deploy and be wrong across
replicas. *How often settings are changed, and how often a change is rejected*
cannot be aggregates — nothing records an update as a row — so they accumulate
here and carry ``since``.

**Counted by section, never by key, and never by person.** A breakdown showing
*"appearance: 41 changes"* is a throughput figure an operator can act on;
*"theme: dark, 41"* is a statement about what people chose, and a breakdown keyed
by recipient would be a live index of who is configuring what. This is the same
line :mod:`services.notification_metrics` draws when it counts by rule rather
than by reader, and it is why this module never sees a value.

**Password changes are counted here rather than in the auth module**, and that is
worth stating because it is the one figure this feature does not own the workflow
for. The spec asks the Settings module's monitoring to report it; the change
itself is :meth:`~services.auth.AuthService.change_password`'s, unchanged and
uninstrumented. The Settings API records the outcome of the call it made, which
means the number is *"password changes made through Settings"* rather than
*"password changes"* — a distinction that matters the day an administrator reset
is expected to appear here and does not.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from core.settings import SettingsSection


class SettingsFailureReason(StrEnum):
    """Why an attempted change did not take effect.

    A closed vocabulary rather than free text, for the reason every other
    ``*_metrics`` module here uses one: these are grouped in a monitoring view,
    and a message interpolated from an exception produces a breakdown with one
    bucket per occurrence.
    """

    #: The request named a setting the platform does not define.
    UNKNOWN_SETTING = "unknown_setting"
    #: A value was of the wrong type, outside the permitted set, or too long.
    INVALID_VALUE = "invalid_value"
    #: A password change was refused because the current password did not match.
    #: Counted separately from :attr:`INVALID_VALUE` because it is the one failure
    #: here that is a **security event** rather than a form error, and an operator
    #: watching this rise across many accounts is watching something different
    #: from somebody mistyping a time zone.
    BAD_CURRENT_PASSWORD = "bad_current_password"
    #: The write itself failed — a constraint, a lost connection, two tabs racing.
    #: The spec's "persistence failures".
    PERSISTENCE_FAILED = "persistence_failed"
    #: Anything unanticipated. Always worth investigating.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SettingsMetricsSnapshot:
    """The counters at one instant, as the monitoring endpoint reports them.

    Frozen rather than a live view of the recorder, for the reason every other
    snapshot here is: reading separately-updated counters while other threads
    increment them produces a report whose numbers contradict each other.
    """

    #: When this process started counting.
    since: datetime

    #: Settings actually changed — one per key that took a new value, not one per
    #: request. A form saving six fields of which two changed counts **two**,
    #: which is what makes this comparable with the stored row counts beside it.
    updated: int
    #: Attempted changes that did not take effect. The spec's "failed updates".
    failed: int
    #: Profile fields changed, counted the same way. Its own figure because the
    #: spec names it, and because a profile write goes to a different table.
    profile_changes: int
    #: Passwords changed through the Settings API — see the module docstring for
    #: why that qualification is in the name of the thing rather than only here.
    password_changes: int
    #: Sessions ended by *"log out other sessions"*. Not in the spec's list and
    #: kept anyway: it is the one control on the page that cannot be undone, and a
    #: deployment seeing it rise is seeing people respond to something.
    session_revocations: int

    #: Changes per section. What turns "settings are being changed" into "the
    #: notification section is being changed", which is the only form of that
    #: sentence a product decision can be made from.
    updated_by_section: dict[str, int]
    #: Failures per cause.
    failures_by_reason: dict[str, int]

    @property
    def success_rate(self) -> float:
        """Share of attempted changes that took effect, as a percentage.

        ``0.0`` when nothing has been attempted — there is nothing to have
        succeeded at yet. Same shape and reasoning as every other rate here.
        """
        attempted = self.updated + self.failed
        if attempted <= 0:
            return 0.0
        return round(self.updated / attempted * 100, 2)


class SettingsMetricsRecorder(Protocol):
    """What the settings service requires of a metrics backend."""

    def record_update(self, section: SettingsSection, *, count: int = 1) -> None:
        """Record settings that took a new value in one section."""
        ...

    def record_failure(self, reason: SettingsFailureReason) -> None:
        """Record one attempted change that did not take effect."""
        ...

    def record_profile_change(self, *, fields: int) -> None:
        """Record a profile update, and how many fields it actually changed."""
        ...

    def record_password_change(self) -> None:
        """Record one password changed through the Settings API."""
        ...

    def record_session_revocation(self) -> None:
        """Record one "log out other sessions"."""
        ...

    def snapshot(self) -> SettingsMetricsSnapshot:
        """Read the counters as one consistent value."""
        ...


class InMemorySettingsMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a snapshot has to be internally consistent:
    counters read without one can report more section changes than total updates.
    The critical sections are a handful of additions on a path that has already
    written to the database, so contention is not a consideration.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._updated = 0
        self._failed = 0
        self._profile_changes = 0
        self._password_changes = 0
        self._session_revocations = 0
        self._by_section: dict[str, int] = {}
        self._failures_by_reason: dict[str, int] = {}

    def record_update(self, section: SettingsSection, *, count: int = 1) -> None:
        """Record settings that took a new value in one section."""
        if count <= 0:
            # A save in which nothing actually changed is not an update. Counting
            # it would make the figure measure *page visits* rather than changes,
            # and the repository has already declined to issue a statement for it.
            return
        with self._lock:
            self._updated += count
            self._by_section[section.value] = self._by_section.get(section.value, 0) + count

    def record_failure(self, reason: SettingsFailureReason) -> None:
        """Record one attempted change that did not take effect."""
        with self._lock:
            self._failed += 1
            self._failures_by_reason[reason.value] = (
                self._failures_by_reason.get(reason.value, 0) + 1
            )

    def record_profile_change(self, *, fields: int) -> None:
        """Record a profile update, and how many fields it actually changed."""
        if fields <= 0:
            return
        with self._lock:
            self._profile_changes += fields
            section = SettingsSection.PROFILE.value
            self._by_section[section] = self._by_section.get(section, 0) + fields

    def record_password_change(self) -> None:
        """Record one password changed through the Settings API."""
        with self._lock:
            self._password_changes += 1

    def record_session_revocation(self) -> None:
        """Record one "log out other sessions"."""
        with self._lock:
            self._session_revocations += 1

    def snapshot(self) -> SettingsMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return SettingsMetricsSnapshot(
                since=self._since,
                updated=self._updated,
                failed=self._failed,
                profile_changes=self._profile_changes,
                password_changes=self._password_changes,
                session_revocations=self._session_revocations,
                updated_by_section=dict(self._by_section),
                failures_by_reason=dict(self._failures_by_reason),
            )

    def reset(self) -> None:
        """Discard every counter. For tests, and for an operator wanting a fresh window."""
        with self._lock:
            self._since = datetime.now(UTC)
            self._updated = 0
            self._failed = 0
            self._profile_changes = 0
            self._password_changes = 0
            self._session_revocations = 0
            self._by_section.clear()
            self._failures_by_reason.clear()


class NullSettingsMetrics:
    """A recorder that counts nothing.

    The default for a service constructed without observability — a script, or a
    unit test that is not about metrics. Same role and reasoning as
    :class:`~services.dashboard_metrics.NullDashboardMetrics`.
    """

    def record_update(self, section: SettingsSection, *, count: int = 1) -> None:
        """Discard the observation."""

    def record_failure(self, reason: SettingsFailureReason) -> None:
        """Discard the observation."""

    def record_profile_change(self, *, fields: int) -> None:
        """Discard the observation."""

    def record_password_change(self) -> None:
        """Discard the observation."""

    def record_session_revocation(self) -> None:
        """Discard the observation."""

    def snapshot(self) -> SettingsMetricsSnapshot:
        """Report an empty window."""
        return SettingsMetricsSnapshot(
            since=datetime.now(UTC),
            updated=0,
            failed=0,
            profile_changes=0,
            password_changes=0,
            session_revocations=0,
            updated_by_section={},
            failures_by_reason={},
        )


#: The one recorder the process shares.
#:
#: Module-level for the reason every other recorder here is: a counter rebuilt
#: per request counts to one.
_shared = InMemorySettingsMetrics()


def get_settings_metrics() -> SettingsMetricsRecorder:
    """Return the process-wide settings metrics recorder."""
    return _shared


def reset_settings_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "InMemorySettingsMetrics",
    "NullSettingsMetrics",
    "SettingsFailureReason",
    "SettingsMetricsRecorder",
    "SettingsMetricsSnapshot",
    "get_settings_metrics",
    "reset_settings_metrics",
]
