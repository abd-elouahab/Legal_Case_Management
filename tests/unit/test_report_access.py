"""Unit tests for report per-resource authorization.

The policy owns exactly one question — *may this caller generate a report about
this case* — and delegates it to the case policy. These tests assert the
delegation as the invariant it is, and assert the boundary around it: that
ownership of a *report* is not this module's business, because it is enforced by
the repository's queries instead.

Pure objects, no database: the policy reads the caller's role-derived permissions
and the case's assignment columns and touches neither the network nor a session.
"""

from __future__ import annotations

import uuid

import pytest

from core.exceptions import CaseAccessDeniedError
from models.case import Case
from models.user import User, UserRole
from services.report_access import ReportAccessPolicy


def user(role: UserRole, user_id: uuid.UUID | None = None) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        email=f"{role.value}@example.com",
        first_name="Amina",
        last_name="Benali",
        hashed_password="x",
        role=role,
    )


def case(
    *, lawyer_id: uuid.UUID | None = None, representative_id: uuid.UUID | None = None
) -> Case:
    return Case(
        id=uuid.uuid4(),
        case_number="CASE-2026-0001",
        title="Benali v. Atlas",
        assigned_lawyer_id=lawyer_id,
        assigned_court_representative_id=representative_id,
    )


@pytest.fixture
def policy() -> ReportAccessPolicy:
    return ReportAccessPolicy()


class TestCaseAccess:
    def test_an_assigned_lawyer_may_generate(self, policy: ReportAccessPolicy) -> None:
        lawyer = user(UserRole.LAWYER)

        assert policy.can_use_case(lawyer, case(lawyer_id=lawyer.id))

    def test_an_unassigned_lawyer_may_not(self, policy: ReportAccessPolicy) -> None:
        lawyer = user(UserRole.LAWYER)

        assert not policy.can_use_case(lawyer, case(lawyer_id=uuid.uuid4()))

    def test_an_administrator_may_generate_for_any_case(
        self, policy: ReportAccessPolicy
    ) -> None:
        """``cases:view-all`` lifts the row restriction, exactly as it does for
        documents, OCR results, indexes, and timelines."""
        assert policy.can_use_case(user(UserRole.ADMINISTRATOR), case())

    def test_an_assigned_representative_passes_the_case_check(
        self, policy: ReportAccessPolicy
    ) -> None:
        """The *case* check, and only that. Whether a court representative may
        reach the report endpoints at all is decided one layer up, by the
        ``reports:generate`` and ``ai:generate-report`` permissions they do not
        hold — which is where a role decision belongs."""
        representative = user(UserRole.COURT_REPRESENTATIVE)

        assert policy.can_use_case(
            representative, case(representative_id=representative.id)
        )

    def test_a_refusal_is_a_403_rather_than_a_concealing_404(
        self, policy: ReportAccessPolicy
    ) -> None:
        """A lawyer who follows a colleague's link to a case needs to know the
        case exists and that they should ask to be assigned. This is the
        *opposite* of what a report belonging to another user gets, and the
        asymmetry is deliberate."""
        with pytest.raises(CaseAccessDeniedError):
            policy.require_case_access(user(UserRole.LAWYER), case())

    def test_a_permitted_caller_passes_silently(self, policy: ReportAccessPolicy) -> None:
        lawyer = user(UserRole.LAWYER)

        assert policy.require_case_access(lawyer, case(lawyer_id=lawyer.id)) is None


class TestDelegation:
    def test_the_policy_owns_no_rule_of_its_own(self) -> None:
        """It delegates to :class:`~services.case_access.CaseAccessPolicy`, so a
        report can never be more reachable than the case it is about — the same
        shape ``document_access``, ``ocr_access``, ``indexing_access``, and
        ``timeline_access`` have."""
        from services.case_access import CaseAccessPolicy

        assert isinstance(ReportAccessPolicy()._cases, CaseAccessPolicy)

    def test_the_case_policy_can_be_injected(self) -> None:
        """Which is what makes the delegation testable as the invariant it is,
        rather than as a coincidence of two implementations agreeing."""
        cases = _RefusingCasePolicy()

        with pytest.raises(CaseAccessDeniedError):
            ReportAccessPolicy(cases).require_case_access(  # type: ignore[arg-type]
                user(UserRole.ADMINISTRATOR), case()
            )

    def test_there_is_no_report_ownership_rule_here(self) -> None:
        """Whose report it is, is enforced by the repository's queries — every
        read is keyed by ``requested_by``, so there is no query in the platform
        that can return another user's report and nothing for a second module to
        keep in step with."""
        members = {name for name in dir(ReportAccessPolicy) if not name.startswith("_")}

        assert members == {"can_use_case", "require_case_access"}


class _RefusingCasePolicy:
    """A case policy that refuses everything, to prove the delegation is real."""

    def can_view(self, user: User, legal_case: Case) -> bool:
        return False
