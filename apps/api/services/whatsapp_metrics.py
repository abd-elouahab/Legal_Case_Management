"""WhatsApp delivery observability.

``18-whatsapp-delivery-channel.md``'s Monitoring section names six figures:
**queued messages, delivered messages, failed deliveries, retry count, average
delivery latency, and provider response time**. They come from two places,
exactly as the email and notification metrics do, and the split is the same one
for the same reason:

* **queued, delivered, and failed are properties of rows**, so they are SQL
  aggregates (:meth:`~repositories.whatsapp.WhatsAppDeliveryRepository.statistics`).
  A process-local count of what is queued would reset on restart *and* be wrong
  across instances — and "how many messages are stuck?" is precisely the question
  an operator asks after a restart;
* **retries, latency, and provider response time are properties of what this
  process did**, so they accumulate here behind a protocol, with ``since``
  reporting the window.

**Average delivery latency and provider response time are the last two figures,
and they are deliberately separate.** The first is what the platform is
responsible for end to end — from the notification being created to the provider
accepting the message — and on a healthy deployment it is mostly queue wait. The
second is the Cloud API call alone, which is what an operator investigating "is
WhatsApp slow, or are we?" actually needs. Reporting one number would answer
neither question.

The shape is the one :mod:`services.search_metrics`, :mod:`services.rag_metrics`,
:mod:`services.assistant_metrics`, :mod:`services.event_metrics`,
:mod:`services.notification_metrics`, and :mod:`services.email_metrics` already
established — a protocol, an in-memory implementation, a null implementation, and
a frozen snapshot.

**Nothing identifying can be recorded here, by construction.** The recorder is
handed durations, counts, and short stable strings — a rule key, a failure code.
There is no parameter for a user, a phone number, a notification, a case, or a
rendered message, so there is nowhere for one to go. That matters more on this
channel than on any other: a per-recipient counter would be a live index of whose
phone the platform is messaging, and a phone number is more identifying than an
email address — it is a device somebody carries.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from core.whatsapp import WhatsAppFailureCode


class WhatsAppSkipReason(StrEnum):
    """Why a notification produced no WhatsApp message at all.

    Distinct from a **failure**, and the distinction is the point: none of these
    is a fault, and counting them as one would make a healthy deployment look
    broken. They are reported because together they answer the operator's real
    question — *"why did that person not get a message?"* — without anyone having
    to read the delivery table.
    """

    #: The notification's rule is not in :data:`~core.whatsapp.WHATSAPP_RULES`. By
    #: far the most common reason, and entirely correct: most of the platform's
    #: notifications are in-app only by design, and this channel carries fewer
    #: kinds than any other.
    NOT_WHATSAPP_ELIGIBLE = "not_whatsapp_eligible"
    #: The recipient has switched the WhatsApp channel off for this kind of
    #: notification. The preference system doing its job.
    SUPPRESSED_BY_PREFERENCE = "suppressed_by_preference"
    #: The account has no usable phone number — none stored, or one that is not a
    #: number this platform is willing to send to. **Expected rather than rare on
    #: this channel**, unlike its email equivalent: ``users.phone`` is optional,
    #: so an account created without one is skipped here forever and that is
    #: correct. A deployment watching this figure sit at the size of its user base
    #: is being told to collect phone numbers, not that something is broken.
    NO_PHONE_NUMBER = "no_phone_number"
    #: A delivery for this notification already existed — a re-dispatch after a
    #: restart, or two workers racing. The duplicate guard doing its job.
    ALREADY_QUEUED = "already_queued"
    #: No provider is configured on this deployment, so nothing is queued at all.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: WhatsApp delivery is switched off here.
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class WhatsAppMetricsSnapshot:
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
    delivered: int
    #: Deliveries this process gave up on — a permanent failure, or the last
    #: transient attempt.
    failed: int
    #: Attempts that failed transiently and were rescheduled. **The spec's "retry
    #: count"**, counted at the moment a retry is *scheduled* rather than when one
    #: runs: a delivery waiting out a backoff has already cost the platform an
    #: attempt, and an operator watching this climb while ``delivered`` does not is
    #: watching a rate limit bite in real time.
    retried: int
    #: Notifications that produced no message, by reason. See
    #: :class:`WhatsAppSkipReason`.
    skipped: int

    #: Mean time from the notification being created to the provider accepting
    #: the message. ``None`` rather than ``0`` when nothing has been delivered: an
    #: average over no deliveries is undefined, while zero would read as
    #: "instantaneous", which is a very different claim.
    average_delivery_latency_ms: float | None
    #: Mean time inside the provider call alone — **the spec's "provider response
    #: time"**. The figure above includes the queue wait, and on a healthy
    #: deployment that is most of it, so the two answer different questions.
    average_provider_response_ms: float | None

    delivered_by_rule: dict[str, int]
    failures_by_code: dict[str, int]
    #: Why deliveries were rescheduled, by cause. Separate from
    #: ``failures_by_code`` on purpose: a wall of ``throttled`` retries that
    #: eventually succeed is WhatsApp asking the platform to slow down, while the
    #: same code under ``failures_by_code`` is a message that never arrived.
    retries_by_code: dict[str, int]
    skipped_by_reason: dict[str, int]

    @property
    def delivery_success_rate(self) -> float:
        """Share of finished deliveries that were accepted, as a percentage.

        ``0.0`` when nothing has finished — there is nothing to have succeeded at
        yet. Same shape and reasoning as every other rate on this platform.
        """
        attempted = self.delivered + self.failed
        if attempted <= 0:
            return 0.0
        return round(self.delivered / attempted * 100, 2)


class WhatsAppMetricsRecorder(Protocol):
    """What the WhatsApp delivery service requires of a metrics backend."""

    def record_queued(self, count: int = 1) -> None:
        """Record deliveries written as ``pending``."""
        ...

    def record_delivered(
        self, rule_key: str, *, latency_ms: float, duration_ms: float
    ) -> None:
        """Record one message a provider accepted.

        Args:
            rule_key: which notification rule it carried. Counted **by rule**,
                which is safe where counting by recipient would not be: "eleven
                hearing changes were messaged" is a throughput figure, while
                "eleven messages to Amina" is a statement about a person's work.
            latency_ms: from the notification being created to the provider
                accepting it — what the platform is responsible for end to end.
            duration_ms: the provider call alone.
        """
        ...

    def record_failed(self, code: WhatsAppFailureCode) -> None:
        """Record one delivery the platform gave up on."""
        ...

    def record_retry(self, code: WhatsAppFailureCode) -> None:
        """Record one transient failure that was rescheduled."""
        ...

    def record_skipped(self, reason: WhatsAppSkipReason, count: int = 1) -> None:
        """Record notifications that produced no message. Not a failure."""
        ...

    def snapshot(self) -> WhatsAppMetricsSnapshot:
        """Read the counters as one consistent value."""
        ...


class InMemoryWhatsAppMetrics:
    """Process-local counters, guarded by a lock.

    A lock rather than atomics because a snapshot has to be internally consistent:
    counters read without one can report more deliveries than queues. The critical
    sections are a handful of additions on a path that is already doing a database
    write and a network round trip, so contention is not a consideration.

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
        self._delivered = 0
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

    def record_delivered(
        self, rule_key: str, *, latency_ms: float, duration_ms: float
    ) -> None:
        """Record one message a provider accepted."""
        with self._lock:
            self._delivered += 1
            self._latency_sum += max(0.0, latency_ms)
            self._duration_sum += max(0.0, duration_ms)
            self._by_rule[rule_key] = self._by_rule.get(rule_key, 0) + 1

    def record_failed(self, code: WhatsAppFailureCode) -> None:
        """Record one delivery the platform gave up on.

        The attempt's duration is deliberately **not** folded into the latency
        sum — the same reasoning every other recorder here gives for excluding
        failures: a message that failed because the API was unreachable says
        nothing about how fast delivery is, and averaging it in would make the
        platform look slow when what it actually is, is broken.
        """
        with self._lock:
            self._failed += 1
            self._failures[code.value] = self._failures.get(code.value, 0) + 1

    def record_retry(self, code: WhatsAppFailureCode) -> None:
        """Record one transient failure that was rescheduled."""
        with self._lock:
            self._retried += 1
            self._retries[code.value] = self._retries.get(code.value, 0) + 1

    def record_skipped(self, reason: WhatsAppSkipReason, count: int = 1) -> None:
        """Record notifications that produced no message."""
        with self._lock:
            self._skipped += max(0, count)
            self._skips[reason.value] = self._skips.get(reason.value, 0) + max(0, count)

    def snapshot(self) -> WhatsAppMetricsSnapshot:
        """Read the counters as one consistent value."""
        with self._lock:
            return WhatsAppMetricsSnapshot(
                since=self._since,
                queued=self._queued,
                delivered=self._delivered,
                failed=self._failed,
                retried=self._retried,
                skipped=self._skipped,
                average_delivery_latency_ms=(
                    round(self._latency_sum / self._delivered, 2)
                    if self._delivered > 0
                    else None
                ),
                average_provider_response_ms=(
                    round(self._duration_sum / self._delivered, 2)
                    if self._delivered > 0
                    else None
                ),
                delivered_by_rule=dict(self._by_rule),
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
            self._delivered = 0
            self._failed = 0
            self._retried = 0
            self._skipped = 0
            self._latency_sum = 0.0
            self._duration_sum = 0.0
            self._by_rule.clear()
            self._failures.clear()
            self._retries.clear()
            self._skips.clear()


class NullWhatsAppMetrics:
    """A recorder that counts nothing.

    The default for a service constructed without observability — a script, or a
    unit test that is not about metrics. Same role and reasoning as
    :class:`~services.email_metrics.NullEmailMetrics`.
    """

    def record_queued(self, count: int = 1) -> None:
        """Discard the observation."""

    def record_delivered(
        self, rule_key: str, *, latency_ms: float, duration_ms: float
    ) -> None:
        """Discard the observation."""

    def record_failed(self, code: WhatsAppFailureCode) -> None:
        """Discard the observation."""

    def record_retry(self, code: WhatsAppFailureCode) -> None:
        """Discard the observation."""

    def record_skipped(self, reason: WhatsAppSkipReason, count: int = 1) -> None:
        """Discard the observation."""

    def snapshot(self) -> WhatsAppMetricsSnapshot:
        """Report an empty window."""
        return WhatsAppMetricsSnapshot(
            since=datetime.now(UTC),
            queued=0,
            delivered=0,
            failed=0,
            retried=0,
            skipped=0,
            average_delivery_latency_ms=None,
            average_provider_response_ms=None,
            delivered_by_rule={},
            failures_by_code={},
            retries_by_code={},
            skipped_by_reason={},
        )


#: The one recorder the process shares.
#:
#: Module-level for the reason every other recorder here is: a counter rebuilt per
#: request counts to one. It matters more here than for most, because **every**
#: message is queued and sent on a background thread with no request to hang a
#: per-request instance off at all — so the recorder the metrics endpoint reads
#: must be the same object the workers write to.
_shared = InMemoryWhatsAppMetrics()


def get_whatsapp_metrics() -> WhatsAppMetricsRecorder:
    """Return the process-wide WhatsApp metrics recorder."""
    return _shared


def reset_whatsapp_metrics() -> None:
    """Clear the process-wide counters. For tests."""
    _shared.reset()


__all__ = [
    "InMemoryWhatsAppMetrics",
    "NullWhatsAppMetrics",
    "WhatsAppMetricsRecorder",
    "WhatsAppMetricsSnapshot",
    "WhatsAppSkipReason",
    "get_whatsapp_metrics",
    "reset_whatsapp_metrics",
]
