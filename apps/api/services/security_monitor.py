"""Security monitoring: failed sign-ins, denials, invalid tokens, and rate limits.

``22-monitoring.md``'s Security Monitoring section names five things to watch —
failed logins, repeated authorization failures, suspicious authentication
activity, excessive API requests, and invalid tokens — and then constrains the
whole feature in one sentence: *"monitoring should assist administrators without
exposing sensitive information."*

That sentence is why this module looks the way it does.

**No account is ever named, and no address is ever stored.** A per-account
failure feed would be a live index of who is being attacked, and — worse — of who
mistypes their password, which is a small profile of a person assembled by
accident. An IP address is personal data in its own right. So the *identity* of a
source is folded into a **salted digest** whose salt is random per process and
never leaves it, and the only thing readable from the result is its
**cardinality**: *"forty-one failures from three sources"* is the sentence an
operator needs, and *"forty-one failures from 203.0.113.7"* is one they do not.
That is the mechanism :mod:`services.dashboard_metrics` introduced for counting
active users, applied where the privacy argument is stronger.

**Rates, not just totals.** A total says a platform has had two thousand failed
sign-ins since it started, which is unreadable; what distinguishes an attack from
a year of Mondays is *how many in the last few minutes*. So every event is
counted into a ring of per-minute buckets, which is bounded, cheap, and gives
:data:`~core.observability.ALERT_RULES` something real to evaluate.

**Nothing here decides anything.** The platform's actual defences —
:class:`~services.login_throttle.LoginThrottle`, the token denylist, the
permission checks — are elsewhere and are unaffected by whether this module is
recording. If it were switched off entirely, every request would be authorized
exactly as it is now; ``22-monitoring.md``'s *"monitoring must never become a
dependency"* is not an aspiration here, it is a consequence of this module having
no callers that read from it.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from core.observability import (
    MetricName,
    SecurityEventType,
    SecuritySeverity,
    security_severity,
    truncate,
)
from core.tracing import current_trace_context
from services.metrics_registry import MetricsRegistry, NullMetricsRegistry

__all__ = [
    "InMemorySecurityMonitor",
    "NullSecurityMonitor",
    "SecurityEventRecord",
    "SecurityMonitor",
    "SecuritySnapshot",
    "get_security_monitor",
    "reset_security_monitor",
]

#: Minutes of per-minute buckets kept. An hour is long enough to see a burst
#: develop and short enough that the ring is sixty integers per event type.
_WINDOW_MINUTES: Final[int] = 60
#: Recent events retained for the feed.
_DEFAULT_RECENT: Final[int] = 100
#: Distinct source digests remembered. A ceiling, because a distributed attempt
#: would otherwise make this set grow with the attack — the number is reported as
#: "at least" once the ceiling is reached, which is honest and bounded.
_MAX_SOURCES: Final[int] = 4_096
#: Bytes of digest kept per source. Sixteen is far past the collision floor for a
#: set this size, and the digest is unusable outside this process anyway.
_DIGEST_BYTES: Final[int] = 16
#: Characters of a digest shown in the recent feed. Enough to tell two sources
#: apart while being far too little to attack the salt with.
_DIGEST_PREFIX: Final[int] = 8


@dataclass(frozen=True, slots=True)
class SecurityEventRecord:
    """One security event, as the monitoring endpoint reports it.

    Note the fields that are absent: no user identifier, no email, no IP address,
    no token, no path with an identifier in it. What is here is what an operator
    can act on — *what* happened, *how bad* it is, *which role* it happened to,
    and *whether it was the same source as the one above*.
    """

    occurred_at: datetime
    event: SecurityEventType
    severity: SecuritySeverity
    #: The role of the account involved, when there was an authenticated one. A
    #: three-value vocabulary, so it identifies nobody.
    role: str | None = None
    #: A short, machine-readable reason — ``invalid_credentials``,
    #: ``account_disabled``. Never a sentence and never anything a user typed.
    reason: str | None = None
    #: The first characters of the salted digest of the source. Correlates two
    #: events without naming anyone, and is meaningless outside this process.
    source: str | None = None
    #: The trace this happened in, when there was one — the handle that leads to
    #: the request in :mod:`services.tracer`'s buffer.
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class SecuritySnapshot:
    """The security picture at one instant."""

    since: datetime
    total_events: int
    events_by_type: dict[str, int]
    events_by_severity: dict[str, int]
    #: Events in the last minute, five minutes, and fifteen minutes, by type.
    #: Three windows because they answer different questions: what is happening
    #: now, whether it is sustained, and whether it is worth waking somebody.
    recent_rates: dict[str, dict[str, int]]
    #: Distinct sources seen, and whether that count hit its ceiling.
    distinct_sources: int
    sources_capped: bool
    #: Most recent first.
    recent: tuple[SecurityEventRecord, ...]

    @property
    def failed_logins(self) -> int:
        """Sign-ins refused since this process started."""
        return self.events_by_type.get(SecurityEventType.LOGIN_FAILED.value, 0)

    @property
    def login_attempts(self) -> int:
        """Sign-ins attempted — successful, failed, and locked out.

        The denominator without which :attr:`failed_logins` cannot be read.
        """
        return (
            self.events_by_type.get(SecurityEventType.LOGIN_SUCCEEDED.value, 0)
            + self.events_by_type.get(SecurityEventType.LOGIN_FAILED.value, 0)
            + self.events_by_type.get(SecurityEventType.LOGIN_LOCKED_OUT.value, 0)
        )

    @property
    def login_failure_rate(self) -> float:
        """Share of sign-in attempts that failed, as a percentage.

        ``0.0`` when nothing has been attempted — there is nothing to have failed
        yet, and the alternative reading of an undefined ratio is a page that
        opens at 100 % on a quiet morning.
        """
        attempts = self.login_attempts
        if attempts <= 0:
            return 0.0
        return round(self.failed_logins / attempts * 100, 2)


class SecurityMonitor(Protocol):
    """What the exception handlers require of a security recorder.

    One recording method, and it is worth reading for what it **cannot** be
    handed: there is no parameter for a user, an email, a password, a token, or a
    request body. ``source`` is the one identifying value it takes, and it is
    consumed into a digest rather than stored — see
    :meth:`InMemorySecurityMonitor.record`.
    """

    def record(
        self,
        event: SecurityEventType,
        *,
        severity: SecuritySeverity | None = None,
        role: str | None = None,
        reason: str | None = None,
        source: str | None = None,
    ) -> None:
        """Record one security-relevant event."""
        ...

    def snapshot(self) -> SecuritySnapshot:
        """Read the counters, the rates, and the recent feed."""
        ...


class InMemorySecurityMonitor:
    """Process-local security counters, guarded by a lock.

    **Never raises**, for the reason :class:`~services.error_tracker.
    InMemoryErrorTracker` does not: it is called from exception handlers, and a
    monitoring failure there would replace a clean 401 with a 500.
    """

    #: The identifier recorded for this backend.
    name = "in-memory"

    def __init__(
        self,
        *,
        metrics: MetricsRegistry | None = None,
        recent_size: int = _DEFAULT_RECENT,
    ) -> None:
        self._lock = threading.Lock()
        self._since = datetime.now(UTC)
        self._metrics = metrics or NullMetricsRegistry()
        # Random per process and never exported, so two API instances produce
        # different digests for the same source — which is a feature: the digests
        # are useless as a cross-instance identifier, which is exactly what they
        # must never become.
        self._salt = secrets.token_bytes(16)
        self._recent: deque[SecurityEventRecord] = deque(maxlen=max(1, recent_size))
        self._by_type: dict[str, int] = {}
        self._by_severity: dict[str, int] = {}
        self._buckets: dict[str, deque[tuple[int, int]]] = {}
        self._sources: set[bytes] = set()
        self._sources_capped = False
        self._total = 0

    def record(
        self,
        event: SecurityEventType,
        *,
        severity: SecuritySeverity | None = None,
        role: str | None = None,
        reason: str | None = None,
        source: str | None = None,
    ) -> None:
        """Count one event, and remember it in the feed.

        ``source`` — a client address, when the platform is configured to trust
        one — is folded into a salted digest **inside the lock and immediately
        discarded**. The parameter exists so the platform can answer *"how many
        distinct sources?"*; there is deliberately no path by which the value
        itself reaches a field, a log line, or a response.
        """
        try:
            resolved = severity or security_severity(event)
            digest = self._digest(source) if source else None
            now = datetime.now(UTC)
            minute = int(time.time() // 60)
            trace = current_trace_context()

            with self._lock:
                self._total += 1
                self._by_type[event.value] = self._by_type.get(event.value, 0) + 1
                self._by_severity[resolved.value] = self._by_severity.get(resolved.value, 0) + 1
                self._bump(event.value, minute)

                if digest is not None:
                    if len(self._sources) < _MAX_SOURCES:
                        self._sources.add(digest)
                    else:
                        self._sources_capped = True

                self._recent.appendleft(
                    SecurityEventRecord(
                        occurred_at=now,
                        event=event,
                        severity=resolved,
                        role=truncate(role, 40) if role else None,
                        reason=truncate(reason, 60) if reason else None,
                        source=digest.hex()[:_DIGEST_PREFIX] if digest else None,
                        trace_id=trace.trace_id if trace else None,
                    )
                )

            self._metrics.increment(
                MetricName.SECURITY_EVENTS_TOTAL,
                labels={"event": event.value, "severity": resolved.value},
            )
            self._record_derived(event, reason=reason, role=role)
        except Exception:  # pragma: no cover - defensive; see the class docstring
            return

    def _record_derived(
        self, event: SecurityEventType, *, reason: str | None, role: str | None
    ) -> None:
        """Mirror a few events onto the dedicated counters an exporter expects.

        ``security_events_total`` already carries everything, so these are
        *derived* rather than a second source of truth — and they exist because
        ``auth_login_failures_total`` is the series a stock alerting rule and a
        stock Grafana panel look for. Deriving them here rather than at the call
        site is what keeps the caller from having to know which metrics exist.
        """
        if event in {
            SecurityEventType.LOGIN_SUCCEEDED,
            SecurityEventType.LOGIN_FAILED,
            SecurityEventType.LOGIN_LOCKED_OUT,
        }:
            self._metrics.increment(MetricName.AUTH_LOGIN_ATTEMPTS_TOTAL)
        if event in {SecurityEventType.LOGIN_FAILED, SecurityEventType.LOGIN_LOCKED_OUT}:
            self._metrics.increment(
                MetricName.AUTH_LOGIN_FAILURES_TOTAL,
                labels={"reason": truncate(reason, 60) if reason else "unknown"},
            )
        if event in {
            SecurityEventType.PERMISSION_DENIED,
            SecurityEventType.RESOURCE_ACCESS_DENIED,
        }:
            self._metrics.increment(
                MetricName.AUTHORIZATION_DENIALS_TOTAL,
                labels={"role": truncate(role, 40) if role else "unknown"},
            )

    def _digest(self, source: str) -> bytes:
        """Fold a source identifier into a keyed digest.

        Keyed rather than plain, deliberately: an unsalted hash of an IPv4
        address is reversible by anybody with four billion guesses and an
        afternoon, which is to say it is not a redaction at all.
        """
        return hashlib.blake2b(
            source.encode("utf-8", errors="ignore"), key=self._salt, digest_size=_DIGEST_BYTES
        ).digest()

    def _bump(self, event: str, minute: int) -> None:
        """Add one to this minute's bucket for ``event``. Lock held."""
        buckets = self._buckets.get(event)
        if buckets is None:
            buckets = deque(maxlen=_WINDOW_MINUTES)
            self._buckets[event] = buckets
        if buckets and buckets[-1][0] == minute:
            last_minute, count = buckets[-1]
            buckets[-1] = (last_minute, count + 1)
        else:
            buckets.append((minute, 1))

    def _rate(self, event: str, minutes: int, now_minute: int) -> int:
        """Events of one type in the last ``minutes``. Lock held."""
        buckets = self._buckets.get(event)
        if not buckets:
            return 0
        floor = now_minute - minutes + 1
        return sum(count for minute, count in buckets if minute >= floor)

    def snapshot(self) -> SecuritySnapshot:
        """Read the counters, the three windows, and the feed."""
        now_minute = int(time.time() // 60)
        with self._lock:
            rates = {
                event: {
                    "1m": self._rate(event, 1, now_minute),
                    "5m": self._rate(event, 5, now_minute),
                    "15m": self._rate(event, 15, now_minute),
                }
                for event in self._buckets
            }
            return SecuritySnapshot(
                since=self._since,
                total_events=self._total,
                events_by_type=dict(self._by_type),
                events_by_severity=dict(self._by_severity),
                recent_rates=rates,
                distinct_sources=len(self._sources),
                sources_capped=self._sources_capped,
                recent=tuple(self._recent),
            )

    def reset(self) -> None:
        """Discard every counter, and re-salt. For tests."""
        with self._lock:
            self._since = datetime.now(UTC)
            self._salt = secrets.token_bytes(16)
            self._recent.clear()
            self._by_type.clear()
            self._by_severity.clear()
            self._buckets.clear()
            self._sources.clear()
            self._sources_capped = False
            self._total = 0


class NullSecurityMonitor:
    """A monitor that records nothing."""

    def record(
        self,
        event: SecurityEventType,
        *,
        severity: SecuritySeverity | None = None,
        role: str | None = None,
        reason: str | None = None,
        source: str | None = None,
    ) -> None:
        """Discard the observation."""

    def snapshot(self) -> SecuritySnapshot:
        """Report an empty window."""
        return SecuritySnapshot(
            since=datetime.now(UTC),
            total_events=0,
            events_by_type={},
            events_by_severity={},
            recent_rates={},
            distinct_sources=0,
            sources_capped=False,
            recent=(),
        )


#: The one monitor the process shares.
_shared: InMemorySecurityMonitor | None = None
_shared_lock = threading.Lock()


def get_security_monitor() -> InMemorySecurityMonitor:
    """Return the process-wide security monitor, creating it on first use."""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                from core.config import settings
                from services.metrics_registry import get_metrics_registry

                _shared = InMemorySecurityMonitor(
                    metrics=get_metrics_registry(),
                    recent_size=settings.MONITORING_SECURITY_FEED_SIZE,
                )
    return _shared


def reset_security_monitor() -> None:
    """Clear every counter. For tests."""
    get_security_monitor().reset()
