"""Tests for the role → permission policy.

The policy is the single place that decides what each role may do, so these
tests assert the *shape* of that policy (administrator full access, the two
restricted roles) rather than re-listing it permission by permission — which
would only duplicate `core/roles.py` and break on every future refinement.
"""

from __future__ import annotations

import pytest

from core.exceptions import AuthorizationConfigurationError
from core.permissions import ALL_PERMISSIONS, Permission
from core.roles import (
    BASE_PERMISSIONS,
    ROLE_PERMISSIONS,
    UserRole,
    permissions_for_role,
    role_from_value,
)


class TestPolicyCoverage:
    def test_every_role_has_a_policy_entry(self) -> None:
        # A role with no entry fails closed at runtime; catching it here means it
        # can never reach production.
        assert set(ROLE_PERMISSIONS) == set(UserRole)

    def test_the_policy_grants_only_defined_permissions(self) -> None:
        for role, permissions in ROLE_PERMISSIONS.items():
            assert permissions <= ALL_PERMISSIONS, f"{role!r} grants an unknown permission"

    def test_the_policy_cannot_be_mutated_at_runtime(self) -> None:
        # A read-only mapping of frozensets means no bug elsewhere can widen
        # someone's access by editing the policy in place.
        with pytest.raises(TypeError):
            ROLE_PERMISSIONS[UserRole.LAWYER] = ALL_PERMISSIONS  # type: ignore[index]

    def test_every_role_receives_the_baseline(self) -> None:
        for role in UserRole:
            assert permissions_for_role(role) >= BASE_PERMISSIONS, f"{role!r} is missing the baseline"


class TestAdministrator:
    def test_has_every_permission(self) -> None:
        assert permissions_for_role(UserRole.ADMINISTRATOR) == ALL_PERMISSIONS

    def test_a_new_permission_reaches_administrators_automatically(self) -> None:
        # Administrators are granted ALL_PERMISSIONS by reference, so adding a
        # member to the enum must not require an edit to the policy.
        assert permissions_for_role(UserRole.ADMINISTRATOR) is ALL_PERMISSIONS


class TestLawyer:
    @pytest.mark.parametrize(
        "permission",
        [
            Permission.CASES_VIEW,
            # Case Management granted this: "update assigned cases where
            # permitted". The *assigned* half is per-resource, not a permission.
            Permission.CASES_UPDATE,
            Permission.DOCUMENTS_VIEW,
            Permission.DOCUMENTS_UPLOAD,
            Permission.TIMELINE_VIEW,
            Permission.TIMELINE_CREATE,
            Permission.REPORTS_VIEW,
            Permission.REPORTS_GENERATE,
            Permission.AI_CHAT,
            Permission.AI_GENERATE_REPORT,
        ],
    )
    def test_can_work_on_assigned_cases(self, permission: Permission) -> None:
        assert permission in permissions_for_role(UserRole.LAWYER)

    @pytest.mark.parametrize(
        "permission",
        [
            Permission.USERS_CREATE,
            Permission.USERS_VIEW,
            Permission.USERS_UPDATE,
            Permission.USERS_DELETE,
            Permission.CASES_CREATE,
            Permission.CASES_DELETE,
            Permission.CASES_ASSIGN,
            # Lawyers may update the cases they are *assigned to*; the row-level
            # half of that rule is what withholding `cases:view-all` expresses.
            Permission.CASES_VIEW_ALL,
            Permission.DOCUMENTS_DELETE,
            # `settings:manage` is the platform's own configuration — maintenance
            # mode, and the defaults every account that has expressed no opinion
            # follows. It is deliberately **not** a wider form of
            # `settings:update`, which is the caller's *own* preferences and is in
            # `BASE_PERMISSIONS`: a role that could not change its own theme or
            # language would be a role the platform is unusable in. This
            # parametrization named `settings:update` until `20-settings.md`
            # gave the two permissions their distinct meanings.
            Permission.SETTINGS_MANAGE,
            Permission.SETTINGS_MONITOR,
        ],
    )
    def test_cannot_administer_the_platform(self, permission: Permission) -> None:
        assert permission not in permissions_for_role(UserRole.LAWYER)

    def test_is_strictly_less_privileged_than_an_administrator(self) -> None:
        lawyer = permissions_for_role(UserRole.LAWYER)

        assert lawyer < permissions_for_role(UserRole.ADMINISTRATOR)


class TestCourtRepresentative:
    @pytest.mark.parametrize(
        "permission",
        [
            Permission.CASES_VIEW,
            # "Trigger case status updates" — narrowed by Case Management from the
            # full `cases:update` it provisionally rode on, so a court
            # representative can record hearings without rewriting the case.
            Permission.CASES_UPDATE_HEARING,
            Permission.DOCUMENTS_VIEW,
            Permission.DOCUMENTS_UPLOAD,
            Permission.TIMELINE_VIEW,
            Permission.TIMELINE_CREATE,
        ],
    )
    def test_can_record_court_activity(self, permission: Permission) -> None:
        assert permission in permissions_for_role(UserRole.COURT_REPRESENTATIVE)

    @pytest.mark.parametrize(
        "permission",
        [
            Permission.REPORTS_VIEW,
            Permission.REPORTS_GENERATE,
            Permission.AI_CHAT,
            Permission.AI_GENERATE_REPORT,
            Permission.USERS_VIEW,
            Permission.CASES_CREATE,
            Permission.CASES_ASSIGN,
            # The full case edit, which would let them rewrite title, description,
            # and category — the narrow `cases:update-hearing` is what they hold.
            Permission.CASES_UPDATE,
            # Both restricted roles are scoped to their assigned cases, which is
            # exactly what withholding this permission expresses.
            Permission.CASES_VIEW_ALL,
        ],
    )
    def test_has_no_reporting_ai_or_administration(self, permission: Permission) -> None:
        assert permission not in permissions_for_role(UserRole.COURT_REPRESENTATIVE)

    def test_is_strictly_less_privileged_than_an_administrator(self) -> None:
        court = permissions_for_role(UserRole.COURT_REPRESENTATIVE)

        assert court < permissions_for_role(UserRole.ADMINISTRATOR)


class TestRoleLookup:
    @pytest.mark.parametrize("identifier", ["administrator", "lawyer", "court"])
    def test_resolves_the_specified_role_identifiers(self, identifier: str) -> None:
        assert role_from_value(identifier).value == identifier

    def test_unknown_role_fails_as_a_configuration_error(self) -> None:
        with pytest.raises(AuthorizationConfigurationError) as excinfo:
            role_from_value("judge")

        assert excinfo.value.status_code == 500
        assert "judge" not in excinfo.value.message
        assert "judge" in excinfo.value.detail
