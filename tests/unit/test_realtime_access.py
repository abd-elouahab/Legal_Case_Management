"""Unit tests for per-topic event authorization (`services/realtime_access.py`).

This module is the whole of "users must never receive events for unauthorized
cases, documents, reports, or another user's material", so the tests are about
who is refused rather than who is admitted.

The policy takes a **session factory**, which is what makes it testable: the
tests hand it the suite's SQLite session and exercise the real repositories and
the real case and document policies. Nothing here is mocked, because the thing
under test *is* the delegation.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from core.events import case_topic, document_topic, report_topic, user_topic
from models.user import UserRole
from services.realtime_access import RealtimeAccessPolicy


@pytest.fixture
def policy(db_session: Session) -> RealtimeAccessPolicy:
    """A policy backed by the test session.

    The factory returns a session that is *not* closed by the ``with`` block —
    the fixture owns its lifecycle — so the policy's own ``with`` statement
    behaves exactly as it does in production without tearing down the test's data.
    """

    class _Factory:
        def __call__(self) -> Session:
            return self

        def __enter__(self) -> Session:
            return db_session

        def __exit__(self, *_: object) -> None:
            return None

    return RealtimeAccessPolicy(_Factory())  # type: ignore[arg-type]


class TestUserTopics:
    def test_a_user_may_follow_their_own_topic(self, policy: RealtimeAccessPolicy, make_user: Any) -> None:
        user = make_user()
        assert policy.decide(user, user_topic(user.id)).allowed is True

    def test_a_user_may_not_follow_somebody_else_s(
        self, policy: RealtimeAccessPolicy, make_user: Any
    ) -> None:
        user = make_user()
        other = make_user(email="other@example.com")
        decision = policy.decide(user, user_topic(other.id))
        assert decision.allowed is False
        assert decision.reason == "not_self"

    def test_an_administrator_may_not_follow_another_user_s_topic(
        self, policy: RealtimeAccessPolicy, make_user: Any
    ) -> None:
        """Identity equality, not a permission — so `cases:view-all` does not lift it."""
        admin = make_user(role=UserRole.ADMINISTRATOR)
        other = make_user(email="other@example.com", role=UserRole.LAWYER)
        assert policy.decide(admin, user_topic(other.id)).allowed is False


class TestCaseTopics:
    def test_an_assigned_lawyer_may_follow_their_case(
        self, policy: RealtimeAccessPolicy, make_user: Any, make_case: Any
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        legal_case = make_case(assigned_lawyer_id=lawyer.id)
        assert policy.decide(lawyer, case_topic(legal_case.id)).allowed is True

    def test_an_unassigned_lawyer_may_not(
        self, policy: RealtimeAccessPolicy, make_user: Any, make_case: Any
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        legal_case = make_case()
        decision = policy.decide(lawyer, case_topic(legal_case.id))
        assert decision.allowed is False
        assert decision.reason == "not_assigned"

    def test_an_administrator_may_follow_any_case(
        self, policy: RealtimeAccessPolicy, make_user: Any, make_case: Any
    ) -> None:
        """`cases:view-all` lifts the row restriction here exactly as it does in SQL."""
        admin = make_user(role=UserRole.ADMINISTRATOR)
        assert policy.decide(admin, case_topic(make_case().id)).allowed is True

    def test_a_case_that_does_not_exist_is_refused(
        self, policy: RealtimeAccessPolicy, make_user: Any
    ) -> None:
        """Fails closed rather than raising or admitting."""
        decision = policy.decide(make_user(), case_topic(uuid.uuid4()))
        assert decision.allowed is False
        assert decision.reason == "case_not_found"


class TestDocumentTopics:
    def test_document_access_follows_case_access(
        self, policy: RealtimeAccessPolicy, make_user: Any, make_case: Any, make_document: Any
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        legal_case = make_case(assigned_lawyer_id=lawyer.id)
        document = make_document(case_id=legal_case.id)
        assert policy.decide(lawyer, document_topic(document.id)).allowed is True

    def test_a_document_on_another_case_is_refused(
        self, policy: RealtimeAccessPolicy, make_user: Any, make_case: Any, make_document: Any
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        make_case(assigned_lawyer_id=lawyer.id)
        document = make_document(case_id=make_case().id)
        assert policy.decide(lawyer, document_topic(document.id)).allowed is False

    def test_a_deleted_document_stops_being_followable(
        self,
        policy: RealtimeAccessPolicy,
        db_session: Session,
        make_user: Any,
        make_case: Any,
        make_document: Any,
    ) -> None:
        """Deletion is logical, so the channel has to honour it at read time —
        exactly as every other read path does."""
        from datetime import UTC, datetime

        admin = make_user(role=UserRole.ADMINISTRATOR)
        document = make_document(case_id=make_case().id)
        assert policy.decide(admin, document_topic(document.id)).allowed is True

        document.deleted_at = datetime.now(UTC)
        db_session.commit()

        decision = policy.decide(admin, document_topic(document.id))
        assert decision.allowed is False
        assert decision.reason == "document_not_found"


class TestReportTopics:
    def test_a_report_is_followable_only_by_its_author(
        self,
        policy: RealtimeAccessPolicy,
        make_user: Any,
        make_case: Any,
        make_report: Any,
    ) -> None:
        author = make_user(email="author@example.com", role=UserRole.LAWYER)
        legal_case = make_case(assigned_lawyer_id=author.id)
        report = make_report(case=legal_case, requested_by=author)
        assert policy.decide(author, report_topic(report.id)).allowed is True

    def test_an_administrator_may_not_follow_somebody_else_s_report(
        self,
        policy: RealtimeAccessPolicy,
        make_user: Any,
        make_case: Any,
        make_report: Any,
    ) -> None:
        """The platform's one ownership rule that `cases:view-all` does not lift.

        A report is private work product. The permission lifts a *row*
        restriction; this is an ownership one.
        """
        admin = make_user(role=UserRole.ADMINISTRATOR)
        author = make_user(email="author@example.com", role=UserRole.LAWYER)
        legal_case = make_case(assigned_lawyer_id=author.id)
        report = make_report(case=legal_case, requested_by=author)

        decision = policy.decide(admin, report_topic(report.id))
        assert decision.allowed is False
        assert decision.reason == "not_owner"


class TestRecheck:
    def test_a_disabled_account_stops_receiving_anything(
        self,
        policy: RealtimeAccessPolicy,
        db_session: Session,
        make_user: Any,
        make_case: Any,
    ) -> None:
        """What stops an already-open socket outliving a deactivation.

        The access token is short-lived but the *connection* is not, so a
        decision made against a snapshot taken at connect time would keep
        delivering until the tab was closed.
        """
        from models.user import UserStatus

        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        legal_case = make_case(assigned_lawyer_id=lawyer.id)
        topic = case_topic(legal_case.id)

        assert policy.recheck(lawyer.id, topic).allowed is True

        lawyer.status = UserStatus.INACTIVE
        db_session.commit()

        decision = policy.recheck(lawyer.id, topic)
        assert decision.allowed is False
        assert decision.reason == "account_disabled"

    def test_losing_an_assignment_revokes_access(
        self,
        policy: RealtimeAccessPolicy,
        db_session: Session,
        make_user: Any,
        make_case: Any,
    ) -> None:
        lawyer = make_user(email="lawyer@example.com", role=UserRole.LAWYER)
        legal_case = make_case(assigned_lawyer_id=lawyer.id)
        topic = case_topic(legal_case.id)

        assert policy.recheck(lawyer.id, topic).allowed is True

        legal_case.assigned_lawyer_id = None
        db_session.commit()

        assert policy.recheck(lawyer.id, topic).allowed is False

    def test_an_unknown_account_receives_nothing(self, policy: RealtimeAccessPolicy) -> None:
        decision = policy.recheck(uuid.uuid4(), case_topic(uuid.uuid4()))
        assert decision.allowed is False
        assert decision.reason == "user_not_found"

    def test_a_user_topic_recheck_needs_no_resource(
        self, policy: RealtimeAccessPolicy, make_user: Any
    ) -> None:
        user = make_user()
        assert policy.recheck(user.id, user_topic(user.id)).allowed is True


class TestFailureMode:
    def test_a_database_failure_refuses_rather_than_admits(self, make_user: Any) -> None:
        """A channel whose authorization degrades to "allow" has no authorization."""

        def _broken() -> Session:
            raise RuntimeError("database is unreachable")

        policy = RealtimeAccessPolicy(_broken)
        decision = policy.decide(make_user(), case_topic(uuid.uuid4()))
        assert decision.allowed is False
        assert decision.reason == "lookup_failed"
