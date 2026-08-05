"""Unit tests for :mod:`services.timeline`.

Two halves:

* **Recording** — what :meth:`TimelineService.record` writes, what it snapshots,
  and its promise never to fail the operation that caused it.
* **Reading** — search, filtering, sorting, and pagination, all of which execute
  against the real repository on SQLite, plus the per-case authorization.

The third half — that the case and document services actually *publish* — is in
``TestAutomaticPublication`` at the bottom, wired the way ``api.deps`` wires it.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy.orm import Session

from core.exceptions import (
    CaseNotFoundError,
    TimelineAccessDeniedError,
    TimelineEventNotFoundError,
)
from core.timeline import MAX_METADATA_BYTES
from models.case import Case, CasePriority, CaseStatus
from models.timeline import TimelineEvent, TimelineEventType
from models.user import User, UserRole
from repositories.case import CaseRepository
from repositories.document import DocumentRepository
from repositories.timeline import TimelineRepository
from repositories.user import UserRepository
from schemas.case import CaseCreate, CaseUpdate, SortOrder
from schemas.document import DocumentUpdate, DocumentUploadForm
from schemas.timeline import TimelineListQuery
from services.case import CaseService
from services.document import DocumentService
from services.document_storage import DocumentStorageService
from services.document_validation import validate_upload
from services.timeline import NullTimelineRecorder, TimelineService
from tests.helpers import PDF_BYTES

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeEvent = Callable[..., TimelineEvent]


@pytest.fixture
def administrator(make_user: MakeUser) -> User:
    return make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(
        email="lawyer@example.com",
        first_name="Sarah",
        last_name="Smith",
        role=UserRole.LAWYER,
    )


@pytest.fixture
def court(make_user: MakeUser) -> User:
    return make_user(
        email="court@example.com",
        first_name="Karim",
        last_name="Ziani",
        role=UserRole.COURT_REPRESENTATIVE,
    )


def _query(**overrides: object) -> TimelineListQuery:
    return TimelineListQuery(**overrides)  # type: ignore[arg-type]


def _types(events: list[TimelineEvent]) -> list[str]:
    return [event.event_type for event in events]


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


class TestRecord:
    def test_it_appends_an_event_with_the_registry_s_default_title(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        event = timeline_service.record(
            case_id=legal_case.id,
            event_type=TimelineEventType.DOCUMENT_UPLOADED,
            actor=administrator,
            description='Amina Benali uploaded "Contract.pdf".',
        )

        assert event is not None
        assert event.event_type == "document_uploaded"
        assert event.title == "Document Uploaded"
        assert event.description == 'Amina Benali uploaded "Contract.pdf".'
        assert event.case_id == legal_case.id

    def test_a_supplied_title_overrides_the_default(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        event = timeline_service.record(
            case_id=make_case().id,
            event_type=TimelineEventType.CASE_UPDATED,
            actor=administrator,
            title="Hearing Rescheduled",
        )

        assert event is not None
        assert event.title == "Hearing Rescheduled"

    def test_it_snapshots_the_actor_s_name_and_role(
        self, timeline_service: TimelineService, lawyer: User, make_case: MakeCase
    ) -> None:
        event = timeline_service.record(
            case_id=make_case().id,
            event_type=TimelineEventType.DOCUMENT_UPLOADED,
            actor=lawyer,
        )

        assert event is not None
        assert event.actor_id == lawyer.id
        assert event.actor_name == "Sarah Smith"
        assert event.actor_role == "lawyer"

    def test_the_snapshot_does_not_follow_a_later_rename(
        self,
        db_session: Session,
        timeline_service: TimelineService,
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        # The whole reason the actor is denormalised: renaming a user must not
        # rewrite what the history says happened.
        event = timeline_service.record(
            case_id=make_case().id,
            event_type=TimelineEventType.CASE_UPDATED,
            actor=lawyer,
        )
        assert event is not None

        lawyer.first_name = "Sara"
        lawyer.last_name = "Smyth"
        db_session.commit()
        db_session.refresh(event)

        assert event.actor_name == "Sarah Smith"

    def test_an_event_with_no_actor_is_allowed(
        self, timeline_service: TimelineService, make_case: MakeCase
    ) -> None:
        # Reserved for a future scheduled job with no user behind it.
        event = timeline_service.record(
            case_id=make_case().id, event_type=TimelineEventType.CASE_UPDATED
        )

        assert event is not None
        assert event.actor_id is None
        assert event.actor_name is None
        assert event.actor_role is None

    def test_metadata_is_normalised_to_json_safe_values(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        document_id = uuid.uuid4()

        event = timeline_service.record(
            case_id=make_case().id,
            event_type=TimelineEventType.DOCUMENT_UPLOADED,
            actor=administrator,
            metadata={"document_id": document_id, "category": None, "version": 1},
        )

        assert event is not None
        assert event.event_metadata == {"document_id": str(document_id), "version": 1}

    def test_absent_metadata_is_stored_as_an_empty_object(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        event = timeline_service.record(
            case_id=make_case().id,
            event_type=TimelineEventType.CASE_CREATED,
            actor=administrator,
        )

        assert event is not None
        assert event.event_metadata == {}

    def test_oversized_metadata_loses_the_specifics_but_keeps_the_event(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        # A publisher's bug must not cost the audit trail the event itself.
        event = timeline_service.record(
            case_id=make_case().id,
            event_type=TimelineEventType.CASE_UPDATED,
            actor=administrator,
            metadata={"blob": "x" * (MAX_METADATA_BYTES + 10)},
        )

        assert event is not None
        assert event.event_metadata == {}

    def test_a_write_failure_returns_none_rather_than_raising(
        self, db_session: Session, administrator: User, make_case: MakeCase
    ) -> None:
        # The business change is already committed by the time this runs, so
        # raising would answer a successful request with a 500.
        class ExplodingRepository(TimelineRepository):
            def add(self, event: TimelineEvent) -> TimelineEvent:
                raise RuntimeError("database is on fire")

        service = TimelineService(
            ExplodingRepository(db_session), CaseRepository(db_session)
        )

        assert (
            service.record(
                case_id=make_case().id,
                event_type=TimelineEventType.CASE_CREATED,
                actor=administrator,
            )
            is None
        )

    def test_an_unknown_case_id_is_not_re_validated(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        # Deliberate: the publisher has just read or written that row, and a
        # second lookup would cost a query per event to re-prove it. SQLite does
        # not enforce foreign keys by default, so this documents the contract
        # rather than the storage.
        assert (
            timeline_service.record(
                case_id=uuid.uuid4(),
                event_type=TimelineEventType.CASE_CREATED,
                actor=administrator,
            )
            is not None
        )


class TestOrderingWithinOneRequest:
    """The timestamp must separate events published back-to-back.

    Found by a flaky test rather than by design: the platform clock is only so
    fine-grained (on Windows, consecutive ``datetime.now()`` calls routinely
    return the same value), and the repository's tiebreaker is a random UUID — so
    tied events came back shuffled. History arriving in the wrong order is a real
    defect, not a test artefact.
    """

    def test_consecutive_events_get_strictly_increasing_timestamps(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        stamps = [
            timeline_service.record(
                case_id=legal_case.id,
                event_type=TimelineEventType.CASE_UPDATED,
                actor=administrator,
            )
            for _ in range(25)
        ]

        times = [event.created_at for event in stamps if event is not None]
        assert len(times) == 25
        assert times == sorted(times)
        assert len(set(times)) == 25, "two events shared a timestamp"

    def test_the_adjustment_stays_within_a_hair_of_the_real_time(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        # At most a microsecond per event, so the recorded time stays accurate to
        # far better than anything the timeline displays.
        legal_case = make_case()
        before = datetime.now(UTC)

        events = [
            timeline_service.record(
                case_id=legal_case.id,
                event_type=TimelineEventType.CASE_UPDATED,
                actor=administrator,
            )
            for _ in range(10)
        ]

        last = events[-1]
        assert last is not None

        # SQLite has no timezone-aware type, so a value round-tripped through it
        # comes back naive — a property of the test database, not of the column,
        # which is `timestamptz` on PostgreSQL.
        stored = last.created_at
        reference = before if stored.tzinfo is not None else before.replace(tzinfo=None)
        assert abs((stored - reference).total_seconds()) < 1

    def test_events_published_across_one_update_read_back_in_order(
        self,
        db_session: Session,
        timeline_service: TimelineService,
        administrator: User,
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        # The concrete case: one PATCH publishes status, priority, assignment, and
        # a generic update, and they must read chronologically.
        cases = CaseService(
            CaseRepository(db_session),
            UserRepository(db_session),
            timeline=timeline_service,
        )
        legal_case = make_case(status=CaseStatus.OPEN, priority=CasePriority.LOW)

        cases.update_case(
            legal_case.id,
            CaseUpdate(
                title="Benali v. Atlas SARL",
                status=CaseStatus.IN_PROGRESS,
                priority=CasePriority.HIGH,
                assigned_lawyer_id=lawyer.id,
            ),
            actor=administrator,
        )

        events = timeline_service.list_case_timeline(
            legal_case.id, _query(page_size=100, sort_order=SortOrder.ASC), actor=administrator
        ).events

        assert _types(events) == [
            "status_changed",
            "priority_changed",
            "lawyer_assigned",
            "case_updated",
        ]


class TestNullTimelineRecorder:
    def test_it_records_nothing(self, administrator: User) -> None:
        assert (
            NullTimelineRecorder().record(
                case_id=uuid.uuid4(),
                event_type=TimelineEventType.CASE_CREATED,
                actor=administrator,
            )
            is None
        )


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


class TestGetEvent:
    def test_it_returns_an_event_the_caller_may_reach(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        event = make_timeline_event(case_id=make_case().id)

        assert timeline_service.get_event(event.id, actor=administrator).id == event.id

    def test_an_unknown_identifier_is_a_404(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        with pytest.raises(TimelineEventNotFoundError):
            timeline_service.get_event(uuid.uuid4(), actor=administrator)

    def test_an_unassigned_lawyer_is_refused(
        self,
        timeline_service: TimelineService,
        lawyer: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        event = make_timeline_event(case_id=make_case().id)

        with pytest.raises(TimelineAccessDeniedError):
            timeline_service.get_event(event.id, actor=lawyer)


class TestListCaseTimeline:
    def test_an_unknown_case_is_a_404(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        with pytest.raises(CaseNotFoundError):
            timeline_service.list_case_timeline(uuid.uuid4(), _query(), actor=administrator)

    def test_an_unassigned_lawyer_is_refused_rather_than_given_an_empty_page(
        self, timeline_service: TimelineService, lawyer: User, make_case: MakeCase
    ) -> None:
        # An empty page would be indistinguishable from a case with no history.
        with pytest.raises(TimelineAccessDeniedError):
            timeline_service.list_case_timeline(make_case().id, _query(), actor=lawyer)

    def test_an_assigned_lawyer_reads_their_case_s_timeline(
        self,
        timeline_service: TimelineService,
        lawyer: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)
        make_timeline_event(case_id=legal_case.id)

        result = timeline_service.list_case_timeline(legal_case.id, _query(), actor=lawyer)

        assert result.total == 1

    def test_it_returns_only_the_requested_case_s_events(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        first = make_case()
        second = make_case()
        make_timeline_event(case_id=first.id)
        make_timeline_event(case_id=first.id)
        make_timeline_event(case_id=second.id)

        result = timeline_service.list_case_timeline(first.id, _query(), actor=administrator)

        assert result.total == 2
        assert {event.case_id for event in result.events} == {first.id}

    def test_it_defaults_to_reverse_chronological_order(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case()
        base = datetime(2026, 7, 1, tzinfo=UTC)
        make_timeline_event(case_id=legal_case.id, description="oldest", created_at=base)
        make_timeline_event(
            case_id=legal_case.id, description="middle", created_at=base + timedelta(days=1)
        )
        make_timeline_event(
            case_id=legal_case.id, description="newest", created_at=base + timedelta(days=2)
        )

        result = timeline_service.list_case_timeline(legal_case.id, _query(), actor=administrator)

        assert [event.description for event in result.events] == ["newest", "middle", "oldest"]

    def test_ascending_order_is_supported(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case()
        base = datetime(2026, 7, 1, tzinfo=UTC)
        make_timeline_event(case_id=legal_case.id, description="oldest", created_at=base)
        make_timeline_event(
            case_id=legal_case.id, description="newest", created_at=base + timedelta(days=2)
        )

        result = timeline_service.list_case_timeline(
            legal_case.id, _query(sort_order=SortOrder.ASC), actor=administrator
        )

        assert [event.description for event in result.events] == ["oldest", "newest"]

    def test_events_sharing_a_timestamp_keep_a_stable_order(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        # One request publishes several events at the same instant; without the
        # primary-key tiebreaker they could be duplicated or skipped across a
        # page boundary, which on a timeline reads as history changing.
        legal_case = make_case()
        at = datetime(2026, 7, 31, 14, 32, tzinfo=UTC)
        for index in range(6):
            make_timeline_event(
                case_id=legal_case.id, description=f"event {index}", created_at=at
            )

        first = timeline_service.list_case_timeline(
            legal_case.id, _query(page=1, page_size=3), actor=administrator
        )
        second = timeline_service.list_case_timeline(
            legal_case.id, _query(page=2, page_size=3), actor=administrator
        )

        ids = [event.id for event in first.events] + [event.id for event in second.events]
        assert len(set(ids)) == 6


class TestSearch:
    @pytest.fixture(autouse=True)
    def _events(self, make_case: MakeCase, make_timeline_event: MakeEvent) -> Case:
        self.legal_case = make_case()
        make_timeline_event(
            case_id=self.legal_case.id,
            event_type=TimelineEventType.DOCUMENT_UPLOADED,
            description='Amina Benali uploaded "Contract.pdf".',
        )
        make_timeline_event(
            case_id=self.legal_case.id,
            event_type=TimelineEventType.STATUS_CHANGED,
            description="Amina Benali changed the status from Open to In progress.",
        )
        return self.legal_case

    def test_it_matches_the_description_case_insensitively(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        result = timeline_service.list_case_timeline(
            self.legal_case.id, _query(search="CONTRACT.PDF"), actor=administrator
        )

        assert result.total == 1

    def test_it_matches_the_title(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        result = timeline_service.list_case_timeline(
            self.legal_case.id, _query(search="status"), actor=administrator
        )

        assert result.total == 1
        assert result.events[0].title == "Status Changed"

    def test_a_wildcard_in_the_term_is_matched_literally(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        # An unescaped % would match everything.
        result = timeline_service.list_case_timeline(
            self.legal_case.id, _query(search="%"), actor=administrator
        )

        assert result.total == 0

    def test_an_underscore_in_the_term_is_matched_literally(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        result = timeline_service.list_case_timeline(
            self.legal_case.id, _query(search="_"), actor=administrator
        )

        assert result.total == 0


class TestFilters:
    @pytest.fixture(autouse=True)
    def _events(
        self,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
        administrator: User,
        lawyer: User,
    ) -> None:
        self.legal_case = make_case()
        base = datetime(2026, 7, 1, tzinfo=UTC)
        make_timeline_event(
            case_id=self.legal_case.id,
            event_type=TimelineEventType.CASE_CREATED,
            actor=administrator,
            created_at=base,
        )
        make_timeline_event(
            case_id=self.legal_case.id,
            event_type=TimelineEventType.DOCUMENT_UPLOADED,
            actor=lawyer,
            created_at=base + timedelta(days=10),
        )
        make_timeline_event(
            case_id=self.legal_case.id,
            event_type=TimelineEventType.DOCUMENT_UPLOADED,
            actor=administrator,
            created_at=base + timedelta(days=20),
        )

    def test_it_filters_by_event_type(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        result = timeline_service.list_case_timeline(
            self.legal_case.id, _query(event_type="document_uploaded"), actor=administrator
        )

        assert result.total == 2

    def test_it_filters_by_actor(
        self, timeline_service: TimelineService, administrator: User, lawyer: User
    ) -> None:
        result = timeline_service.list_case_timeline(
            self.legal_case.id, _query(actor_id=lawyer.id), actor=administrator
        )

        assert result.total == 1

    def test_it_filters_by_date_range(
        self, timeline_service: TimelineService, administrator: User
    ) -> None:
        result = timeline_service.list_case_timeline(
            self.legal_case.id,
            _query(date_from="2026-07-05", date_to="2026-07-15"),
            actor=administrator,
        )

        assert result.total == 1

    def test_the_upper_bound_covers_the_whole_day(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_timeline_event: MakeEvent,
    ) -> None:
        # `<= 2026-08-01` must include an event recorded at 14:32 that day.
        make_timeline_event(
            case_id=self.legal_case.id,
            created_at=datetime(2026, 8, 1, 14, 32, tzinfo=UTC),
        )

        result = timeline_service.list_case_timeline(
            self.legal_case.id,
            _query(date_from="2026-08-01", date_to="2026-08-01"),
            actor=administrator,
        )

        assert result.total == 1

    def test_filters_combine(
        self, timeline_service: TimelineService, administrator: User, lawyer: User
    ) -> None:
        result = timeline_service.list_case_timeline(
            self.legal_case.id,
            _query(event_type="document_uploaded", actor_id=lawyer.id),
            actor=administrator,
        )

        assert result.total == 1


class TestPagination:
    def test_the_total_counts_the_whole_filtered_set(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case()
        for _ in range(7):
            make_timeline_event(case_id=legal_case.id)

        result = timeline_service.list_case_timeline(
            legal_case.id, _query(page=1, page_size=3), actor=administrator
        )

        assert result.total == 7
        assert len(result.events) == 3
        assert result.page == 1
        assert result.page_size == 3

    def test_a_page_past_the_end_is_empty_rather_than_an_error(
        self,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case()
        make_timeline_event(case_id=legal_case.id)

        result = timeline_service.list_case_timeline(
            legal_case.id, _query(page=9), actor=administrator
        )

        assert result.events == []
        assert result.total == 1


# --------------------------------------------------------------------------- #
# Automatic publication
#
# The spec's central requirement: events must be produced by the modules that
# cause them, not written by hand. Wired exactly as `api.deps` wires it.
# --------------------------------------------------------------------------- #


class TestAutomaticPublication:
    @pytest.fixture
    def cases(self, db_session: Session, timeline_service: TimelineService) -> CaseService:
        return CaseService(
            CaseRepository(db_session),
            UserRepository(db_session),
            timeline=timeline_service,
        )

    @pytest.fixture
    def documents(  # type: ignore[no-untyped-def]
        self, db_session: Session, document_storage, timeline_service: TimelineService
    ) -> DocumentService:
        return DocumentService(
            DocumentRepository(db_session),
            CaseRepository(db_session),
            cast(DocumentStorageService, document_storage),
            timeline=timeline_service,
        )

    def _timeline(
        self, timeline_service: TimelineService, case_id: uuid.UUID, actor: User
    ) -> list[TimelineEvent]:
        return timeline_service.list_case_timeline(
            case_id, _query(page_size=100, sort_order=SortOrder.ASC), actor=actor
        ).events

    # ------------------------------------------------------------- cases #

    def test_creating_a_case_records_it(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
    ) -> None:
        created = cases.create_case(CaseCreate(title="Benali v. Atlas"), actor=administrator)

        events = self._timeline(timeline_service, created.id, administrator)
        assert _types(events) == ["case_created"]
        assert events[0].description is not None
        assert created.case_number in events[0].description
        assert events[0].actor_id == administrator.id

    def test_creating_a_case_with_assignees_records_the_assignments_too(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        lawyer: User,
        court: User,
    ) -> None:
        # Otherwise "who has been on this case, since when" is unanswerable.
        created = cases.create_case(
            CaseCreate(
                title="Benali v. Atlas",
                assigned_lawyer_id=lawyer.id,
                assigned_court_representative_id=court.id,
            ),
            actor=administrator,
        )

        events = self._timeline(timeline_service, created.id, administrator)
        assert _types(events) == [
            "case_created",
            "lawyer_assigned",
            "representative_assigned",
        ]

    def test_updating_descriptive_fields_records_one_case_updated(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        cases.update_case(
            legal_case.id,
            CaseUpdate(title="Benali v. Atlas SARL", court_name="Tribunal de Commerce"),
            actor=administrator,
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["case_updated"]
        assert events[0].event_metadata["fields"] == ["court", "title"]

    def test_a_status_change_records_status_changed_and_not_case_updated(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        # A request that merely moved the status must not also claim the case
        # was edited.
        legal_case = make_case(status=CaseStatus.OPEN)

        cases.update_case(
            legal_case.id, CaseUpdate(status=CaseStatus.IN_PROGRESS), actor=administrator
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["status_changed"]
        assert events[0].event_metadata == {"from": "open", "to": "in_progress"}
        assert events[0].description == (
            "Amina Benali changed the status from Open to In progress."
        )

    def test_a_priority_change_is_its_own_event(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(priority=CasePriority.MEDIUM)

        cases.update_case(
            legal_case.id, CaseUpdate(priority=CasePriority.URGENT), actor=administrator
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["priority_changed"]
        assert events[0].event_metadata == {"from": "medium", "to": "urgent"}

    def test_one_request_can_produce_several_events(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(status=CaseStatus.OPEN, priority=CasePriority.LOW)

        cases.update_case(
            legal_case.id,
            CaseUpdate(
                title="Benali v. Atlas SARL",
                status=CaseStatus.IN_PROGRESS,
                priority=CasePriority.HIGH,
                assigned_lawyer_id=lawyer.id,
            ),
            actor=administrator,
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert set(_types(events)) == {
            "status_changed",
            "priority_changed",
            "lawyer_assigned",
            "case_updated",
        }

    def test_assigning_a_lawyer_names_them(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        cases.update_case(
            legal_case.id, CaseUpdate(assigned_lawyer_id=lawyer.id), actor=administrator
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["lawyer_assigned"]
        assert events[0].description == "Amina Benali assigned Sarah Smith as the lawyer."
        assert events[0].event_metadata["assignee_id"] == str(lawyer.id)

    def test_assigning_a_representative_names_them(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        court: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        cases.update_case(
            legal_case.id,
            CaseUpdate(assigned_court_representative_id=court.id),
            actor=administrator,
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["representative_assigned"]
        assert events[0].description == (
            "Amina Benali assigned Karim Ziani as the court representative."
        )

    def test_removing_a_lawyer_names_who_was_removed(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        # Captured before the write: after the assignment is cleared there is
        # nobody left on the row to name.
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        cases.update_case(
            legal_case.id, CaseUpdate(assigned_lawyer_id=None), actor=administrator
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["lawyer_removed"]
        assert events[0].description == "Amina Benali removed Sarah Smith as the lawyer."

    def test_clearing_an_empty_assignment_records_nothing(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        cases.update_case(
            legal_case.id, CaseUpdate(assigned_lawyer_id=None), actor=administrator
        )

        assert self._timeline(timeline_service, legal_case.id, administrator) == []

    def test_archiving_records_case_archived(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(status=CaseStatus.OPEN)

        cases.archive_case(legal_case.id, actor=administrator)

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["case_archived"]
        assert events[0].event_metadata == {"from": "open", "to": "archived"}

    def test_archiving_twice_records_one_event(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        # The operation is idempotent; the history must be too.
        legal_case = make_case(status=CaseStatus.OPEN)

        cases.archive_case(legal_case.id, actor=administrator)
        cases.archive_case(legal_case.id, actor=administrator)

        assert _types(self._timeline(timeline_service, legal_case.id, administrator)) == [
            "case_archived"
        ]

    def test_restoring_records_case_restored_rather_than_status_changed(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(status=CaseStatus.ARCHIVED)

        cases.update_case(
            legal_case.id, CaseUpdate(status=CaseStatus.OPEN), actor=administrator
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["case_restored"]

    def test_archiving_through_the_update_path_records_case_archived(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(status=CaseStatus.OPEN)

        cases.update_case(
            legal_case.id, CaseUpdate(status=CaseStatus.ARCHIVED), actor=administrator
        )

        assert _types(self._timeline(timeline_service, legal_case.id, administrator)) == [
            "case_archived"
        ]

    def test_re_submitting_unchanged_values_records_nothing(
        self,
        cases: CaseService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        # A form that round-trips every field must not manufacture history.
        legal_case = make_case(status=CaseStatus.OPEN, priority=CasePriority.MEDIUM)

        cases.update_case(
            legal_case.id,
            CaseUpdate(status=CaseStatus.OPEN, priority=CasePriority.MEDIUM),
            actor=administrator,
        )

        assert self._timeline(timeline_service, legal_case.id, administrator) == []

    # --------------------------------------------------------- documents #

    def _upload(
        self, documents: DocumentService, case_id: uuid.UUID, actor: User, name: str = "Contract.pdf"
    ) -> uuid.UUID:
        created = documents.upload_document(
            DocumentUploadForm(case_id=case_id),
            validate_upload(filename=name, stream=io.BytesIO(PDF_BYTES)),
            actor=actor,
        )
        return created.id

    def test_uploading_a_document_records_it_with_the_filename(
        self,
        documents: DocumentService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        self._upload(documents, legal_case.id, administrator)

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["document_uploaded"]
        # The spec's own example. Note this is the one place a filename is
        # recorded — the application log deliberately never carries one.
        assert events[0].description == 'Amina Benali uploaded "Contract.pdf".'
        assert events[0].event_metadata["filename"] == "Contract.pdf"

    def test_updating_a_document_s_metadata_records_it(
        self,
        documents: DocumentService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()
        document_id = self._upload(documents, legal_case.id, administrator)

        documents.update_document(
            document_id, DocumentUpdate(description="Signed original."), actor=administrator
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["document_uploaded", "document_updated"]
        assert events[1].event_metadata["fields"] == ["description"]

    def test_replacing_a_document_records_the_new_version(
        self,
        documents: DocumentService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()
        document_id = self._upload(documents, legal_case.id, administrator)

        documents.replace_document(
            document_id,
            validate_upload(filename="Contract-v2.pdf", stream=io.BytesIO(PDF_BYTES)),
            actor=administrator,
        )

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["document_uploaded", "document_replaced"]
        assert events[1].event_metadata["previous_version"] == 1
        assert events[1].event_metadata["version"] == 2

    def test_deleting_a_document_records_it_once(
        self,
        documents: DocumentService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()
        document_id = self._upload(documents, legal_case.id, administrator)

        documents.delete_document(document_id, actor=administrator)
        documents.delete_document(document_id, actor=administrator)

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["document_uploaded", "document_deleted"]

    def test_downloading_a_document_is_recorded(
        self,
        documents: DocumentService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        # Who took a copy of a legal document is exactly what the audit trail is
        # for, and the spec lists DOCUMENT_DOWNLOADED as an event type.
        legal_case = make_case()
        document_id = self._upload(documents, legal_case.id, administrator)

        documents.open_download(document_id, actor=administrator)

        events = self._timeline(timeline_service, legal_case.id, administrator)
        assert _types(events) == ["document_uploaded", "document_downloaded"]
        assert events[1].event_metadata["downloaded_version"] == 1

    def test_previewing_a_document_is_not_recorded(
        self,
        documents: DocumentService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        # The spec defines no preview event, and inventing one is not this
        # feature's call.
        legal_case = make_case()
        document_id = self._upload(documents, legal_case.id, administrator)

        documents.open_preview(document_id, actor=administrator)

        assert _types(self._timeline(timeline_service, legal_case.id, administrator)) == [
            "document_uploaded"
        ]

    def test_document_events_land_on_the_document_s_case(
        self,
        documents: DocumentService,
        timeline_service: TimelineService,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        first = make_case()
        second = make_case()
        self._upload(documents, first.id, administrator)

        assert len(self._timeline(timeline_service, first.id, administrator)) == 1
        assert self._timeline(timeline_service, second.id, administrator) == []
