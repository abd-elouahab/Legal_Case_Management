"""WhatsApp delivery ORM model.

``18-whatsapp-delivery-channel.md`` asks for delivery **status tracking** and for
delivery metadata *"persisted for troubleshooting"*. This module is that record:
one row per notification the platform tried to deliver over WhatsApp, carrying
its lifecycle, its attempt count, the provider's own message identifier, and the
machine-readable reason it stopped where it did.

**One table, and it deliberately stores no message.** The wording is *rendered
per attempt* from :func:`~core.notifications.render_notification` and the
descriptors in ``apps/api/whatsapp/``. Storing it would create a second copy of
prose :mod:`models.notification` went out of its way not to persist — and it
would put the contents of a message into a table, which the spec's Logging
section forbids even for a log line. What is kept is the **envelope and the
outcome**: which number it was addressed to, which template produced it, which
language it was rendered in, and what happened.

**The delivery is not the notification**, exactly as
:class:`~models.email.EmailDelivery` is not: a notification is a thing one person
was told, a delivery is one attempt to carry it over one channel. They are two
tables because a notification is readable whether or not a message ever left the
building — nothing about the feed depends on this table existing, which is what
makes ``WHATSAPP_ENABLED=false`` a configuration rather than a degradation.

**``uq_whatsapp_deliveries_notification`` is the whole of "avoid duplicate
messages".** One notification produces at most one delivery row, whatever
re-dispatches it — a retried worker, a second API process, a restart that
re-queues. Retrying is then re-*using* that row rather than writing another,
which is the same shape ``ocr_results``, ``document_indexes``, and
``email_deliveries`` use for their own idempotency.

**The status enum is a database enum and the failure code is a ``VARCHAR``**, the
same split :mod:`models.email` makes. The four states are closed,
platform-defined, and each needs a colour in a monitoring view; the failure codes
are an open registry that grows as providers are added, and an ``ALTER TYPE`` per
new provider failure is exactly the redesign
``18-whatsapp-delivery-channel.md`` asks to avoid.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class WhatsAppDeliveryStatus(StrEnum):
    """Lifecycle state of one WhatsApp delivery.

    Exactly the four ``18-whatsapp-delivery-channel.md`` lists under "Delivery
    Status". The legal moves between them are declared once, in
    :data:`~core.whatsapp.STATUS_TRANSITIONS`, and every write goes through
    :class:`~services.whatsapp_delivery.WhatsAppDeliveryService` — so a delivery
    cannot arrive at :attr:`DELIVERED` without having been :attr:`SENDING`, which
    would make its duration a lie.

    Defined **here rather than in** :mod:`core.whatsapp`, following the rule this
    platform applies to :class:`~models.user.UserRole`,
    :class:`~models.report.ReportStatus`, and
    :class:`~models.email.EmailDeliveryStatus`: a vocabulary persisted as a
    database enum has its canonical definition beside the column it constrains,
    where the storage type and the Python type cannot drift apart.
    :mod:`core.whatsapp` re-exports it.
    """

    #: Queued, not yet picked up by a worker — or waiting out a retry backoff,
    #: which is the same state with a :attr:`WhatsAppDelivery.next_attempt_at` in
    #: the future. One state rather than two, for the reason
    #: :attr:`~models.email.EmailDeliveryStatus.PENDING` gives.
    PENDING = "pending"
    #: Claimed by a worker: rendering and handing to the provider.
    SENDING = "sending"
    #: **The provider accepted the message and issued an identifier for it.**
    #:
    #: The spec names this state "Delivered", and the name is kept — but what it
    #: honestly means is what :attr:`~models.email.EmailDeliveryStatus.SENT` means
    #: one channel over: the Cloud API returned a ``wamid`` and took
    #: responsibility for onward delivery. WhatsApp *does* publish real
    #: sent/delivered/read receipts, and they arrive on an **inbound webhook** —
    #: which is a public endpoint, a signature-verification scheme, and an inbound
    #: message surface that ``18-whatsapp-delivery-channel.md`` does not ask for.
    #: :attr:`WhatsAppDelivery.provider_message_id` is the identifier such a
    #: webhook would correlate on, which is why it is recorded now rather than
    #: when that feature arrives.
    DELIVERED = "delivered"
    #: Delivery stopped. Either the failure was permanent, or the transient
    #: retries were exhausted. Nothing else was touched: the notification is
    #: still in the recipient's feed, unread and unaffected.
    FAILED = "failed"


class WhatsAppDelivery(Base):
    """One attempt-tracked delivery of one notification, over WhatsApp."""

    __tablename__ = "whatsapp_deliveries"

    __table_args__ = (
        # **The duplicate guard, and it is an invariant rather than a
        # heuristic.** One notification is one message, whatever re-dispatches
        # it. A notification's identity is assigned once when it is persisted and
        # never reused, so this cannot suppress a genuine repeat — a hearing
        # changed twice produces two notifications and therefore two deliveries.
        UniqueConstraint("notification_id", name="uq_whatsapp_deliveries_notification"),
        # The sweeper's query, whole: everything queued and due, oldest first.
        # `next_attempt_at` is nullable (a first attempt is due immediately),
        # which is why the service asks for "null OR past" rather than relying on
        # how a dialect orders NULLs.
        Index(
            "ix_whatsapp_deliveries_status_next_attempt_at", "status", "next_attempt_at"
        ),
        # "Why did this person not get the message?", which is the question the
        # spec's troubleshooting requirement is actually about.
        Index("ix_whatsapp_deliveries_recipient_created_at", "recipient_id", "created_at"),
        # The monitoring aggregate's window.
        Index("ix_whatsapp_deliveries_created_at_status", "created_at", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: The notification this carries.
    #:
    #: ``CASCADE``, matching ``email_deliveries.notification_id``: a delivery
    #: describes an attempt to send one specific notification, so a delivery whose
    #: notification is gone is a record of nothing that anything could render or
    #: explain.
    notification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False
    )

    #: Who it is addressed to. ``CASCADE`` for the reason the notification's
    #: recipient is: every read of this table starts from a person or from an
    #: aggregate, and a recipient-less row is unreachable data.
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The number actually used, in E.164 without the leading ``+``, which is the
    #: form the Cloud API takes. **Snapshotted** rather than joined, for the
    #: reason :attr:`~models.email.EmailDelivery.recipient_email` is: a join would
    #: render the number the account has *today*, so a user who changed their
    #: phone would silently rewrite the history of where messages were sent — and
    #: "which number did this go to?" is the first question of any delivery
    #: investigation. It is an identifier, not content: it is never logged and
    #: never leaves the API in a response body.
    recipient_phone: Mapped[str] = mapped_column(String(32), nullable=False)

    #: Which :class:`~core.notifications.NotificationRule` produced the wording,
    #: and which area of the platform it belongs to. Copied from the notification
    #: so the monitoring aggregate can group by either without a join — and so a
    #: delivery still says what it was about if its rule is withdrawn in a later
    #: version.
    rule_key: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)

    #: Which template descriptor rendered its parameters, and which version of
    #: that descriptor. **This is also the name the template is registered under
    #: in the WhatsApp Business account**, which is what makes "was this sent
    #: through the template we had approved last week?" answerable from the row.
    template: Mapped[str] = mapped_column(String(100), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: ISO 639-1 code the wording was rendered in. Translated to the provider's
    #: own language tag at the boundary — see
    #: :func:`~core.whatsapp.provider_language_code`.
    language: Mapped[str] = mapped_column(String(10), nullable=False)

    status: Mapped[WhatsAppDeliveryStatus] = mapped_column(
        Enum(
            WhatsAppDeliveryStatus,
            name="whatsapp_delivery_status",
            # Persist the enum *values* rather than the Python member names,
            # matching every other enum column on this platform.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=WhatsAppDeliveryStatus.PENDING,
        server_default=WhatsAppDeliveryStatus.PENDING.value,
        index=True,
    )

    #: Send attempts made so far, including the one in flight.
    #:
    #: On the row rather than in the queue, for the reason every job's state on
    #: this platform is in PostgreSQL: the schedule lives in memory and does not
    #: survive a restart, while "this has already been tried four times" must.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: Why the last attempt failed, as a
    #: :class:`~core.whatsapp.WhatsAppFailureCode`. A machine-readable **code**,
    #: never the provider's message — the Cloud API's error bodies quote the
    #: recipient's number and echo template content, and this platform keeps both
    #: out of stored diagnostics.
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: When this becomes eligible for another attempt. ``None`` means "now".
    #:
    #: The whole of the retry schedule: a transient failure moves the row back to
    #: ``pending`` with this set into the future, and the sweeper picks it up when
    #: it comes due. A worker that slept out the backoff instead would hold a
    #: thread for an hour and lose the schedule on restart.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: When the current or most recent attempt was claimed. Reset on every claim,
    #: so a row stuck in ``sending`` past the stale threshold is recoverable.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: When the provider accepted the message.
    #:
    #: **Accepted, not read.** See :attr:`WhatsAppDeliveryStatus.DELIVERED` for
    #: the distinction and for why the platform does not currently claim more.
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Which provider accepted it ("meta"), for a deployment that changes one.
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: The provider's own identifier for the message — a ``wamid`` on Meta.
    #:
    #: Recorded because it is the **only** handle that correlates this row with
    #: anything on the provider's side: a support case, a Business Manager log,
    #: and the delivery-receipt webhook a later feature would consume. It is an
    #: opaque identifier rather than content, so keeping it breaks none of this
    #: feature's rules about what may be stored.
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: How long the successful send took, in milliseconds. Only the provider
    #: call — rendering is measured separately and is not what an operator
    #: investigating slow delivery is looking at.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # ------------------------------------------------------------- derived #

    @property
    def is_terminal(self) -> bool:
        """Whether this delivery has finished, one way or the other."""
        return self.status in (
            WhatsAppDeliveryStatus.DELIVERED,
            WhatsAppDeliveryStatus.FAILED,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Identifiers, the rule, and the state. **Never the number**, which is a
        # person's contact information, and never rendered wording — a repr ends
        # up in whatever log line interpolates the object, and
        # `18-whatsapp-delivery-channel.md` forbids logging message contents.
        return (
            f"<WhatsAppDelivery id={self.id!s} notification_id={self.notification_id!s} "
            f"rule={self.rule_key!r} status={self.status.value!r} attempts={self.attempts}>"
        )


__all__ = ["WhatsAppDelivery", "WhatsAppDeliveryStatus"]
