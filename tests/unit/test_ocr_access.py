"""Unit tests for :class:`~services.ocr_access.OcrAccessPolicy`.

The policy owns no rules of its own — it delegates to the document policy, which
delegates to the case policy. These tests assert exactly that: **extracted text
can never be more visible than the file it was read from**, which is the spec's
"extracted text inherits document permissions" stated as an invariant rather than
as a convention.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.exceptions import OcrAccessDeniedError
from models.user import UserRole
from services.ocr_access import OcrAccessPolicy

MakeUser = Any
MakeCase = Any
MakeDocument = Any
MakeOcrResult = Any


@pytest.fixture
def policy() -> OcrAccessPolicy:
    return OcrAccessPolicy()


@pytest.fixture
def administrator(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def assigned_lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="assigned@example.com", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="other@example.com", role=UserRole.LAWYER)


@pytest.fixture
def court(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="court@example.com", role=UserRole.COURT_REPRESENTATIVE)


@pytest.fixture
def legal_case(make_case: MakeCase, assigned_lawyer, court):  # type: ignore[no-untyped-def]
    return make_case(
        assigned_lawyer_id=assigned_lawyer.id,
        assigned_court_representative_id=court.id,
    )


@pytest.fixture
def document(make_document: MakeDocument, legal_case):  # type: ignore[no-untyped-def]
    return make_document(case_id=legal_case.id)


@pytest.fixture
def result(make_ocr_result: MakeOcrResult, document):  # type: ignore[no-untyped-def]
    return make_ocr_result(document_id=document.id)


class TestDelegation:
    def test_it_matches_the_document_policy_exactly(
        self, policy: OcrAccessPolicy, document, administrator, assigned_lawyer, other_lawyer, court
    ) -> None:
        from services.document_access import DocumentAccessPolicy

        documents = DocumentAccessPolicy()

        # Not "similar to" — identical, for every role. If the two could differ,
        # the extracted text would be the copy that leaked.
        for user in (administrator, assigned_lawyer, other_lawyer, court):
            assert policy.can_view_document(user, document) is documents.can_view(user, document)

    def test_the_visibility_scope_matches_the_document_scope(
        self, policy: OcrAccessPolicy, administrator, assigned_lawyer
    ) -> None:
        from services.document_access import DocumentAccessPolicy

        documents = DocumentAccessPolicy()

        for user in (administrator, assigned_lawyer):
            assert policy.visibility_scope(user) == documents.visibility_scope(user)


class TestReading:
    def test_an_administrator_may_read_any_run(
        self, policy: OcrAccessPolicy, result, administrator
    ) -> None:
        assert policy.can_view(administrator, result)
        policy.require_view(administrator, result)

    def test_the_assigned_lawyer_may_read(
        self, policy: OcrAccessPolicy, result, assigned_lawyer
    ) -> None:
        assert policy.can_view(assigned_lawyer, result)

    def test_the_assigned_court_representative_may_read(
        self, policy: OcrAccessPolicy, result, court
    ) -> None:
        assert policy.can_view(court, result)

    def test_an_unassigned_lawyer_is_refused(
        self, policy: OcrAccessPolicy, result, other_lawyer
    ) -> None:
        assert not policy.can_view(other_lawyer, result)
        with pytest.raises(OcrAccessDeniedError):
            policy.require_view(other_lawyer, result)

    def test_the_denial_names_nothing(
        self, policy: OcrAccessPolicy, result, other_lawyer
    ) -> None:
        with pytest.raises(OcrAccessDeniedError) as raised:
            policy.require_view(other_lawyer, result)

        message = str(raised.value)
        # A 403 body must not hand back a map of the capability model.
        assert "ocr:" not in message
        assert "lawyer" not in message.lower()


class TestDocumentAccess:
    def test_it_guards_the_document_the_same_way(
        self, policy: OcrAccessPolicy, document, other_lawyer, assigned_lawyer
    ) -> None:
        policy.require_document_access(assigned_lawyer, document)

        with pytest.raises(OcrAccessDeniedError):
            policy.require_document_access(other_lawyer, document)

    def test_a_reassignment_takes_effect_immediately(
        self, policy: OcrAccessPolicy, document, legal_case, other_lawyer, db_session
    ) -> None:
        assert not policy.can_view_document(other_lawyer, document)

        legal_case.assigned_lawyer_id = other_lawyer.id
        db_session.commit()

        # Evaluated against the row, not cached anywhere: assignment grants and
        # withdraws access to the extracted text at the same moment it does to
        # the document.
        assert policy.can_view_document(other_lawyer, document)


class TestLogging:
    # `structlog.testing.capture_logs` rather than pytest's `caplog`: the
    # platform logs through structlog, whose console renderer writes to stdout
    # without passing the event through the stdlib handler `caplog` installs —
    # so a `caplog`-based assertion here would pass against an empty string and
    # prove nothing.
    def denial_events(self, policy: OcrAccessPolicy, result, user) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        from structlog.testing import capture_logs

        with capture_logs() as events, pytest.raises(OcrAccessDeniedError):
            policy.require_view(user, result)
        return events

    def test_the_denial_log_carries_no_filename(
        self, policy: OcrAccessPolicy, result, other_lawyer
    ) -> None:
        events = self.denial_events(policy, result, other_lawyer)

        # A log line goes to an operator with no entitlement to the case, and a
        # filename can name a client or a matter.
        assert result.document.original_filename not in str(events)

    def test_the_denial_log_identifies_what_was_refused(
        self, policy: OcrAccessPolicy, result, other_lawyer
    ) -> None:
        events = self.denial_events(policy, result, other_lawyer)

        denial = next(event for event in events if event["event"] == "ocr_access_denied")
        assert denial["ocr_result_id"] == str(result.id)
        assert denial["document_id"] == str(result.document_id)
        assert denial["reason"] == "not_assigned"


class TestScope:
    def test_an_administrator_is_unscoped(
        self, policy: OcrAccessPolicy, administrator
    ) -> None:
        assert policy.visibility_scope(administrator) is None

    def test_a_lawyer_is_scoped_to_themselves(
        self, policy: OcrAccessPolicy, assigned_lawyer
    ) -> None:
        scope = policy.visibility_scope(assigned_lawyer)

        assert isinstance(scope, uuid.UUID)
        assert scope == assigned_lawyer.id
