"""Wire schemas for the real-time module's HTTP surface.

Only the **REST** surface: the monitoring view and the presence roster. The
socket's own frames are not Pydantic models and deliberately so — they are
encoded and decoded by :mod:`websocket.protocol`, which is pure and has no
validation framework in it, because a frame arrives thousands of times more often
than a request and building a model per event would be paying request-shaped
costs on a message-shaped path.

Both models below are **read-only and administrative**. Neither carries a case, a
document, a topic, or a payload: an operator asking "is the channel healthy?"
must not receive an index of which matters are being worked on, which is exactly
what a per-topic breakdown would be.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class RealtimeMetricsRead(BaseModel):
    """Platform-wide real-time health.

    The five figures ``15-real-time-synchronization.md``'s Monitoring section
    names — active connections, event throughput, average delivery latency,
    failed deliveries, reconnect count — plus the derived rates and the
    breakdowns an operator needs to act on any of them.
    """

    model_config = ConfigDict(from_attributes=True)

    since: datetime = Field(
        description=(
            "When this process started counting. The counters below are in-process: "
            "they reset on restart, and each API instance counts only its own traffic. "
            "`active_connections` and `present_users` are exact for this instance by "
            "definition, because a socket is held by exactly one process."
        )
    )

    # --- Connections -------------------------------------------------------- #
    enabled: bool = Field(description="Whether this deployment accepts WebSocket connections.")
    active_connections: int = Field(description="Live connections held by this process.")
    present_users: int = Field(
        description=(
            "Distinct accounts holding at least one connection. Lower than "
            "`active_connections` whenever somebody has two tabs open."
        )
    )
    total_connections: int
    total_disconnections: int
    reconnections: int = Field(
        description="Connections that arrived declaring a previous sequence."
    )
    rejected_connections: int
    subscribed_topics: int = Field(
        description="Distinct topics currently followed. A shape figure — never the topics."
    )
    pending_dispatches: int = Field(
        description=(
            "Events accepted and not yet routed. Non-zero for more than an instant "
            "means the dispatch thread is behind."
        )
    )

    # --- Events ------------------------------------------------------------- #
    events_published: int
    events_rejected: int = Field(
        description=(
            "Events the dispatcher refused to build — a malformed payload or an unscoped "
            "type. Always a publisher bug, so any non-zero value is actionable."
        )
    )
    events_delivered: int
    events_denied: int = Field(
        description=(
            "Deliveries suppressed because the connection was no longer authorized for "
            "the topic. Not a failure — the authorization layer doing its job."
        )
    )
    events_deduplicated: int = Field(
        description="Deliveries suppressed because that connection already had the event."
    )
    failed_deliveries: int

    # --- Derived ------------------------------------------------------------ #
    average_delivery_latency_ms: float | None = Field(
        default=None,
        description=(
            "Mean time from publication to being queued for a socket. `null` when nothing "
            "has been delivered — an average over no deliveries is undefined, while zero "
            "would read as instantaneous."
        ),
    )
    delivery_success_rate: float
    average_fanout: float | None = Field(
        default=None,
        description="Mean connections one published event reached. Near zero means nobody is subscribing.",
    )

    events_by_type: dict[str, int] = Field(default_factory=dict)
    failures_by_code: dict[str, int] = Field(default_factory=dict)
    subscriber_failures: dict[str, int] = Field(default_factory=dict)
    subscribers: list[str] = Field(
        default_factory=list,
        description=(
            "Registered event consumers. One today (`websocket`); Notifications, Email, "
            "WhatsApp, and analytics are specified to join it."
        ),
    )


# --------------------------------------------------------------------------- #
# Presence
# --------------------------------------------------------------------------- #


class PresenceRead(BaseModel):
    """One account currently connected.

    Counts connections rather than listing them: how many devices somebody has
    open is not something anyone needs, and the roster answers "who is working",
    not "from where".
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    role: str
    connections: int = Field(description="How many live connections this account holds.")
    since: datetime = Field(
        description="When this account's oldest live connection was established."
    )


class PresenceListRead(BaseModel):
    """The presence roster for this API process."""

    items: list[PresenceRead]
    total: int = Field(description="Distinct accounts present on this instance.")


__all__ = ["PresenceListRead", "PresenceRead", "RealtimeMetricsRead"]
