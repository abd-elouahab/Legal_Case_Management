"""Notification ORM models.

``architecture.md`` has listed *Notifications* under PostgreSQL since the storage
model was written; this module is that listing, and it is where **persistence of
an event** enters the platform. Real-time synchronization is ephemeral by design
— a client that missed something refetches — while ``code-standards.md`` requires
notifications to be *"persistent and recoverable"*, which is the whole reason
this feature has tables and ``modules/realtime`` has none.

**Two tables, and each earns its own.** :class:`Notification` is one row per
thing one person was told; :class:`NotificationPreference` is one row per
``(person, preference)`` they have expressed an opinion about. They are not one
table with a settings blob because they have entirely different lifetimes: a
notification is written once and never edited except to mark it read, while a
preference is a small mutable record with no history.

**A notification stores no prose, and that is the load-bearing decision here.**
The row keeps its ``rule_key`` and a small ``context``; the title and the message
are rendered per request by :func:`~core.notifications.render_notification` in
the language the reader asks for. Three consequences, and all three are why:

* a reader who switches to Arabic sees their **whole history** in Arabic, rather
  than a feed frozen in whichever language each row was written in;
* ``16-notifications.md``'s *"never log confidential notification contents"* is
  trivially true — there is no stored prose to log;
* a future email or WhatsApp sender renders from the same module, so
  ``code-standards.md``'s *"notification logic must never be duplicated"* holds
  across channels that do not exist yet.

**Two vocabularies are enums and one is not**, which is the same split
:mod:`models.report` and :mod:`models.timeline` make from opposite ends.
``notification_type`` and ``priority`` are closed, four-valued, platform-defined,
and each needs an icon and a colour in the client — so a fifth arriving without a
migration would be a value nothing could render, exactly like a sixth
``report_type``. ``category`` and ``rule_key`` are **open registries**:
``16-notifications.md`` requires that future categories and future events arrive
*"without redesign"*, and an ``ALTER TYPE`` per new event type is precisely that
redesign — the argument ``timeline_events.event_type`` already makes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from db.base import Base
from models.user import User

#: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else — the same variant
#: :mod:`models.timeline`, :mod:`models.conversation`, and :mod:`models.report`
#: use, and for the same two reasons: JSONB stores parsed and is indexable, and
#: the SQLite test database has no JSONB at all.
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class NotificationType(StrEnum):
    """How a notification should be read: is this news, a success, or a problem?

    The four ``16-notifications.md`` names. Defined **here rather than in**
    :mod:`core.notifications`, following the rule this platform already applies
    to :class:`~models.user.UserRole`, :class:`~models.case.CaseStatus`, and
    :class:`~models.report.ReportStatus`: a vocabulary persisted as a database
    enum has its canonical definition beside the column, so the storage type and
    the Python type cannot disagree. :mod:`core.notifications` re-exports it,
    exactly as :mod:`core.roles` re-exports ``UserRole``.

    That is also why :class:`~core.notifications.NotificationCategory` is *not*
    here: it is stored as a ``VARCHAR`` because the registry is open by design,
    so it has no storage type to be the canonical definition of.
    """

    INFORMATION = "information"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class NotificationPriority(StrEnum):
    """How loudly a notification should present itself.

    ``16-notifications.md``: *"Priority should influence presentation but not
    authorization."* Honoured literally — nothing in the notification service,
    the recipient resolver, or the API reads a priority when deciding **who**
    receives something. Defined here for the same reason
    :class:`NotificationType` is.

    The display and sort order is :data:`~core.notifications.PRIORITY_RANK`, not
    the member order: sorting alphabetically would place ``critical`` below
    ``low`` and make "most urgent first" mean nothing.
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class Notification(Base):
    """One thing one person was told."""

    __tablename__ = "notifications"

    __table_args__ = (
        # The notification panel and the history page, whole: this person's
        # notifications, newest first. Every read of this table starts from the
        # recipient, which is why all four indexes below lead with it — the same
        # reasoning `ix_reports_requested_by_status_created_at` records.
        Index("ix_notifications_recipient_created_at", "recipient_id", "created_at"),
        # The unread badge and the "unread only" filter. `read_at` is two-valued,
        # which would normally make it a poor index column — it is here because
        # the *badge* is the platform's most frequent notification query by a
        # wide margin, and it is a covering count over an already-narrow set.
        Index("ix_notifications_recipient_read_at", "recipient_id", "read_at", "created_at"),
        # The category filter the spec's "notification filtering" requires.
        Index(
            "ix_notifications_recipient_category_created_at",
            "recipient_id",
            "category",
            "created_at",
        ),
        # The semantic duplicate check: "has this person already been told this,
        # recently?" See `core.notifications.dedupe_key` for why that is a
        # windowed query rather than a unique constraint.
        Index("ix_notifications_recipient_dedupe_key", "recipient_id", "dedupe_key", "created_at"),
        # **The first of the two duplicate mechanisms**, and the one that is an
        # invariant rather than a heuristic: one dispatched event produces at
        # most one notification per person, whatever redelivers it. An event's
        # identity is assigned once by the dispatcher and never reused, so this
        # cannot suppress a genuine repeat — a case updated twice is two events
        # with two identities.
        #
        # ``event_id`` is nullable (a system announcement has no event), and both
        # PostgreSQL and SQLite treat NULLs as distinct in a unique index, so
        # announcements are unconstrained by it rather than limited to one.
        UniqueConstraint("recipient_id", "event_id", name="uq_notifications_recipient_event"),
        # The monitoring aggregate's window.
        Index("ix_notifications_created_at_category", "created_at", "category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: Whose notification this is.
    #:
    #: ``CASCADE`` rather than the ``SET NULL`` most user references on this
    #: platform use, matching ``conversations.owner_id`` and ``reports.
    #: requested_by`` and for the same reason: every read here is
    #: ``recipient_id = :caller``, so a recipient-less row would be unreachable
    #: data that nothing can ever serve or clean up.
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The dispatched :class:`~core.events.DomainEvent` this came from, or
    #: ``None`` for a system announcement, which has no event. See the unique
    #: constraint above for what it guarantees.
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    #: The event type's identifier, as a string. ``VARCHAR`` rather than an enum
    #: for the reason the module docstring gives, and stored beside ``rule_key``
    #: rather than derived from it because one event can produce several rules
    #: (a case assignment notifies the assignee and the unassigned separately).
    event_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    #: Which :class:`~core.notifications.NotificationRule` renders this row.
    rule_key: Mapped[str] = mapped_column(String(100), nullable=False)

    #: Which area of the platform this belongs to. An **open** registry — see the
    #: module docstring — and every read path tolerates a value it does not know.
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    notification_type: Mapped[NotificationType] = mapped_column(
        Enum(
            NotificationType,
            name="notification_type",
            # Persist the enum *values* rather than the Python member names,
            # matching `user_role`, `case_status`, `document_category`,
            # `ocr_status`, `index_status`, `conversation_status`, and
            # `report_status`.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=NotificationType.INFORMATION,
        server_default=NotificationType.INFORMATION.value,
    )

    priority: Mapped[NotificationPriority] = mapped_column(
        Enum(
            NotificationPriority,
            name="notification_priority",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=NotificationPriority.NORMAL,
        server_default=NotificationPriority.NORMAL.value,
    )

    #: The handful of values the template interpolates — a case number, a page
    #: count, a failure code. Bounded and screened by
    #: :func:`~core.notifications.normalize_context`, and deliberately **not** a
    #: copy of the event payload: a context is kept, and *"payloads should remain
    #: lightweight"* applies at least as strongly to something that is.
    context: Mapped[dict[str, Any]] = mapped_column(
        _JSON_TYPE, nullable=False, default=dict, server_default="{}"
    )

    #: The case this concerns, when there is one. Carried so a client can group a
    #: feed by matter without resolving each notification's target first, and
    #: ``SET NULL`` because a case is archived rather than deleted — this only
    #: guards manual cleanup, and a notification is still readable news after it.
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Who caused it, when a person did. ``SET NULL``, unlike the recipient: this
    #: is a reference for display, and losing the account must not cost the
    #: reader the notification.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: What this opens, as a **resource** rather than a URL — see
    #: :class:`~core.notifications.NotificationTargetType` for why navigation
    #: metadata never carries a path. ``target_id`` is null for a target that
    #: names the reader (their own account) and for a rule with no target at all.
    target_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    #: **The second duplicate mechanism.** A hash, so the column carries no
    #: identifiers even in an index dump; matched within a bounded window rather
    #: than constrained to be unique, because a case genuinely updated twice in a
    #: week is two notifications. See :func:`~core.notifications.dedupe_key`.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)

    #: When the recipient read it, or ``None``.
    #:
    #: A timestamp rather than a boolean, for the reason ``deleted_at`` is one
    #: everywhere else on this platform: *when* is strictly more information than
    #: *whether*, it costs the same, and "unread" stays expressible as a single
    #: ``IS NULL`` that an index can serve.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: When the recipient archived it, or ``None``.
    #:
    #: ``16-notifications.md`` puts archiving out of scope and asks the
    #: implementation to *"prepare for future archive functionality"*. This is
    #: that preparation and nothing more: the column exists, every read excludes
    #: archived rows, and **no endpoint writes it** — so the future feature is a
    #: route and a service method rather than a migration on a table that has
    #: grown by then.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # -------------------------------------------------------- relationships #

    actor: Mapped[User | None] = relationship("User", foreign_keys=[actor_id], lazy="selectin")

    # ------------------------------------------------------------- derived #

    @property
    def is_read(self) -> bool:
        """Whether the recipient has read this."""
        return self.read_at is not None

    @property
    def is_archived(self) -> bool:
        """Whether the recipient has archived this."""
        return self.archived_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Identifiers, the rule, and the state. **Never the context**, which
        # carries a case number, and never a rendered message — a repr ends up
        # in whatever log line interpolates the object, and `code-standards.md`
        # keeps a client's matter out of logs.
        return (
            f"<Notification id={self.id!s} recipient_id={self.recipient_id!s} "
            f"rule={self.rule_key!r} read={self.is_read}>"
        )


class NotificationPreference(Base):
    """One person's answer to "do you want to be told about this?".

    **One row per ``(user, key)`` rather than a column per preference**, and that
    is the shape the spec's *"preferences should prepare the platform for future
    delivery channels"* asks for. Two extensions fall out of it and neither is a
    redesign:

    * an **eighth preference** — hearing reminders, deadline alerts — is a new
      key and a new row, with **no migration**, because ``preference_key`` is an
      open registry like ``timeline_events.event_type``;
    * a **second channel** — email, WhatsApp, push — is one boolean column beside
      :attr:`in_app`, and every row already exists to receive it. That prediction
      has since been cashed: :attr:`email` is that column, added by
      ``17-email-delivery-channel.md`` with no new table and no backfill.

    A row is written only when somebody actually changes something. An account
    that has never opened the settings page has no rows at all and follows
    :data:`~core.notifications.DEFAULT_PREFERENCES`, which is both what that
    person would expect and what keeps a platform-wide default change from
    needing a backfill.
    """

    __tablename__ = "notification_preferences"

    __table_args__ = (
        # One opinion per person per preference. The unique constraint *is* the
        # upsert target — see `NotificationRepository.set_preferences` — so two
        # concurrent settings saves cannot leave a user with two contradictory
        # rows for the same key.
        UniqueConstraint(
            "user_id", "preference_key", name="uq_notification_preferences_user_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Which preference this is, from
    #: :class:`~core.notifications.NotificationPreferenceKey`. ``VARCHAR``
    #: because the registry is open; read tolerantly by
    #: :func:`~core.notifications.preference_from_value`, so a key written by a
    #: later version of the platform cannot make an earlier one unable to load
    #: somebody's settings.
    preference_key: Mapped[str] = mapped_column(String(50), nullable=False)

    #: Whether in-app notifications of this kind are delivered.
    in_app: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    #: Whether emails of this kind are delivered.
    #:
    #: **The second channel, and it arrived as one column** — which is exactly
    #: what the note above predicted when this table shipped with one. Nothing
    #: else about the preference model changed: no new table, no new key, no
    #: backfill, and an account that has never opened the settings page still has
    #: no row and still follows
    #: :data:`~core.notifications.DEFAULT_PREFERENCES`.
    #:
    #: ``NOT NULL DEFAULT true`` rather than nullable, deliberately. A nullable
    #: column would make "the user has not chosen" and "the user chose on" two
    #: different stored states for the same behaviour, and every reader would have
    #: to collapse them — while the platform already has a perfectly good
    #: representation of "has not chosen": **no row at all**. ``whatsapp``,
    #: ``push``, and ``sms`` join this the same way when those channels land.
    #:
    #: Note that this only ever *narrows*: a notification kind with no entry in
    #: :data:`~core.email.EMAIL_RULES` is never emailed however this is set.
    email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<NotificationPreference user_id={self.user_id!s} "
            f"key={self.preference_key!r} in_app={self.in_app} email={self.email}>"
        )


__all__ = [
    "Notification",
    "NotificationPreference",
    "NotificationPriority",
    "NotificationType",
]
