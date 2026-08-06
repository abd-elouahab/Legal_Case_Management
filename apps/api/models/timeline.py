"""Timeline event ORM model.

The platform's activity timeline and audit trail: one append-only row per
significant thing that happened to a case. `architecture.md` invariant 9 requires
every sensitive operation to be recorded, and ``08-timeline.md`` makes that record
a **business** artefact — something a lawyer reads in the case workspace — rather
than an operational log.

Three properties shape the table:

* **Append-only.** Nothing in the platform updates or deletes a timeline event.
  A record of what happened that could later be edited is not an audit trail, so
  the row has no ``updated_at`` and no soft-delete column: there is no operation
  that would set either.
* **The actor is snapshotted, not joined.** ``actor_name`` and ``actor_role`` are
  copied onto the row at the moment of the event. A join would render the actor's
  name and role *as they are today*, so renaming a user or changing their role
  would silently rewrite history. ``actor_id`` is kept alongside for correlation
  and stays usable while the account exists.
* **``metadata`` is open.** A JSON column is what lets a future module attach the
  specifics of its own events — a filename, a pair of statuses, an assignee —
  without a schema change, which is the extensibility ``08-timeline.md``
  requires.

No authorization logic lives here. Whether a caller may use the timeline
capability is decided by :mod:`core.roles`; whether they may reach *this* event is
decided by :mod:`services.timeline_access`, which defers to the event's case.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from db.base import Base
from models.case import Case
from models.user import User


class TimelineEventCategory(StrEnum):
    """The family an event belongs to, which decides how it is presented.

    ``08-timeline.md`` names five in its "Timeline Icons" section — case,
    document, assignment, status, priority — and each gets one icon. It is
    *derived* from the event type (see :data:`~core.timeline.EVENT_CATEGORIES`)
    rather than stored, so the two can never disagree and a new event type cannot
    arrive uncategorised.
    """

    CASE = "case"
    STATUS = "status"
    PRIORITY = "priority"
    ASSIGNMENT = "assignment"
    DOCUMENT = "document"


class TimelineEventType(StrEnum):
    """The registry of event types the platform records.

    Every event type is named exactly once, here, in the three groups
    ``08-timeline.md`` lists. Publishers import a member rather than writing a
    string, so a typo is a static error rather than an event nobody can filter
    for.

    **Extending it is one member below plus a grant of meaning in
    :mod:`core.timeline`** — no migration, because the column is a plain
    ``VARCHAR`` rather than a PostgreSQL enum. That is a deliberate departure from
    ``user_role``, ``case_status``, and ``document_category``: those are small,
    closed vocabularies, whereas the spec requires that *"future modules should be
    able to publish events without modifying the Timeline implementation"*, and an
    ``ALTER TYPE`` per new event type is exactly the modification it rules out.
    """

    # --- Case events -------------------------------------------------------- #
    CASE_CREATED = "case_created"
    CASE_UPDATED = "case_updated"
    CASE_ARCHIVED = "case_archived"
    CASE_RESTORED = "case_restored"
    STATUS_CHANGED = "status_changed"
    PRIORITY_CHANGED = "priority_changed"

    # --- Assignment events -------------------------------------------------- #
    LAWYER_ASSIGNED = "lawyer_assigned"
    LAWYER_REMOVED = "lawyer_removed"
    REPRESENTATIVE_ASSIGNED = "representative_assigned"
    REPRESENTATIVE_REMOVED = "representative_removed"

    # --- Document events ---------------------------------------------------- #
    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_UPDATED = "document_updated"
    DOCUMENT_REPLACED = "document_replaced"
    DOCUMENT_DELETED = "document_deleted"
    DOCUMENT_DOWNLOADED = "document_downloaded"

    # --- OCR events --------------------------------------------------------- #
    #
    # Added by ``09-ocr-processing.md`` and the first demonstration that the
    # registry's promise holds: four members here, four lines in each mapping in
    # :mod:`core.timeline`, and **no migration** — because ``event_type`` is a
    # ``VARCHAR`` rather than a database enum, precisely so a later module could
    # do this. They are categorised as document events, so they reuse the five
    # icon families ``08-timeline.md`` defines rather than inventing a sixth.
    OCR_STARTED = "ocr_started"
    OCR_COMPLETED = "ocr_completed"
    OCR_FAILED = "ocr_failed"
    OCR_RETRIED = "ocr_retried"

    # --- Document indexing events ------------------------------------------- #
    #
    # Added by ``10-document-indexing.md``, the second module to extend the
    # registry — again with **no migration**, for the reason given above. Also
    # categorised as document events: making a document searchable is something
    # that happened to that document, and inventing a sixth icon family for it
    # would fragment the timeline's presentation for no gain to the reader.
    INDEXING_STARTED = "indexing_started"
    INDEXING_COMPLETED = "indexing_completed"
    INDEXING_FAILED = "indexing_failed"
    INDEXING_RETRIED = "indexing_retried"


#: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else.
#:
#: JSONB stores parsed and is indexable, which is what a future "find every event
#: mentioning this document" query would need. The variant keeps the SQLite test
#: database working, where JSONB does not exist.
_METADATA_TYPE = JSON().with_variant(JSONB(), "postgresql")


class TimelineEvent(Base):
    """One recorded activity on a case."""

    __tablename__ = "timeline_events"

    # The composite index is the one the case timeline actually uses: every read
    # of this table filters by case and orders by time. Two single-column indexes
    # cannot serve that as well, because the second could only be applied after
    # the first has already produced the rows.
    __table_args__ = (
        Index("ix_timeline_events_case_id_created_at", "case_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: Every event belongs to a case — the timeline is the case's history, and an
    #: event with no case would be unreachable by every query in the module.
    #: CASCADE for the same reason as ``documents.case_id``: cases are archived
    #: rather than deleted, so this only guards against manual cleanup.
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: A member of :class:`TimelineEventType`. Stored as text rather than as a
    #: database enum so the registry can grow without a migration — see the class
    #: docstring for why that matters here and nowhere else.
    event_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    #: Short headline, e.g. "Document Uploaded". Defaulted from the event type by
    #: :func:`~core.timeline.default_title` so every publisher does not have to
    #: invent one, but overridable for an event whose type is too generic to say
    #: what happened.
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    #: The human sentence describing the event, e.g. ``Amina Benali uploaded
    #: "Contract.pdf"``. ``Text`` rather than a capped ``String`` because it is
    #: prose, and truncating it mid-sentence would be worse than storing it.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---------------------------------------------------------------- actor #
    #
    # Kept as an identifier *and* as a snapshot. The identifier correlates the
    # event with a live account; the snapshot is what the timeline displays, so
    # the history reads as it did when it happened.

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The actor's display name at the time of the event. Nullable because a
    #: future scheduled job or system process may act with no user behind it.
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: The actor's platform role at the time of the event, as a
    #: :class:`~models.user.UserRole` value.
    #:
    #: Text rather than the ``user_role`` enum, for the same reason as
    #: :attr:`event_type`: this column is a *snapshot*, and the whole point of
    #: snapshotting is that the live vocabulary may move on. A role retired from
    #: ``UserRole`` in five years must still read back from a row written today,
    #: which a column constrained to the current enum could not do.
    actor_role: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: Structured specifics of this event, e.g. ``{"from": "open", "to":
    #: "in_progress"}``. The extension point the spec asks for: a future module
    #: attaches whatever its events need without a schema change. Never ``NULL`` —
    #: an absent value is an empty object, so no reader has to handle both.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        # The column is named ``metadata`` as the spec specifies; the *attribute*
        # cannot be, because ``Base.metadata`` is SQLAlchemy's table registry and
        # shadowing it would break the declarative mapping.
        "metadata",
        _METADATA_TYPE,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    #: When the event happened. The only timestamp: an append-only row is never
    #: modified, so an ``updated_at`` would be a column that can never change.
    #:
    #: **Assigned by the service, not by the database**, unlike every other
    #: timestamp on the platform. Two reasons, and the second is the one that
    #: matters: the value is the *event's* time rather than the row's, and the
    #: timeline is ordered by it — so it has to be precise enough to separate the
    #: several events one request can publish. ``server_default`` is kept as a
    #: safety net for a row inserted outside the service.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    # -------------------------------------------------------- relationships #

    #: Loaded lazily, unlike the case relationship on :class:`Document`. The
    #: timeline never *displays* the case — it is already scoped to one — so the
    #: only reader is the per-event authorization check, and eager loading would
    #: fetch a case per row for every page nobody looks at it on.
    case: Mapped[Case] = relationship("Case")

    #: The live account behind :attr:`actor_id`, when it still exists. Never used
    #: for display: that is what the snapshot columns are for.
    actor: Mapped[User | None] = relationship("User", foreign_keys=[actor_id])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Title and description can quote a filename or a client's business, so
        # they stay out of reprs and therefore out of any log line that
        # interpolates an event.
        return (
            f"<TimelineEvent id={self.id!s} case_id={self.case_id!s} "
            f"type={self.event_type!r}>"
        )
