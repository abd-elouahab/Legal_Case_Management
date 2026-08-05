"""Unit tests for :mod:`services.timeline_access`.

The module's whole claim is that it owns no policy: **an event is reachable
exactly when its case is.** These assert that claim rather than re-testing the
case rules, which ``tests/unit/test_case_access.py`` already covers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from core.exceptions import TimelineAccessDeniedError
from models.case import Case
from models.timeline import TimelineEvent, TimelineEventType
from models.user import User, UserRole
from services.case_access import CaseAccessPolicy
from services.timeline_access import TimelineAccessPolicy

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeEvent = Callable[..., TimelineEvent]


@pytest.fixture
def policy() -> TimelineAccessPolicy:
    return TimelineAccessPolicy()


@pytest.fixture
def administrator(make_user: MakeUser) -> User:
    return make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(email="lawyer@example.com", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: MakeUser) -> User:
    return make_user(email="other@example.com", role=UserRole.LAWYER)


@pytest.fixture
def court(make_user: MakeUser) -> User:
    return make_user(email="court@example.com", role=UserRole.COURT_REPRESENTATIVE)


class TestVisibilityScope:
    def test_an_administrator_is_unrestricted(
        self, policy: TimelineAccessPolicy, administrator: User
    ) -> None:
        # `cases:view-all` lifts the row restriction, so no scope is applied.
        assert policy.visibility_scope(administrator) is None

    def test_a_lawyer_is_scoped_to_themselves(
        self, policy: TimelineAccessPolicy, lawyer: User
    ) -> None:
        assert policy.visibility_scope(lawyer) == lawyer.id

    def test_it_returns_exactly_what_the_case_policy_returns(
        self, policy: TimelineAccessPolicy, lawyer: User, court: User, administrator: User
    ) -> None:
        # The delegation, stated as the invariant it is: if these ever disagree,
        # a lawyer sees a timeline for a case they cannot open.
        cases = CaseAccessPolicy()
        for user in (administrator, lawyer, court):
            assert policy.visibility_scope(user) == cases.visibility_scope(user)


class TestRequireCaseAccess:
    def test_an_administrator_reaches_any_case(
        self,
        policy: TimelineAccessPolicy,
        administrator: User,
        make_case: MakeCase,
    ) -> None:
        policy.require_case_access(administrator, make_case())

    def test_an_assigned_lawyer_reaches_their_case(
        self, policy: TimelineAccessPolicy, lawyer: User, make_case: MakeCase
    ) -> None:
        policy.require_case_access(lawyer, make_case(assigned_lawyer_id=lawyer.id))

    def test_an_assigned_representative_reaches_their_case(
        self, policy: TimelineAccessPolicy, court: User, make_case: MakeCase
    ) -> None:
        policy.require_case_access(
            court, make_case(assigned_court_representative_id=court.id)
        )

    def test_an_unassigned_lawyer_is_refused(
        self,
        policy: TimelineAccessPolicy,
        lawyer: User,
        other_lawyer: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=other_lawyer.id)

        with pytest.raises(TimelineAccessDeniedError):
            policy.require_case_access(lawyer, legal_case)


class TestRequireView:
    def test_an_assigned_lawyer_reaches_an_event_on_their_case(
        self,
        policy: TimelineAccessPolicy,
        lawyer: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)
        event = make_timeline_event(case_id=legal_case.id)

        policy.require_view(lawyer, event)
        assert policy.can_view(lawyer, event) is True

    def test_an_unassigned_lawyer_is_refused_an_event(
        self,
        policy: TimelineAccessPolicy,
        lawyer: User,
        other_lawyer: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=other_lawyer.id)
        event = make_timeline_event(
            case_id=legal_case.id, event_type=TimelineEventType.DOCUMENT_UPLOADED
        )

        assert policy.can_view(lawyer, event) is False
        with pytest.raises(TimelineAccessDeniedError):
            policy.require_view(lawyer, event)

    def test_the_denial_answers_403_with_a_generic_body(
        self,
        policy: TimelineAccessPolicy,
        lawyer: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        event = make_timeline_event(case_id=make_case().id)

        with pytest.raises(TimelineAccessDeniedError) as raised:
            policy.require_view(lawyer, event)

        # Never names the permission, the role, or the case that was refused.
        assert raised.value.status_code == 403
        assert raised.value.error_code == "forbidden"
        assert "timeline" not in raised.value.message.lower()

    def test_reassigning_the_case_immediately_changes_reachability(
        self,
        policy: TimelineAccessPolicy,
        lawyer: User,
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        # Access is decided against the case, not copied onto the event, so a
        # historical entry follows the case's *current* assignment.
        legal_case = make_case()
        event = make_timeline_event(case_id=legal_case.id)

        assert policy.can_view(lawyer, event) is False

        legal_case.assigned_lawyer_id = lawyer.id
        assert policy.can_view(lawyer, event) is True


class TestDelegation:
    def test_it_holds_no_policy_of_its_own(
        self, lawyer: User, make_case: MakeCase, make_timeline_event: MakeEvent
    ) -> None:
        # Swapping in a case policy that admits everyone must admit everyone here
        # too — if it does not, this module has grown a rule of its own.
        class AlwaysAllow(CaseAccessPolicy):
            def can_view(self, user: User, legal_case: Case) -> bool:
                return True

            def visibility_scope(self, user: User) -> uuid.UUID | None:
                return None

        policy = TimelineAccessPolicy(AlwaysAllow())
        event = make_timeline_event(case_id=make_case().id)

        assert policy.can_view(lawyer, event) is True
        assert policy.visibility_scope(lawyer) is None
