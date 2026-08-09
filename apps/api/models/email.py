"""Email delivery ORM model.

``17-email-delivery-channel.md`` asks for delivery **status tracking** and for a
delivery history that *"remains available for troubleshooting"*. This module is
that record: one row per notification the platform tried to deliver by email,
carrying its lifecycle, its attempt count, and the machine-readable reason it
stopped where it did.

**One table, and it deliberately stores no message.** The subject, the HTML, and
the plain text are *rendered per attempt* from
:func:`~core.notifications.render_notification` and the templates in
``apps/api/emails/``. Storing them would create a second copy of wording that
:mod:`models.notification` went out of its way not to persist — and it would put
the contents of an email into a table, which the spec's Logging section forbids
even for a log line. What is kept is the **envelope and the outcome**: who it was
addressed to, which template produced it, which language it was rendered in, and
what happened.

**The delivery is not the notification, and that separation is the feature.**
A notification is a thing one person was told; a delivery is one attempt to carry
it over one channel. They are one-to-one *today* because email is the only
channel with a delivery record, and they are two tables because a notification is
readable whether or not an email ever left the building — nothing about the feed
depends on this table existing, which is what makes
``EMAIL_ENABLED=false`` a configuration rather than a degradation.

**``uq_email_deliveries_notification`` is the whole of "avoid duplicate
emails".** One notification produces at most one delivery row, whatever
re-dispatches it — a retried worker, a second API process, a restart that
re-queues. Retrying is then re-*using* that row rather than writing another,
which is the same shape ``ocr_results`` and ``document_indexes`` use for their
own idempotency: the row is the identity of the work, so the work cannot be
duplicated by being scheduled twice.

**The status enum is a database enum and the failure code is a ``VARCHAR``**, the
split :mod:`models.report` and :mod:`models.timeline` make from opposite ends.
The four states are closed, platform-defined, and each needs a colour in a
monitoring view; the failure codes are an open registry that grows as providers
are added, and an ``ALTER TYPE`` per new provider failure is exactly the redesign
``17-email-delivery-channel.md`` asks to avoid.
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


class EmailDeliveryStatus(StrEnum):
    """Lifecycle state of one email delivery.

    Exactly the four ``17-email-delivery-channel.md`` lists under "Delivery
    Status". The legal moves between them are declared once, in
    :data:`~core.email.STATUS_TRANSITIONS`, and every write goes through
    :class:`~services.email_delivery.EmailDeliveryService` — so a delivery cannot
    arrive at :attr:`SENT` without having been :attr:`SENDING`, which would make
    its duration a lie.

    Defined **here rather than in** :mod:`core.email`, following the rule this
    platform applies to :class:`~models.user.UserRole`,
    :class:`~models.report.ReportStatus`, and
    :class:`~models.notification.NotificationType`: a vocabulary persisted as a
    database enum has its canonical definition beside the column it constrains,
    where the storage type and the Python type cannot drift apart.
    :mod:`core.email` re-exports it.
    """

    #: Queued, not yet picked up by a worker — or waiting out a retry backoff,
    #: which is the same state with a :attr:`EmailDelivery.next_attempt_at` in the
    #: future. One state rather than two, because "a worker should pick this up
    #: when it is due" is one condition and a second state would be a second
    #: thing for the sweeper to remember to look at.
    PENDING = "pending"
    #: Claimed by a worker: rendering and handing to the provider.
    SENDING = "sending"
    #: The provider accepted it. **Not** "the recipient read it" — see
    #: :attr:`EmailDelivery.sent_at`.
    SENT = "sent"
    #: Delivery stopped. Either the failure was permanent, or the transient
    #: retries were exhausted. Nothing else was touched: the notification is
    #: still in the recipient's feed, unread and unaffected.
    FAILED = "failed"


class EmailDelivery(Base):
    """One attempt-tracked delivery of one notification, by email."""

    __tablename__ = "email_deliveries"

    __table_args__ = (
        # **The duplicate guard, and it is an invariant rather than a
        # heuristic.** One notification is one email, whatever re-dispatches it.
        # A notification's identity is assigned once when it is persisted and
        # never reused, so this cannot suppress a genuine repeat — a case updated
        # twice produces two notifications and therefore two deliveries.
        UniqueConstraint("notification_id", name="uq_email_deliveries_notification"),
        # The sweeper's query, whole: everything queued and due, oldest first.
        # `next_attempt_at` is nullable (a first attempt is due immediately), and
        # both PostgreSQL and SQLite order NULLs consistently within a single
        # dialect — which is why the service asks for "null OR past" rather than
        # relying on the ordering of the two.
        Index("ix_email_deliveries_status_next_attempt_at", "status", "next_attempt_at"),
        # "Why did this person not get the email?", which is the question the
        # spec's troubleshooting requirement is actually about.
        Index("ix_email_deliveries_recipient_created_at", "recipient_id", "created_at"),
        # The monitoring aggregate's window.
        Index("ix_email_deliveries_created_at_status", "created_at", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: The notification this carries.
    #:
    #: ``CASCADE``, matching ``notifications.recipient_id``: a delivery describes
    #: an attempt to send one specific notification, so a delivery whose
    #: notification is gone is a record of nothing that anything could ever
    #: render or explain.
    notification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False
    )

    #: Who it is addressed to. ``CASCADE`` for the reason the notification's
    #: recipient is: every read of this table starts from a person or from an
    #: aggregate, and a recipient-less row is unreachable data.
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The address actually used, **snapshotted** rather than joined.
    #:
    #: The same reasoning ``timeline_events.actor_name`` records: a join would
    #: render the address the account has *today*, so a user who changed their
    #: email would silently rewrite the history of where messages were sent — and
    #: "which address did this go to?" is the first question of any delivery
    #: investigation. It is an identifier, not content: it is never logged and
    #: never leaves the API in a response body.
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)

    #: Which :class:`~core.notifications.NotificationRule` produced the wording,
    #: and which area of the platform it belongs to. Copied from the notification
    #: so the monitoring aggregate can group by either without a join — and so a
    #: delivery still says what it was about if its rule is withdrawn in a later
    #: version.
    rule_key: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)

    #: Which template rendered it, and which version of that template.
    #:
    #: Recorded for the reason a message records its prompt version and an index
    #: records its embedding model: configuration is *current* and a delivery is
    #: *historical*, so "was this sent with the template we fixed last week?" is
    #: only answerable if the row says.
    template: Mapped[str] = mapped_column(String(100), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: ISO 639-1 code the subject and body were rendered in.
    language: Mapped[str] = mapped_column(String(10), nullable=False)

    status: Mapped[EmailDeliveryStatus] = mapped_column(
        Enum(
            EmailDeliveryStatus,
            name="email_delivery_status",
            # Persist the enum *values* rather than the Python member names,
            # matching every other enum column on this platform.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=EmailDeliveryStatus.PENDING,
        server_default=EmailDeliveryStatus.PENDING.value,
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
    #: :class:`~core.email.EmailFailureCode`. A machine-readable **code**, never
    #: the provider's message — an SMTP server's rejection text can echo the
    #: envelope, and this platform keeps recipients out of stored diagnostics for
    #: the same reason it keeps OCR's library messages out of ``ocr_results``.
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
    #: **Accepted, not delivered and certainly not read.** SMTP hands off to a
    #: relay; what happens after that is outside anything this platform can
    #: observe, and a column called ``delivered_at`` would be a claim the
    #: platform cannot support. The same line
    #: :class:`~services.notification_metrics.NotificationMetricsRecorder` draws
    #: between "published onto the channel" and "arrived in a browser".
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Which provider accepted it ("smtp"), for a deployment that changes one.
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: How long the successful send took, in milliseconds. Only the provider
    #: call — rendering is measured separately and is not what an operator
    #: investigating slow mail is looking at.
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
        return self.status in (EmailDeliveryStatus.SENT, EmailDeliveryStatus.FAILED)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Identifiers, the rule, and the state. **Never the address**, which is a
        # person's contact information, and never a rendered subject — a repr
        # ends up in whatever log line interpolates the object, and
        # ``17-email-delivery-channel.md`` forbids logging email contents.
        return (
            f"<EmailDelivery id={self.id!s} notification_id={self.notification_id!s} "
            f"rule={self.rule_key!r} status={self.status.value!r} attempts={self.attempts}>"
        )


__all__ = ["EmailDelivery", "EmailDeliveryStatus"]
