"""Timeline and audit-trail service.

Owns what ``08-timeline.md`` defines: recording the significant events that
happen to a case, and serving them back as a chronological history.

**This service contains no business logic, by design.** It never decides that a
case may be archived or that a document may be replaced — it is *told* that those
things happened, by the services that own those rules:

    Case Service      → create case     → TimelineService.record(...)
    Document Service  → upload document → TimelineService.record(...)

:meth:`TimelineService.record` is the reusable method the spec asks for, and it is
deliberately generic: a case identifier, a registry event type, an actor, and a
free-form ``metadata`` object. A future module publishes by adding one member to
:class:`~models.timeline.TimelineEventType` and calling this — nothing in the
timeline implementation changes.

Scope boundaries, kept deliberately sharp:

* **Capability authorization is not decided here.** Whether the caller may use
  the timeline endpoints is settled by the dependencies in
  :mod:`api.authorization` before a request reaches this service.
* **Per-resource authorization is applied here**, through
  :class:`~services.timeline_access.TimelineAccessPolicy`, because it needs the
  case row — a dependency cannot know whether a lawyer is assigned to a case it
  has not loaded.
* **Application logging is a separate concern.** The spec is explicit that
  application logs stay for debugging and must not be duplicated into the
  timeline. Timeline events are business facts a lawyer reads; the structured
  logs the case and document services already emit are for an operator. Neither
  is derived from the other.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from core.exceptions import CaseNotFoundError, TimelineEventNotFoundError
from core.timeline import (
    InvalidTimelineMetadataError,
    default_title,
    normalize_actor_name,
    normalize_description,
    normalize_metadata,
    normalize_title,
)
from models.case import Case
from models.timeline import TimelineEvent, TimelineEventType
from models.user import User
from repositories.case import CaseRepository
from repositories.timeline import TimelineRepository
from schemas.timeline import TimelineListQuery
from services.timeline_access import TimelineAccessPolicy

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TimelinePageResult:
    """One page of timeline events together with the total matching the filters."""

    events: list[TimelineEvent]
    total: int
    page: int
    page_size: int


class TimelineRecorder(Protocol):
    """The narrow half of the timeline a publishing module depends on.

    Case Management and Document Management need to *record*; they have no
    business reading, filtering, or authorizing a timeline. Depending on this
    protocol rather than on :class:`TimelineService` states that in the type
    system, and lets a unit test for those modules substitute a recorder without
    a database.
    """

    def record(
        self,
        *,
        case_id: uuid.UUID,
        event_type: TimelineEventType,
        actor: User | None = None,
        title: str | None = None,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TimelineEvent | None:
        """Record one event against a case."""
        ...


class NullTimelineRecorder:
    """A recorder that records nothing.

    The default for a service constructed without a timeline — a script, a
    migration helper, or a unit test that is not about the timeline. It exists so
    publishing code can be written as a plain call with no ``if self._timeline``
    guard at every site, which is where a forgotten branch would silently drop
    events.

    The application wires the real service in :mod:`api.deps`; a test asserts that
    it does, so "the default is a no-op" cannot quietly become the production
    behaviour.
    """

    def record(
        self,
        *,
        case_id: uuid.UUID,
        event_type: TimelineEventType,
        actor: User | None = None,
        title: str | None = None,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TimelineEvent | None:
        """Discard the event and return ``None``."""
        return None


class TimelineService:
    """Records and serves case activity."""

    def __init__(
        self,
        events: TimelineRepository,
        cases: CaseRepository,
        access: TimelineAccessPolicy | None = None,
    ) -> None:
        self._events = events
        self._cases = cases
        self._access = access or TimelineAccessPolicy()
        #: The last timestamp this instance issued — see :meth:`_next_timestamp`.
        self._last_stamp: datetime | None = None

    # ----------------------------------------------------------- recording #

    def record(
        self,
        *,
        case_id: uuid.UUID,
        event_type: TimelineEventType,
        actor: User | None = None,
        title: str | None = None,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TimelineEvent | None:
        """Append one event to a case's timeline.

        The reusable entry point every publishing module calls. Nothing about it
        is case- or document-specific: ``event_type`` names what happened,
        ``description`` is the sentence a user reads, and ``metadata`` carries
        whatever structured specifics that event has.

        Args:
            case_id: the case the event belongs to. Not validated against the
                ``cases`` table — the publisher has just read or written that row,
                and a second lookup here would cost a query per event to re-prove
                something the caller already knows.
            event_type: a member of the central registry.
            actor: who acted. Their name and role are **snapshotted** onto the
                event, so the history reads as it did when it happened. ``None``
                for an event with no user behind it.
            title: overrides the registry's default headline.
            description: the human sentence, e.g. ``Amina Benali uploaded
                "Contract.pdf"``.
            metadata: structured specifics; normalised to a JSON-safe object.

        Returns:
            The recorded event, or ``None`` if it could not be written.

        **Never raises.** A failure to record must not fail the operation that
        caused it: the business change is already committed by the time this is
        called, so raising here would answer a successful request with a 500 and
        invite a retry that would duplicate the work. The failure is logged at
        error level instead — and the structured application log for the
        underlying operation (``case_created``, ``document_uploaded``, …) is
        emitted independently, so the operational record survives even when the
        user-facing entry does not.
        """
        try:
            event = TimelineEvent(
                id=uuid.uuid4(),
                case_id=case_id,
                created_at=self._next_timestamp(),
                event_type=event_type.value,
                title=normalize_title(title or default_title(event_type.value)),
                description=normalize_description(description),
                actor_id=actor.id if actor is not None else None,
                actor_name=normalize_actor_name(actor.full_name) if actor is not None else None,
                actor_role=actor.role.value if actor is not None else None,
                event_metadata=self._safe_metadata(event_type, metadata),
            )
            recorded = self._events.add(event)
        except Exception:
            self._events.rollback()
            logger.error(
                "timeline_event_write_failed",
                case_id=str(case_id),
                event_type=event_type.value,
                actor_id=str(actor.id) if actor is not None else None,
            )
            return None

        logger.info(
            "timeline_event_recorded",
            # Identifiers and the event type only. The title and description are
            # business text — they can quote a filename or name a client — and the
            # spec keeps application logs and the timeline separate precisely so
            # one does not become a copy of the other.
            event_id=str(recorded.id),
            case_id=str(case_id),
            event_type=event_type.value,
            actor_id=str(actor.id) if actor is not None else None,
        )
        return recorded

    # ------------------------------------------------------------- reading #

    def list_case_timeline(
        self, case_id: uuid.UUID, query: TimelineListQuery, *, actor: User
    ) -> TimelinePageResult:
        """Return one page of a case's timeline.

        The case is loaded and authorized first, so a caller who is not party to
        it is refused rather than handed an empty page — an empty page would be
        indistinguishable from a case with no history, and would leak that the
        identifier exists only through timing.

        Raises:
            CaseNotFoundError: no case has this identifier.
            TimelineAccessDeniedError: the caller is not party to the case.
        """
        legal_case = self._require_case(case_id)
        self._access.require_case_access(actor, legal_case)

        events, total = self._events.list_events(query, case_id=legal_case.id)
        return TimelinePageResult(
            events=events, total=total, page=query.page, page_size=query.page_size
        )

    def get_event(self, event_id: uuid.UUID, *, actor: User) -> TimelineEvent:
        """Return one timeline event the caller is entitled to see.

        Raises:
            TimelineEventNotFoundError: no event has this identifier.
            TimelineAccessDeniedError: the caller is not party to its case.
        """
        event = self._events.get_by_id(event_id)
        if event is None:
            logger.info("timeline_event_lookup_failed", event_id=str(event_id))
            raise TimelineEventNotFoundError

        self._access.require_view(actor, event)
        return event

    # ------------------------------------------------------------- helpers #

    def _next_timestamp(self) -> datetime:
        """The event's time, guaranteed to increase within this service instance.

        Stamped here rather than by the database, and *monotonically*, because the
        timeline is ordered by this column and **one request can publish several
        events** — a PATCH that moves the status, changes the priority, and
        re-assigns the lawyer publishes three, and they have to read back in that
        order.

        Neither a database default nor a bare ``datetime.now()`` can promise that:

        * PostgreSQL's ``now()`` returns the *transaction's* start time, and
          SQLite's ``CURRENT_TIMESTAMP`` has one-second resolution;
        * ``datetime.now()`` is only as fine-grained as the platform clock — on
          Windows consecutive calls routinely return the same value — and the
          primary-key tiebreaker is a random UUID, so a tie orders the events
          arbitrarily. That is history arriving shuffled, and it is not
          hypothetical: it is what this method was written to fix.

        A service instance is per request (see :func:`api.deps.get_timeline_service`,
        whose result FastAPI caches for the request, so the case and document
        services share one), which is exactly the scope where ordering is
        guaranteed and needed. Across requests the wall clock is authority, and two
        genuinely concurrent operations have no meaningful order to preserve —
        while the repository's tiebreaker still keeps their pagination stable.

        The adjustment is at most a microsecond per event, so the recorded time
        stays accurate to far better than anything the timeline displays.
        """
        now = datetime.now(UTC)
        if self._last_stamp is not None and now <= self._last_stamp:
            now = self._last_stamp + timedelta(microseconds=1)
        self._last_stamp = now
        return now

    def _require_case(self, case_id: uuid.UUID) -> Case:
        legal_case = self._cases.get_by_id(case_id)
        if legal_case is None:
            logger.info("timeline_case_lookup_failed", case_id=str(case_id))
            raise CaseNotFoundError
        return legal_case

    @staticmethod
    def _safe_metadata(
        event_type: TimelineEventType, metadata: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Normalise metadata, degrading to an empty object rather than losing the event.

        Oversized metadata is a fault in the *publisher*, not in the user's
        request. Dropping the specifics keeps the event — which is what an audit
        trail most needs — and the rejection is logged so the publisher's bug is
        visible.
        """
        try:
            return normalize_metadata(metadata)
        except InvalidTimelineMetadataError as exc:
            logger.error(
                "timeline_metadata_rejected", event_type=event_type.value, reason=str(exc)
            )
            return {}
