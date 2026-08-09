"""Email delivery response schemas.

Deliberately small, and what is **absent** is the design rather than an omission.

``17-email-delivery-channel.md`` asks for monitoring and for a delivery history
that *"remains available for troubleshooting"*, and this module serves the first
and stops there. There is **no endpoint that lists deliveries**, because a
delivery names a person, a rule, a moment, and an address, and a list of them is a
live index of who the platform is writing to about what — precisely the disclosure
:mod:`services.notification_metrics` refuses to make when it explains why its
counters are grouped by *rule* rather than by recipient. The history lives in
``email_deliveries``, where an operator investigating a specific complaint can
query it under the access controls the database already has, and the API exposes
what an operational view legitimately needs: **counts, durations, rates, and
causes**.

Nothing here can carry a subject, a body, a case number, or an address. The
figures are aggregates and the breakdowns are keyed by rule and by failure code,
both of which are platform vocabulary rather than anybody's data.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EmailMetricsQuery(BaseModel):
    """Validated query parameters for ``GET /notifications/email/metrics``."""

    model_config = ConfigDict(extra="forbid")

    window_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description=(
            "Only count deliveries created in the last N days. Omitted covers the platform's "
            "whole history, which is the right default for a figure like 'emails sent'."
        ),
    )


class EmailMetricsRead(BaseModel):
    """Platform-wide email delivery health.

    Every figure ``17-email-delivery-channel.md``'s Monitoring section names —
    **queued emails, sent emails, failed emails, retry count, and average delivery
    latency** — plus the breakdowns that say *what* is being sent and *why*
    something failed.

    The figures come from two places on purpose, exactly as the notification
    metrics do. Counts of rows (queued, sending, sent, failed, attempts) are **SQL
    aggregates**: they are properties of persisted deliveries, so counting them in
    a process would reset on restart *and* be wrong across instances — and "how
    many emails are stuck?" is the first question after a restart. Retries,
    latency, and skips accumulate **in this process**, because an attempt is not a
    row, which is why `since` qualifies those and not the others.
    """

    since: datetime = Field(
        description="When this process started counting the in-process figures."
    )
    enabled: bool = Field(description="Whether email delivery is switched on here.")
    provider: str = Field(description="Which provider implementation is configured.")
    provider_available: bool = Field(
        description=(
            "Whether that provider is configured well enough to be worth calling. False means "
            "nothing is queued at all — a deliberate choice over accumulating a backlog whose "
            "only outcome is a burst of very old mail the day somebody configures a relay."
        )
    )
    templates_available: bool = Field(
        description="Whether the email templates can be loaded on this deployment."
    )

    total_deliveries: int = Field(description="Deliveries recorded, in the window.")
    queued: int = Field(
        description=(
            "Deliveries waiting for a worker, or waiting out a retry backoff. The spec's "
            "'queued emails'."
        )
    )
    sending: int = Field(
        description=(
            "Deliveries claimed by a worker right now. A figure that stays high is the "
            "signature of a process that died mid-send; the sweeper reclaims them."
        )
    )
    sent: int = Field(
        description=(
            "Deliveries a provider **accepted**. Not 'arrived' and certainly not 'read': SMTP "
            "hands off to a relay, and what happens after that is outside anything this "
            "platform can observe."
        )
    )
    failed: int = Field(
        description="Deliveries the platform gave up on, permanently or after exhausting retries."
    )
    delivery_rate: float = Field(
        description="Share of finished deliveries that were accepted, 0-100."
    )
    recipients: int = Field(
        description="Distinct accounts the platform has written to. A count, never a list."
    )
    attempts: int = Field(
        description="Send attempts across every delivery in the window, successes included."
    )

    queued_this_process: int = Field(
        description="Deliveries this process queued."
    )
    sent_this_process: int = Field(description="Deliveries this process sent.")
    failed_this_process: int = Field(description="Deliveries this process gave up on.")
    retried: int = Field(
        description=(
            "Transient failures this process rescheduled — the spec's 'retry count'. Counted "
            "when a retry is *scheduled* rather than when one runs, so a relay failing in real "
            "time is visible as this climbing while `sent_this_process` does not."
        )
    )
    skipped: int = Field(
        description=(
            "Notifications that produced no email. **Not a failure**: most of the platform's "
            "notifications are in-app only by design. See `skipped_by_reason`."
        )
    )

    average_delivery_latency_ms: float | None = Field(
        default=None,
        description=(
            "Mean time from the notification being created to a provider accepting the message. "
            "Null rather than zero when nothing has been sent — an average over no deliveries "
            "is undefined, while zero would read as instantaneous."
        ),
    )
    average_send_duration_ms: float | None = Field(
        default=None,
        description=(
            "Mean time inside the provider call alone. What an operator investigating a slow "
            "relay wants: the figure above includes the queue wait, which on a healthy "
            "deployment is most of it."
        ),
    )

    sent_by_rule: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "What this process has been sending, by notification rule. By rule rather than by "
            "recipient: 'eleven hearing changes were mailed' is throughput, 'eleven emails to "
            "Amina' is a statement about a person's work."
        ),
    )
    failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description="Why deliveries were given up on, by cause, in this process.",
    )
    stored_failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description="Why stored deliveries failed, by cause. Survives a restart.",
    )
    retries_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Why deliveries were rescheduled, by cause. Separate from the failure breakdowns: a "
            "wall of `throttled` retries that eventually succeed is a relay asking the platform "
            "to slow down, while the same code under failures is mail that never arrived."
        ),
    )
    skipped_by_reason: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Why notifications produced no email — not email-eligible, suppressed by a "
            "preference, no usable address, already queued, no provider, or disabled. Together "
            "these answer 'why did that person not get an email?' without reading the table."
        ),
    )

    window_days: int | None = Field(
        default=None,
        description="The window applied to the SQL figures, or null for all history.",
    )


__all__ = ["EmailMetricsQuery", "EmailMetricsRead"]
