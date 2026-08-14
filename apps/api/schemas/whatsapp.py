"""WhatsApp delivery response schemas.

Deliberately small, and what is **absent** is the design rather than an omission.

``18-whatsapp-delivery-channel.md`` asks for monitoring and for delivery metadata
*"persisted for troubleshooting"*, and this module serves the first and stops
there. There is **no endpoint that lists deliveries**, for the reason
:mod:`schemas.email` gives and one degree more strongly: a delivery names a
person, a rule, a moment, and a **phone number**, and a list of them is a live
index of who the platform is messaging about what. A phone number is more
identifying than an email address — it is a device somebody carries, and it is
frequently the same one used for everything else in their life. The history lives
in ``whatsapp_deliveries``, where an operator investigating a specific complaint
can query it under the access controls the database already has, and the API
exposes what an operational view legitimately needs: **counts, durations, rates,
and causes**.

Nothing here can carry a message, a case number, or a number. The figures are
aggregates and the breakdowns are keyed by rule and by failure code, both of which
are platform vocabulary rather than anybody's data. The one field that is neither
is ``configuration_errors`` — a list of **setting names**, which is configuration
rather than content and is exactly what the spec's *"provide meaningful error
messages"* asks a misconfigured deployment to be told.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WhatsAppMetricsQuery(BaseModel):
    """Validated query parameters for ``GET /notifications/whatsapp/metrics``."""

    model_config = ConfigDict(extra="forbid")

    window_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description=(
            "Only count deliveries created in the last N days. Omitted covers the platform's "
            "whole history, which is the right default for a figure like 'messages delivered'."
        ),
    )


class WhatsAppMetricsRead(BaseModel):
    """Platform-wide WhatsApp delivery health.

    Every figure ``18-whatsapp-delivery-channel.md``'s Monitoring section names —
    **queued messages, delivered messages, failed deliveries, retry count, average
    delivery latency, and provider response time** — plus the breakdowns that say
    *what* is being sent and *why* something failed.

    The figures come from two places on purpose, exactly as the email metrics do.
    Counts of rows (queued, sending, delivered, failed, attempts) are **SQL
    aggregates**: they are properties of persisted deliveries, so counting them in
    a process would reset on restart *and* be wrong across instances — and "how
    many messages are stuck?" is the first question after a restart. Retries,
    latency, provider response time, and skips accumulate **in this process**,
    because an attempt is not a row, which is why `since` qualifies those and not
    the others.
    """

    since: datetime = Field(
        description="When this process started counting the in-process figures."
    )
    enabled: bool = Field(description="Whether WhatsApp delivery is switched on here.")
    provider: str = Field(description="Which provider implementation is configured.")
    provider_available: bool = Field(
        description=(
            "Whether that provider is configured well enough to be worth calling. False means "
            "nothing is queued at all — a deliberate choice over accumulating a backlog whose "
            "only outcome is a burst of very old messages the day somebody finishes the setup."
        )
    )
    configuration_errors: list[str] = Field(
        default_factory=list,
        description=(
            "Required settings that are missing or unusable, **by name** — never by value. The "
            "spec's 'provide meaningful error messages' for a misconfiguration: a deployment "
            "that switched the channel on and forgot a token reads the setting's name here "
            "rather than guessing from an empty metrics page."
        ),
    )
    templates_available: bool = Field(
        description=(
            "Whether the template descriptors can be loaded on this deployment. Note that this "
            "says nothing about whether the matching templates are **approved** in the WhatsApp "
            "Business account — the platform cannot see that, and a template that is not "
            "approved surfaces as `template_rejected` in the failure breakdown."
        )
    )

    total_deliveries: int = Field(description="Deliveries recorded, in the window.")
    queued: int = Field(
        description=(
            "Deliveries waiting for a worker, or waiting out a retry backoff. The spec's "
            "'queued messages'."
        )
    )
    sending: int = Field(
        description=(
            "Deliveries claimed by a worker right now. A figure that stays high is the "
            "signature of a process that died mid-send; the sweeper reclaims them."
        )
    )
    delivered: int = Field(
        description=(
            "Deliveries a provider **accepted and issued an identifier for**. Not 'read', and "
            "not the sent/delivered/read receipt WhatsApp itself publishes — those arrive on an "
            "inbound webhook, which is a public endpoint and an inbound message surface this "
            "spec does not ask for. The `wamid` is recorded on every row so that a later feature "
            "has something to correlate on."
        )
    )
    failed: int = Field(
        description="Deliveries the platform gave up on, permanently or after exhausting retries."
    )
    delivery_rate: float = Field(
        description="Share of finished deliveries that were accepted, 0-100."
    )
    recipients: int = Field(
        description="Distinct accounts the platform has messaged. A count, never a list."
    )
    attempts: int = Field(
        description="Send attempts across every delivery in the window, successes included."
    )

    queued_this_process: int = Field(description="Deliveries this process queued.")
    delivered_this_process: int = Field(
        description="Deliveries this process handed to a provider successfully."
    )
    failed_this_process: int = Field(description="Deliveries this process gave up on.")
    retried: int = Field(
        description=(
            "Transient failures this process rescheduled — the spec's 'retry count'. Counted "
            "when a retry is *scheduled* rather than when one runs, so a rate limit biting in "
            "real time is visible as this climbing while `delivered_this_process` does not."
        )
    )
    skipped: int = Field(
        description=(
            "Notifications that produced no message. **Not a failure**: most of the platform's "
            "notifications are in-app only by design, and an account with no phone number is "
            "skipped forever and correctly. See `skipped_by_reason`."
        )
    )

    average_delivery_latency_ms: float | None = Field(
        default=None,
        description=(
            "Mean time from the notification being created to a provider accepting the message. "
            "Null rather than zero when nothing has been delivered — an average over no "
            "deliveries is undefined, while zero would read as instantaneous."
        ),
    )
    average_provider_response_ms: float | None = Field(
        default=None,
        description=(
            "Mean time inside the provider call alone — the spec's 'provider response time'. "
            "The figure above includes the queue wait, which on a healthy deployment is most of "
            "it, so the two answer different questions: 'are we slow?' and 'is WhatsApp slow?'."
        ),
    )

    delivered_by_rule: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "What this process has been sending, by notification rule. By rule rather than by "
            "recipient: 'eleven hearing changes were messaged' is throughput, 'eleven messages "
            "to Amina' is a statement about a person's work."
        ),
    )
    failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description="Why deliveries were given up on, by cause, in this process.",
    )
    stored_failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Why stored deliveries failed, by cause. Survives a restart. `template_rejected` is "
            "the one to watch: it means the WhatsApp Business account refused an approved "
            "template — paused, deleted, missing in that language, or given the wrong number of "
            "parameters — and it is fixed in Business Manager rather than in this codebase."
        ),
    )
    retries_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Why deliveries were rescheduled, by cause. Separate from the failure breakdowns: a "
            "wall of `throttled` retries that eventually succeed is WhatsApp asking the platform "
            "to slow down, while the same code under failures is a message that never arrived."
        ),
    )
    skipped_by_reason: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Why notifications produced no message — not WhatsApp-eligible, suppressed by a "
            "preference, no usable phone number, already queued, no provider, or disabled. "
            "Together these answer 'why did that person not get a message?' without reading the "
            "table."
        ),
    )

    window_days: int | None = Field(
        default=None,
        description="The window applied to the SQL figures, or null for all history.",
    )


__all__ = ["WhatsAppMetricsQuery", "WhatsAppMetricsRead"]
