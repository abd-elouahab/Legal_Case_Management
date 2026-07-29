"""Tests for per-resource case authorization.

RBAC answers "may this user use the case-viewing capability?"; this module is the
other half — "may they reach *this* case, and write *these* fields?". Both are
required by `code-standards.md`, and the second is the one RBAC deliberately
deferred until case assignments existed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from core.exceptions import CaseAccessDeniedError
from models.case import Case
from models.user import User, UserRole
from services.case_access import (
    ASSIGNMENT_FIELDS,
    FIELD_PERMISSIONS,
    HEARING_FIELDS,
    CaseAccessPolicy,
)

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]

PASSWORD = "correct-horse-battery"


@pytest.fixture
def policy() -> CaseAccessPolicy:
    return CaseAccessPolicy()


@pytest.fixture
def administrator(make_user: MakeUser) -> User:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(email="lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def representative(make_user: MakeUser) -> User:
    return make_user(
        email="court@example.com", password=PASSWORD, role=UserRole.COURT_REPRESENTATIVE
    )


class TestVisibilityScope:
    def test_an_administrator_is_unrestricted(
        self, policy: CaseAccessPolicy, administrator: User
    ) -> None:
        assert policy.sees_all_cases(administrator) is True
        # `None` means "no WHERE clause", which is what pushes the decision into
        # the query rather than into a Python filter over every row.
        assert policy.visibility_scope(administrator) is None

    @pytest.mark.parametrize("role_fixture", ["lawyer", "representative"])
    def test_a_restricted_role_is_scoped_to_itself(
        self, policy: CaseAccessPolicy, request: pytest.FixtureRequest, role_fixture: str
    ) -> None:
        user: User = request.getfixturevalue(role_fixture)

        assert policy.sees_all_cases(user) is False
        assert policy.visibility_scope(user) == user.id


class TestCanView:
    def test_an_administrator_reaches_any_case(
        self, policy: CaseAccessPolicy, administrator: User, make_case: MakeCase
    ) -> None:
        assert policy.can_view(administrator, make_case()) is True

    def test_an_unassigned_lawyer_is_refused(
        self, policy: CaseAccessPolicy, lawyer: User, make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        assert policy.can_view(lawyer, legal_case) is False
        with pytest.raises(CaseAccessDeniedError):
            policy.require_view(lawyer, legal_case)

    def test_the_assigned_lawyer_is_admitted(
        self, policy: CaseAccessPolicy, lawyer: User, make_case: MakeCase
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        assert policy.can_view(lawyer, legal_case) is True
        policy.require_view(lawyer, legal_case)

    def test_the_assigned_representative_is_admitted(
        self, policy: CaseAccessPolicy, representative: User, make_case: MakeCase
    ) -> None:
        legal_case = make_case(assigned_court_representative_id=representative.id)

        assert policy.can_view(representative, legal_case) is True

    def test_being_assigned_to_a_different_case_does_not_help(
        self, policy: CaseAccessPolicy, lawyer: User, make_case: MakeCase
    ) -> None:
        make_case(assigned_lawyer_id=lawyer.id)
        other = make_case()

        assert policy.can_view(lawyer, other) is False

    def test_a_denial_is_a_generic_403(
        self, policy: CaseAccessPolicy, lawyer: User, make_case: MakeCase
    ) -> None:
        # A refused caller learns only that they were refused — never which
        # permission or assignment would have admitted them.
        with pytest.raises(CaseAccessDeniedError) as excinfo:
            policy.require_view(lawyer, make_case())

        assert excinfo.value.status_code == 403
        assert excinfo.value.error_code == "forbidden"
        assert "assign" not in excinfo.value.message


class TestFieldPermissions:
    def test_the_governed_fields_are_the_hearing_and_assignment_ones(self) -> None:
        assert set(FIELD_PERMISSIONS) == HEARING_FIELDS | ASSIGNMENT_FIELDS

    def test_an_administrator_may_write_anything(
        self, policy: CaseAccessPolicy, administrator: User, make_case: MakeCase
    ) -> None:
        policy.require_writable(
            administrator,
            ["title", "status", "court_name", "assigned_lawyer_id"],
            legal_case=make_case(),
        )

    def test_a_lawyer_may_write_the_case_but_not_the_assignments(
        self, policy: CaseAccessPolicy, lawyer: User, make_case: MakeCase
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        policy.require_writable(lawyer, ["title", "priority", "status"], legal_case=legal_case)

        with pytest.raises(CaseAccessDeniedError):
            policy.require_writable(lawyer, ["assigned_lawyer_id"], legal_case=legal_case)

    @pytest.mark.parametrize("field", sorted(HEARING_FIELDS))
    def test_a_representative_may_write_the_hearing_fields(
        self, policy: CaseAccessPolicy, representative: User, make_case: MakeCase, field: str
    ) -> None:
        policy.require_writable(representative, [field], legal_case=make_case())

    @pytest.mark.parametrize("field", ["title", "description", "category", "priority"])
    def test_a_representative_may_not_rewrite_the_case(
        self, policy: CaseAccessPolicy, representative: User, make_case: MakeCase, field: str
    ) -> None:
        with pytest.raises(CaseAccessDeniedError):
            policy.require_writable(representative, [field], legal_case=make_case())

    def test_one_refused_field_refuses_the_whole_write(
        self, policy: CaseAccessPolicy, representative: User, make_case: MakeCase
    ) -> None:
        # No partial application: a form submitted whole is accepted whole or not
        # at all.
        with pytest.raises(CaseAccessDeniedError):
            policy.require_writable(
                representative, ["court_name", "title"], legal_case=make_case()
            )

    def test_an_ungoverned_field_falls_back_to_the_full_update_permission(
        self, policy: CaseAccessPolicy, representative: User, make_case: MakeCase
    ) -> None:
        # A field added to `CaseUpdate` without a mapping entry must default to
        # the strictest rule, not arrive ungoverned.
        assert "category" not in FIELD_PERMISSIONS

        with pytest.raises(CaseAccessDeniedError):
            policy.require_writable(representative, ["category"], legal_case=make_case())

    def test_writable_fields_describe_the_callers_reach(
        self, policy: CaseAccessPolicy, representative: User
    ) -> None:
        assert policy.writable_fields(representative) == HEARING_FIELDS

    def test_writing_nothing_is_permitted(
        self, policy: CaseAccessPolicy, representative: User, make_case: MakeCase
    ) -> None:
        # An empty PATCH is rejected by the schema, not by authorization — asking
        # for no fields is not an authorization failure.
        policy.require_writable(representative, [], legal_case=make_case())


class TestIsAssignedTo:
    def test_an_unrelated_identifier_is_not_assigned(self, make_case: MakeCase) -> None:
        assert make_case().is_assigned_to(uuid.uuid4()) is False

    def test_either_position_counts(self, make_case: MakeCase) -> None:
        lawyer_id, representative_id = uuid.uuid4(), uuid.uuid4()
        legal_case = make_case(
            assigned_lawyer_id=lawyer_id, assigned_court_representative_id=representative_id
        )

        assert legal_case.is_assigned_to(lawyer_id) is True
        assert legal_case.is_assigned_to(representative_id) is True
