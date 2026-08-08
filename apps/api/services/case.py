"""Case management business logic.

Owns the workflow ``06-case-management.md`` defines: creating, listing,
retrieving, updating, assigning, and archiving a legal case.

Scope boundaries, kept deliberately sharp:

* **Capability authorization is not decided here.** Whether the caller may use
  the case-management endpoints is settled by the dependencies in
  :mod:`api.authorization` before a request reaches this service.
* **Per-resource authorization is applied here**, through
  :class:`~services.case_access.CaseAccessPolicy`, because it needs the case row
  — a dependency cannot know whether a lawyer is assigned to a case it has not
  loaded.
* **What it enforces on its own** are the business rules no permission can
  express: case-number uniqueness and generation, legal status transitions, that
  an assignee exists and holds the matching role, that a hearing does not precede
  its filing, and that audit fields come from the authenticated caller rather
  than the request.
* **It publishes to the timeline; it does not implement one.** Every lifecycle
  change here is announced to :class:`~services.timeline.TimelineRecorder` after
  the change has been committed. The timeline module owns storage, presentation,
  and authorization of those events — this module only knows *that* something
  happened and can describe it. Publication never fails a request: see
  :meth:`~services.timeline.TimelineService.record`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError

from core.cases import build_case_number, can_transition
from core.events import DomainEventType, case_topic
from core.exceptions import (
    CaseNotFoundError,
    CaseNumberGenerationError,
    DuplicateCaseNumberError,
    InvalidAssignmentError,
    InvalidCaseDatesError,
    InvalidCaseTransitionError,
)
from core.timeline import humanize
from models.case import Case, CasePriority, CaseStatus
from models.timeline import TimelineEventType
from models.user import User, UserRole
from repositories.case import CaseRepository
from repositories.user import UserRepository
from schemas.case import CaseCreate, CaseListQuery, CaseUpdate
from schemas.errors import ErrorDetail
from services.case_access import ASSIGNMENT_FIELDS, CaseAccessPolicy
from services.events import EventPublisher, NullEventPublisher
from services.timeline import NullTimelineRecorder, TimelineRecorder

logger = structlog.get_logger(__name__)

#: How many times a generated case number is retried after a collision.
#:
#: A collision means another request took the same sequence between this one
#: reading the highest and committing. Retrying re-reads it, so each attempt is
#: strictly more likely to succeed; five is far beyond what any realistic
#: concurrency needs, and exhausting it is a systems problem rather than a
#: client one.
_CASE_NUMBER_ATTEMPTS = 5

#: Which role each assignment field requires of the user being assigned.
#:
#: "Assignments should validate user roles" — a court representative assigned as
#: the case's lawyer would hold the lawyer's access to it without the role that
#: is supposed to carry that access.
_ASSIGNMENT_ROLES: dict[str, UserRole] = {
    "assigned_lawyer_id": UserRole.LAWYER,
    "assigned_court_representative_id": UserRole.COURT_REPRESENTATIVE,
}


@dataclass(frozen=True, slots=True)
class _AssignmentPosition:
    """How one assignment field is announced on the timeline.

    Keyed by the *identifier* column, because that is what a ``CaseUpdate``
    carries, but it also names the relationship — the timeline shows a person's
    name, and a UUID would tell a reader nothing.
    """

    #: Attribute holding the assigned :class:`~models.user.User`, for the name.
    relationship: str
    #: What the position is called in a sentence.
    label: str
    assigned_event: TimelineEventType
    removed_event: TimelineEventType


#: Assignment field → how a change to it appears on the timeline.
#:
#: Derived from the same two fields :data:`_ASSIGNMENT_ROLES` governs, so a third
#: assignment position added to a case would announce itself by adding one entry
#: here rather than by editing the update path.
_ASSIGNMENT_POSITIONS: dict[str, _AssignmentPosition] = {
    "assigned_lawyer_id": _AssignmentPosition(
        relationship="assigned_lawyer",
        label="lawyer",
        assigned_event=TimelineEventType.LAWYER_ASSIGNED,
        removed_event=TimelineEventType.LAWYER_REMOVED,
    ),
    "assigned_court_representative_id": _AssignmentPosition(
        relationship="assigned_court_representative",
        label="court representative",
        assigned_event=TimelineEventType.REPRESENTATIVE_ASSIGNED,
        removed_event=TimelineEventType.REPRESENTATIVE_REMOVED,
    ),
}


#: Case field → how it is named in a timeline description.
#:
#: Only the descriptive fields: status, priority, and the assignments each get
#: their *own* event type, so they never appear in a "Case Updated" sentence.
_FIELD_LABELS: dict[str, str] = {
    "title": "title",
    "description": "description",
    "category": "category",
    "court_name": "court",
    "filing_date": "filing date",
    "next_hearing_date": "next hearing date",
}


def _join_labels(labels: list[str]) -> str:
    """Render a list of field names as an English list ("a, b and c")."""
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


@dataclass(frozen=True, slots=True)
class CasePageResult:
    """One page of cases together with the total matching the filters."""

    cases: list[Case]
    total: int
    page: int
    page_size: int


class CaseService:
    """Coordinates the case lifecycle."""

    def __init__(
        self,
        cases: CaseRepository,
        users: UserRepository,
        access: CaseAccessPolicy | None = None,
        *,
        timeline: TimelineRecorder | None = None,
        events: EventPublisher | None = None,
    ) -> None:
        self._cases = cases
        self._users = users
        self._access = access or CaseAccessPolicy()
        # Defaults to a recorder that records nothing, so this service can be
        # constructed in a context with no timeline (a script, a unit test about
        # something else) without every publication site growing a guard. The
        # application always injects the real one — see `api.deps.get_case_service`,
        # and the test that asserts it does.
        self._timeline: TimelineRecorder = timeline or NullTimelineRecorder()
        # The **event** half, and it is deliberately a second, separate
        # collaborator rather than something derived from the timeline. The two
        # answer different questions and are not substitutes: the timeline is the
        # case's permanent, readable *history*, written to PostgreSQL and read
        # back by a lawyer weeks later; an event is an ephemeral *"this just
        # changed"* delivered to whoever is looking at the case right now.
        # Deriving one from the other would tie a durable audit record to a
        # transport, and would leave a failed timeline write silently costing the
        # user their live update as well.
        self._events: EventPublisher = events or NullEventPublisher()

    # ------------------------------------------------------------- reading #

    def list_cases(self, query: CaseListQuery, *, actor: User) -> CasePageResult:
        """Return one page of cases the caller is entitled to see.

        Search, filtering, sorting, pagination, **and the assignment scope** are
        all applied in the database, so the cost of a page does not grow with the
        caseload and the totals describe only what the caller may access.
        """
        cases, total = self._cases.list_cases(
            query, visible_to=self._access.visibility_scope(actor)
        )
        return CasePageResult(cases=cases, total=total, page=query.page, page_size=query.page_size)

    def get_case(self, case_id: uuid.UUID, *, actor: User) -> Case:
        """Return one case the caller is entitled to see.

        Raises:
            CaseNotFoundError: no case has this identifier.
            CaseAccessDeniedError: the caller is not assigned to it and does not
                hold ``cases:view-all``.
        """
        legal_case = self._require_case(case_id)
        self._access.require_view(actor, legal_case)
        return legal_case

    # ------------------------------------------------------------ creating #

    def create_case(self, payload: CaseCreate, *, actor: User) -> Case:
        """Open a new case.

        The case number is taken from the payload when supplied and generated
        otherwise, as the spec requires. Assignments are validated against the
        assignee's role before anything is written.

        Raises:
            DuplicateCaseNumberError: the supplied case number is already in use.
            InvalidAssignmentError: an assignee does not exist, is disabled, or
                holds the wrong role.
            CaseNumberGenerationError: a unique number could not be generated.
        """
        assignments = {
            field: getattr(payload, field)
            for field in ASSIGNMENT_FIELDS
            if getattr(payload, field) is not None
        }
        self._validate_assignments(assignments)

        if payload.case_number is not None:
            if self._cases.case_number_taken(payload.case_number):
                logger.info("case_create_rejected", reason="duplicate_case_number")
                raise DuplicateCaseNumberError
            created = self._cases.create(self._build_case(payload, payload.case_number, actor))
        else:
            created = self._create_with_generated_number(payload, actor=actor)

        logger.info(
            "case_created",
            case_id=str(created.id),
            # The case *number* identifies the record for an operator without
            # putting the title or description — client-confidential — in a log.
            case_number=created.case_number,
            status=created.status.value,
            priority=created.priority.value,
            assigned_lawyer_id=_optional_id(created.assigned_lawyer_id),
            assigned_court_representative_id=_optional_id(
                created.assigned_court_representative_id
            ),
            actor_id=str(actor.id),
        )

        self._timeline.record(
            case_id=created.id,
            event_type=TimelineEventType.CASE_CREATED,
            actor=actor,
            description=f"{actor.full_name} created case {created.case_number}.",
            metadata={
                "case_number": created.case_number,
                "status": created.status.value,
                "priority": created.priority.value,
                "category": created.category,
            },
        )
        self._announce(
            created,
            DomainEventType.CASE_CREATED,
            actor=actor,
            status=created.status.value,
            priority=created.priority.value,
        )
        # An assignment made at creation is still an assignment. Without these,
        # a case opened with its lawyer already named would carry no record of who
        # put them on it — and "who has been on this case, since when" is the
        # question the assignment events exist to answer.
        for field, position in _ASSIGNMENT_POSITIONS.items():
            if getattr(created, field) is not None:
                self._record_assignment(created, position, actor=actor, previous=None)

        return created

    # ------------------------------------------------------------ updating #

    def update_case(self, case_id: uuid.UUID, payload: CaseUpdate, *, actor: User) -> Case:
        """Apply a partial update to a case.

        Only the fields the client actually sent are written, so a PATCH that
        omits a field leaves it alone while an explicit ``null`` clears it — which
        is how an assignment is removed.

        Three checks run before anything is written, in this order: the caller
        must be entitled to *this* case, then to *these fields*, and only then are
        the values themselves validated. Authorization first means an unentitled
        caller learns nothing about the case's contents from a validation message.

        Raises:
            CaseNotFoundError: no case has this identifier.
            CaseAccessDeniedError: the caller is not entitled to the case, or to
                one of the fields they are writing.
            InvalidCaseTransitionError: the status change is not a legal move.
            InvalidAssignmentError: an assignee does not exist, is disabled, or
                holds the wrong role.
        """
        legal_case = self._require_case(case_id)
        self._access.require_view(actor, legal_case)

        changes = payload.provided_fields()
        self._access.require_writable(actor, changes.keys(), legal_case=legal_case)

        self._validate_status_change(legal_case, changes)
        self._validate_dates(legal_case, changes)
        self._validate_assignments(
            {field: changes[field] for field in ASSIGNMENT_FIELDS & changes.keys()},
            current=legal_case,
        )

        previous_status = legal_case.status
        previous_priority = legal_case.priority
        # Captured *before* the write, because a removal has to name the person
        # who was removed — after the assignment is cleared there is nobody left
        # on the row to name.
        previous_assignees = {
            field: getattr(legal_case, position.relationship)
            for field, position in _ASSIGNMENT_POSITIONS.items()
        }

        for field, value in changes.items():
            setattr(legal_case, field, value)
        legal_case.updated_by = actor.id

        saved = self._cases.save(legal_case)

        logger.info(
            "case_updated",
            case_id=str(saved.id),
            case_number=saved.case_number,
            # Field *names* only: an operator can see what was touched without a
            # case title, a court, or a client's business entering the log.
            fields=sorted(changes),
            actor_id=str(actor.id),
        )
        self._log_status_change(saved, previous_status, actor=actor)
        self._log_assignment_changes(saved, changes, actor=actor)

        self._publish_update(
            saved,
            changes,
            actor=actor,
            previous_status=previous_status,
            previous_priority=previous_priority,
            previous_assignees=previous_assignees,
        )
        return saved

    def archive_case(self, case_id: uuid.UUID, *, actor: User) -> Case:
        """Archive a case (soft delete).

        The row is never removed: documents, timeline entries, hearings, and
        audit records all reference a case, and deleting it would orphan that
        history. An archived case stays searchable, as the spec requires — it
        simply leaves the working set.

        Idempotent — archiving an already-archived case succeeds and leaves it
        archived, so a repeated request is not an error.

        Raises:
            CaseNotFoundError: no case has this identifier.
            CaseAccessDeniedError: the caller is not entitled to this case.
        """
        legal_case = self._require_case(case_id)
        self._access.require_view(actor, legal_case)

        already_archived = legal_case.is_archived
        previous_status = legal_case.status
        legal_case.status = CaseStatus.ARCHIVED
        legal_case.updated_by = actor.id

        saved = self._cases.save(legal_case)

        logger.info(
            "case_archived",
            case_id=str(saved.id),
            case_number=saved.case_number,
            actor_id=str(actor.id),
            already_archived=already_archived,
        )

        # Only when something actually changed. Archiving is idempotent, and a
        # repeated request must not append a second "archived" entry to a history
        # in which nothing happened the second time.
        if not already_archived:
            self._timeline.record(
                case_id=saved.id,
                event_type=TimelineEventType.CASE_ARCHIVED,
                actor=actor,
                description=f"{actor.full_name} archived the case.",
                metadata={"from": previous_status.value, "to": saved.status.value},
            )
            self._announce(
                saved,
                DomainEventType.CASE_ARCHIVED,
                actor=actor,
                previous_status=previous_status.value,
                status=saved.status.value,
            )
        return saved

    # ------------------------------------------------------------- helpers #

    def _announce(
        self,
        legal_case: Case,
        event_type: DomainEventType,
        *,
        actor: User,
        **payload: Any,
    ) -> None:
        """Publish one domain event about this case.

        Every call site sits **beside** a :meth:`TimelineRecorder.record` call
        rather than inside it, so what the case's permanent history says and what
        a connected screen is told stay independently readable — and so the one
        event this feature adds that has *no* timeline entry (there is none in
        this service, but there is in the report agent) does not need a second
        mechanism.

        The payload carries the **case number and the changed values**, and
        deliberately not the title, the description, the court, or the client:
        the number identifies the case for a client that already has it on
        screen, while the rest is the confidential matter itself and belongs in
        the authorized read the client makes next. This is the same line
        `code-standards.md` draws for logs, applied to a wire.
        """
        self._events.publish(
            event_type=event_type,
            topic=case_topic(legal_case.id),
            case_id=legal_case.id,
            actor_id=actor.id,
            payload={"case_number": legal_case.case_number, **payload},
        )

    def _require_case(self, case_id: uuid.UUID) -> Case:
        legal_case = self._cases.get_by_id(case_id)
        if legal_case is None:
            logger.info("case_lookup_failed", case_id=str(case_id))
            raise CaseNotFoundError
        return legal_case

    def _build_case(self, payload: CaseCreate, case_number: str, actor: User) -> Case:
        return Case(
            id=uuid.uuid4(),
            case_number=case_number,
            title=payload.title,
            description=payload.description,
            category=payload.category,
            status=payload.status,
            priority=payload.priority,
            court_name=payload.court_name,
            filing_date=payload.filing_date,
            next_hearing_date=payload.next_hearing_date,
            assigned_lawyer_id=payload.assigned_lawyer_id,
            assigned_court_representative_id=payload.assigned_court_representative_id,
            # Audit fields are populated here, never accepted from the request:
            # a client must not be able to claim someone else filed the case.
            created_by=actor.id,
            updated_by=actor.id,
        )

    def _create_with_generated_number(self, payload: CaseCreate, *, actor: User) -> Case:
        """Create a case under a generated number, retrying on collision.

        The number is derived from the highest already issued for the current
        year, which is a read followed by a write — so two simultaneous creations
        can pick the same sequence. The unique index is what actually guarantees
        uniqueness; this loop turns the resulting ``IntegrityError`` into a fresh
        attempt rather than a failed request.
        """
        year = datetime.now(UTC).year

        for attempt in range(_CASE_NUMBER_ATTEMPTS):
            case_number = build_case_number(
                year, self._cases.highest_generated_sequence(year) + 1
            )
            try:
                return self._cases.create(self._build_case(payload, case_number, actor))
            except IntegrityError:
                # A failed flush leaves the session unusable until it is rolled
                # back, so the retry would fail for the wrong reason without this.
                self._cases.rollback()
                logger.info(
                    "case_number_collision", case_number=case_number, attempt=attempt + 1
                )

        raise CaseNumberGenerationError(
            detail=(
                f"Could not generate a unique case number for {year} "
                f"after {_CASE_NUMBER_ATTEMPTS} attempts."
            )
        )

    def _validate_status_change(self, legal_case: Case, changes: dict[str, Any]) -> None:
        """Refuse a status change that is not a legal move.

        Re-submitting the current status is not a transition, so a form that
        round-trips every field still works — the same allowance the user
        service makes for an unchanged role.
        """
        target = changes.get("status")
        if not isinstance(target, CaseStatus) or can_transition(legal_case.status, target):
            return

        logger.info(
            "case_update_rejected",
            reason="invalid_transition",
            case_id=str(legal_case.id),
            current=legal_case.status.value,
            target=target.value,
        )
        raise InvalidCaseTransitionError(legal_case.status.value, target.value)

    @staticmethod
    def _validate_dates(legal_case: Case, changes: dict[str, Any]) -> None:
        """Refuse a hearing scheduled before the case was filed.

        The schema can only check this when a request carries *both* dates. A
        PATCH that moves one of them has to be compared against the stored case,
        which is the only place the other value is known.
        """
        filing = changes.get("filing_date", legal_case.filing_date)
        hearing = changes.get("next_hearing_date", legal_case.next_hearing_date)

        if isinstance(filing, date) and isinstance(hearing, date) and hearing < filing:
            logger.info(
                "case_update_rejected", reason="hearing_before_filing", case_id=str(legal_case.id)
            )
            raise InvalidCaseDatesError(
                details=[
                    ErrorDetail(
                        field="next_hearing_date",
                        message="The next hearing date cannot be before the filing date.",
                    )
                ]
            )

    def _validate_assignments(
        self, assignments: dict[str, Any], *, current: Case | None = None
    ) -> None:
        """Check that each assignee exists, is active, and holds the matching role.

        An unchanged assignment is not re-checked: a case whose lawyer was later
        deactivated must still be editable in every other respect, and refusing
        that would make an unrelated field impossible to correct.

        Raises:
            InvalidAssignmentError: an assignee is unknown, disabled, or holds the
                wrong role.
        """
        for field, user_id in assignments.items():
            if user_id is None:
                # Removing an assignment: nothing to validate.
                continue
            if current is not None and getattr(current, field) == user_id:
                continue

            required_role = _ASSIGNMENT_ROLES[field]
            assignee = self._users.get_by_id(user_id)

            if assignee is None or not assignee.is_active or assignee.role is not required_role:
                logger.info(
                    "case_assignment_rejected",
                    field=field,
                    assignee_id=str(user_id),
                    # Which of the three conditions failed, without confirming to
                    # the caller whether the identifier belongs to a real account.
                    reason=(
                        "unknown"
                        if assignee is None
                        else "inactive"
                        if not assignee.is_active
                        else "wrong_role"
                    ),
                )
                raise InvalidAssignmentError(
                    f"The assigned user must be an active account with the "
                    f"{required_role.value!r} role.",
                    details=[
                        ErrorDetail(
                            field=field,
                            message="This user cannot be assigned to that position.",
                        )
                    ],
                )

    # ------------------------------------------------------- timeline events #
    #
    # Everything below turns a committed change into the events that describe it.
    # None of it decides anything: by the time these run the case is already
    # saved, and a failure to record is swallowed by the recorder rather than
    # failing the request that succeeded.

    def _publish_update(
        self,
        legal_case: Case,
        changes: dict[str, Any],
        *,
        actor: User,
        previous_status: CaseStatus,
        previous_priority: CasePriority,
        previous_assignees: dict[str, User | None],
    ) -> None:
        """Announce an update as the specific events it consists of.

        One PATCH can be several events, because the spec gives status, priority,
        and each assignment their own type. A generic "Case Updated" is recorded
        **only** for the descriptive fields left over, so a request that merely
        moved the status does not also claim the case was edited.
        """
        self._publish_status_change(legal_case, previous_status, actor=actor)
        self._publish_priority_change(legal_case, previous_priority, actor=actor)

        for field in sorted(ASSIGNMENT_FIELDS & changes.keys()):
            self._record_assignment(
                legal_case,
                _ASSIGNMENT_POSITIONS[field],
                actor=actor,
                previous=previous_assignees[field],
            )

        descriptive = sorted(
            _FIELD_LABELS[field] for field in changes if field in _FIELD_LABELS
        )
        if descriptive:
            self._timeline.record(
                case_id=legal_case.id,
                event_type=TimelineEventType.CASE_UPDATED,
                actor=actor,
                description=f"{actor.full_name} updated the {_join_labels(descriptive)}.",
                metadata={"fields": descriptive},
            )
            # The field *labels*, never their values: "the title and the court
            # name changed" is what tells a client to refetch, while the new
            # title is the confidential matter it refetches under authorization.
            self._announce(
                legal_case, DomainEventType.CASE_UPDATED, actor=actor, fields=descriptive
            )

    def _publish_status_change(
        self, legal_case: Case, previous: CaseStatus, *, actor: User
    ) -> None:
        """Announce a lifecycle move, as the most specific event that fits.

        Archiving and restoring are status changes, but the spec names them as
        their own event types — and they are what a reader actually wants to see
        called out, so they take precedence over the generic one.
        """
        if legal_case.status is previous:
            return

        if legal_case.status is CaseStatus.ARCHIVED:
            event_type = TimelineEventType.CASE_ARCHIVED
            description = f"{actor.full_name} archived the case."
        elif previous is CaseStatus.ARCHIVED:
            event_type = TimelineEventType.CASE_RESTORED
            description = (
                f"{actor.full_name} restored the case to "
                f"{humanize(legal_case.status.value)}."
            )
        else:
            event_type = TimelineEventType.STATUS_CHANGED
            description = (
                f"{actor.full_name} changed the status from {humanize(previous.value)} "
                f"to {humanize(legal_case.status.value)}."
            )

        self._timeline.record(
            case_id=legal_case.id,
            event_type=event_type,
            actor=actor,
            description=description,
            metadata={"from": previous.value, "to": legal_case.status.value},
        )
        # The domain vocabulary makes the same three-way distinction the timeline
        # does, and for the same reason: a client that has to infer "archived"
        # from a status field is a client that will get it wrong the first time a
        # status is added.
        if legal_case.status is CaseStatus.ARCHIVED:
            announced = DomainEventType.CASE_ARCHIVED
        elif previous is CaseStatus.ARCHIVED:
            announced = DomainEventType.CASE_RESTORED
        else:
            announced = DomainEventType.CASE_STATUS_CHANGED

        self._announce(
            legal_case,
            announced,
            actor=actor,
            previous_status=previous.value,
            status=legal_case.status.value,
        )

    def _publish_priority_change(
        self, legal_case: Case, previous: CasePriority, *, actor: User
    ) -> None:
        """Announce a priority change."""
        if legal_case.priority is previous:
            return

        self._timeline.record(
            case_id=legal_case.id,
            event_type=TimelineEventType.PRIORITY_CHANGED,
            actor=actor,
            description=(
                f"{actor.full_name} changed the priority from {humanize(previous.value)} "
                f"to {humanize(legal_case.priority.value)}."
            ),
            metadata={"from": previous.value, "to": legal_case.priority.value},
        )
        self._announce(
            legal_case,
            DomainEventType.CASE_PRIORITY_CHANGED,
            actor=actor,
            previous_priority=previous.value,
            priority=legal_case.priority.value,
        )

    def _record_assignment(
        self,
        legal_case: Case,
        position: _AssignmentPosition,
        *,
        actor: User,
        previous: User | None,
    ) -> None:
        """Announce one assignment position changing.

        Names the person rather than their identifier — a timeline entry reading
        "assigned 7f3a…" would tell nobody anything. A removal names whoever was
        there *before*, which is why the caller captures that ahead of the write.
        """
        assignee: User | None = getattr(legal_case, position.relationship)

        if assignee is not None:
            self._timeline.record(
                case_id=legal_case.id,
                event_type=position.assigned_event,
                actor=actor,
                description=(
                    f"{actor.full_name} assigned {assignee.full_name} as the {position.label}."
                ),
                metadata={
                    "assignee_id": assignee.id,
                    "assignee_name": assignee.full_name,
                    "position": position.label,
                    "previous_assignee_name": previous.full_name if previous else None,
                },
            )
            # The assignee's **identifier**, never their name. The timeline names
            # people because it is read by the people on the matter; this reaches
            # every screen following the case, and a name is personal data that
            # the authorized read after it will supply anyway.
            self._announce(
                legal_case,
                DomainEventType.CASE_ASSIGNMENT_CHANGED,
                actor=actor,
                position=position.label,
                assignee_id=assignee.id,
                assigned=True,
            )
            return

        # Cleared. If there was nobody there to begin with, the request changed
        # nothing worth recording.
        if previous is None:
            return

        self._timeline.record(
            case_id=legal_case.id,
            event_type=position.removed_event,
            actor=actor,
            description=(
                f"{actor.full_name} removed {previous.full_name} as the {position.label}."
            ),
            metadata={
                "assignee_id": previous.id,
                "assignee_name": previous.full_name,
                "position": position.label,
            },
        )
        self._announce(
            legal_case,
            DomainEventType.CASE_ASSIGNMENT_CHANGED,
            actor=actor,
            position=position.label,
            assignee_id=previous.id,
            assigned=False,
        )

    @staticmethod
    def _log_status_change(legal_case: Case, previous: CaseStatus, *, actor: User) -> None:
        """Record a status change as its own event.

        Separate from ``case_updated`` because a lifecycle change is what
        notifications, the timeline, and reporting will each react to — the spec
        lists it as a distinct thing to log, and future modules subscribe to it
        rather than parsing a field list.
        """
        if legal_case.status is previous:
            return

        logger.info(
            "case_status_changed",
            case_id=str(legal_case.id),
            case_number=legal_case.case_number,
            previous=previous.value,
            current=legal_case.status.value,
            actor_id=str(actor.id),
        )

    @staticmethod
    def _log_assignment_changes(
        legal_case: Case, changes: dict[str, Any], *, actor: User
    ) -> None:
        """Record each assignment change as its own event."""
        for field in sorted(ASSIGNMENT_FIELDS & changes.keys()):
            logger.info(
                "case_assignment_changed",
                case_id=str(legal_case.id),
                case_number=legal_case.case_number,
                field=field,
                assignee_id=_optional_id(getattr(legal_case, field)),
                actor_id=str(actor.id),
            )


def _optional_id(value: uuid.UUID | None) -> str | None:
    """Render an optional identifier for a log entry."""
    return str(value) if value is not None else None
