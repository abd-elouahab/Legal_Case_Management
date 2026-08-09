"""Email delivery observability.

``17-email-delivery-channel.md``'s Monitoring section names five figures:
**queued emails, sent emails, failed emails, retry count, and average delivery
latency**. They come from two places, exactly as the notification metrics do, and
the split is the same one for the same reason:

* **queued, sent, and failed are properties of rows**, so they are SQL aggregates
  (:meth:`~repositories.email.EmailDeliveryRepository.statistics`). A
  process-local count of what is queued would reset on restart *and* be wrong
  across instances — and "how many emails are stuck?" is precisely the question
  an operator asks after a restart;
* **retries and latency are properties of what this process did**, so they
  accumulate here behind a protocol, with ``since`` reporting the window.

The shape is the one :mod:`services.search_metrics`,
:mod:`services.rag_metrics`, :mod:`services.assistant_metrics`,
:mod:`services.event_metrics`, and :mod:`services.notification_metrics` already
established — a protocol, an in-memory implementation, a null implementation, and
a frozen snapshot.

**Nothing identifying can be recorded here, by construction.** The recorder is
handed durations, counts, and short stable strings — a rule key, a failure code.
There is no parameter for a user, an address, a notification, a case, or a
rendered subject, so there is nowhere for one to go. That matters more on this
channel than on any other: a per-recipient counter would be a live index of whose
mailbox the platform is writing to, and an address is the one piece of personal
data this feature necessarily handles.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from core.email import EmailFailureCode


class EmailSkipReason(StrEnum):
    """Why a notification produced no email at all.

    Distinct from a **failure**, and the distinction is the point: none of these
    is a fault, and counting them as one would make a healthy deployment look
    broken. They are reported because together they answer the operator's real
    question — *"why did that person not get an email?"* — without anyone having
    to read the delivery table.
    """

    #: The notification's rule is not in :data:`~core.email.EMAIL_RULES`. By far
    #: the most common reason, and entirely correct: most of the platform's
    #: notifications are in-app only by design.
    NOT_EMAIL_ELIGIBLE = "not_email_eligible"
    #: The recipient has switched the email channel off for this kind of
    #: notification. The preference system doing its job.
    SUPPRESSED_BY_PREFERENCE = "suppressed_by_preference"
    #: The account has no usable address. Rare, and worth seeing.
    NO_ADDRESS = "no_address"
    #: A delivery for this notification already existed — a re-dispatch after a
    #: restart, or two workers racing. The duplicate guard doing its job.
    ALREADY_QUEUED = "already_queued"
    #: No provider is configured on this deployment, so nothing is queued at all.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: Email delivery is switched off here.
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class EmailMetricsSnapshot:
    """The counters at one instant, as the monitoring endpoint reports them.

    Frozen rather than a live view of the recorder, for the reason every other
    snapshot on this platform is: reading a dozen separately-updated counters
    while other threads increment them produces a report whose numbers contradict
    each other, and a monitoring page that does not add up is worse than one that
    is a second stale.
    """

    #: When this process started counting.
    since: datetime

    #: Deliveries this process queued.
    queued: int
    #: Deliveries this process handed to a provider successfully.
    sent: int
    #: Deliveries this process gave up on — a permanent failure, or the last
    #: transient attempt.
    failed: int
    #: Attempts that failed transiently and were rescheduled. **The spec's "retry
    #: count"**, and it is deliberately counted at the moment a retry is
    #: *scheduled* rather than when one runs: a delivery waiting out a backoff has
    #: already cost the platform an attempt, and an operator watching this climb
    #: while ``sent`` does not is watching a relay fail in real time.
    retried: int
    #: Notifications that produced no email, by reason. See :class:`EmailSkipReason`.
    skipped: int

    #: Mean time from the notification being created to the provider accepting
    #: the message. ``None`` rather than ``0`` when nothing has been sent: an
    #: average over no deliveries is undefined, while zero would read as
    #: "instantaneous", which is a very different claim.
    average_delivery_latency_ms: float | None
    #: Mean time inside the provider call alone, which is what an operator
    #: investigating a slow relay actually wants — the figure above includes the
    #: queue wait, and on a healthy deployment that is most of it.
    average_send_duration_ms: float | None

    sent_by_rule: dict[str, int]
    failures_by_code: dict[str, int]
    #: Why deliveries were rescheduled, by cause. Separate from
    #: ``failures_by_code`` on purpose: a wall of ``throttled`` retries that
    #: eventually succeed is a relay asking the platform to slow down, while the
    #: same code under ``failures_by_code`` is mail that never arrived.
    retries_by_code: dict[str, int]
    skipped_by_reason: dict[str, int]

    @property
    def delivery_success_rate(self) -> float:
        """Share of finished deliveries that were accepted, as a percentage.

        ``0.0`` when nothing has finished — there is nothing to have succeeded at
        yet. Same shape and reasoning as every other rate on this platform.
        """
        attempted = self.sent + self.failed
        if attempted <= 0:
            return 0.0
        return round(self.sent / attempted * 100, 2)


class EmailMetricsRecorder(Protocol):
    """What the email delivery service requires of a metrics backend."""

    def record_queued(self, count: int = 1) -> None:
        """Record deliveries written as ``pending``."""
        ...

    def record_sent(self, rule_key: str, *, latency_ms: float, duration_ms: float) -> None:
        """Record one message a provider accepted.

        Args:
            rule_key: which notification rule it carried. Counted **by rule**,
                which is safe where counting by recipient would not be:
                "eleven hearing changes were mailed" is a throughput figure, while
                "eleven emails to Amina" is a statement about a person's work.
            latency_ms: from the notification being created to the provider
                accepting it — what the platform is responsible for end to end.
            duration_ms: the provider call alone.
        """
        ...

    def record_failed(self, code: EmailFailureCode) -> None:
        """Record one delivery the platform gave up on."""
        ...

    def record_retry(self, code: EmailFailureCode) -> None:
        """Record one transient failure that was rescheduled."""
        ...

    def record_skipped(self, reason: EmailSkipReason, count: int = 1) -> None:
        """Record notifications that produced no email. Not a failure."""
        ...

    def snapshot(self) -> EmailMetricsSnapshot:
        """Read the counters as one consistent value."""
        ...


class InMemoryEmailMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a snapshot has to be internally
    consistent: counters read without one can report more sends than queues. The
    critical sections are a handful of additions on a path that is already doing a
    database write and a network round trip, so contention is not a consideration.

    Sums are accumulated rather than storing every observation, for the reason
    :class:`~services.search_metrics.InMemorySearchMetrics` records: a running
    total and a count give the same mean in constant memory, and a metrics
    recorder that grows with traffic is a leak with a chart on it.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._queued = 0
        self._sent = 0
        self._failed = 0
        self._retried = 0
        self._skipped = 0
        self._latency_sum = 0.0
        self._duration_sum = 0.0
        self._by_rule: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._retries: dict[str, int] = {}
        self._skips: dict[str, int] = {}

    def record_queued(self, count: int = 1) -> None:
        """Record deliveries written as ``pending``."""
        with self._lock:
            self._queued += max(0, count)

    def record_sent(self, rule_key: str, *, latency_ms: float, duration_ms: float) -> None:
        """Record one message a provider accepted."""
        with self._lock:
            self._sent += 1
            self._latency_sum += max(0.0, latency_ms)
            self._duration_sum += max(0.0, duration_ms)
            self._by_rule[rule_key] = self._by_rule.get(rule_key, 0) + 1

    def record_failed(self, code: EmailFailureCode) -> None:
        """Record one delivery the platform gave up on.

        The attempt's duration is deliberately **not** folded into the latency
        sum — the same reasoning every other recorder here gives for excluding
        failures: a message that failed because the relay was unreachable says
        nothing about how fast delivery is, and averaging it in would make the
        platform look slow when what it actually is, is broken.
        """
        with self._lock:
            self._failed += 1
            self._failures[code.value] = self._failures.get(code.value, 0) + 1

    def record_retry(self, code: EmailFailureCode) -> None:
        """Record one transient failure that was rescheduled."""
        with self._lock:
            self._retried += 1
            self._retries[code.value] = self._retries.get(code.value, 0) + 1

    def record_skipped(self, reason: EmailSkipReason, count: int = 1) -> None:
        """Record notifications that produced no email."""
        with self._lock:
            self._skipped += max(0, count)
            self._skips[reason.value] = self._skips.get(reason.value, 0) + max(0, count)

    def snapshot(self) -> EmailMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return EmailMetricsSnapshot(
                since=self._since,
                queued=self._queued,
                sent=self._sent,
                failed=self._failed,
                retried=self._retried,
                skipped=self._skipped,
                average_delivery_latency_ms=(
                    round(self._latency_sum / self._sent, 2) if self._sent > 0 else None
                ),
                average_send_duration_ms=(
                    round(self._duration_sum / self._sent, 2) if self._sent > 0 else None
                ),
                sent_by_rule=dict(self._by_rule),
                failures_by_code=dict(self._failures),
                retries_by_code=dict(self._retries),
                skipped_by_reason=dict(self._skips),
            )

    def reset(self) -> None:
        """Discard every counter.

        For tests, and for an operator who wants a fresh window. Not called by the
        application — the counters are the process's history, and clearing them on
        a schedule would make the ``since`` timestamp a lie.
        """
        with self._lock:
            self._since = datetime.now(UTC)
            self._queued = 0
            self._sent = 0
            self._failed = 0
            self._retried = 0
            self._skipped = 0
            self._latency_sum = 0.0
            self._duration_sum = 0.0
            self._by_rule.clear()
            self._failures.clear()
            self._retries.clear()
            self._skips.clear()


class NullEmailMetrics:
    """A recorder that counts nothing.

    The default for a service constructed without observability — a script, or a
    unit test that is not about metrics. Same role and reasoning as
    :class:`~services.notification_metrics.NullNotificationMetrics`.
    """

    def record_queued(self, count: int = 1) -> None:
        """Discard the observation."""

    def record_sent(self, rule_key: str, *, latency_ms: float, duration_ms: float) -> None:
        """Discard the observation."""

    def record_failed(self, code: EmailFailureCode) -> None:
        """Discard the observation."""

    def record_retry(self, code: EmailFailureCode) -> None:
        """Discard the observation."""

    def record_skipped(self, reason: EmailSkipReason, count: int = 1) -> None:
        """Discard the observation."""

    def snapshot(self) -> EmailMetricsSnapshot:
        """Report an empty window."""
        return EmailMetricsSnapshot(
            since=datetime.now(UTC),
            queued=0,
            sent=0,
            failed=0,
            retried=0,
            skipped=0,
            average_delivery_latency_ms=None,
            average_send_duration_ms=None,
            sent_by_rule={},
            failures_by_code={},
            retries_by_code={},
            skipped_by_reason={},
        )


#: The one recorder the process shares.
#:
#: Module-level for the reason every other recorder here is: a counter rebuilt per
#: request counts to one. It matters more here than for most, because **every**
#: email is queued and sent on a background thread with no request to hang a
#: per-request instance off at all — so the recorder the metrics endpoint reads
#: must be the same object the workers write to.
_shared = InMemoryEmailMetrics()


def get_email_metrics() -> EmailMetricsRecorder:
    """Return the process-wide email metrics recorder."""
    return _shared


def reset_email_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "EmailMetricsRecorder",
    "EmailMetricsSnapshot",
    "EmailSkipReason",
    "InMemoryEmailMetrics",
    "NullEmailMetrics",
    "get_email_metrics",
    "reset_email_metrics",
]
