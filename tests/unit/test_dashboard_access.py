"""Unit tests for the dashboard's per-widget authorization.

``19-dashboard-analytics.md`` states the rule three ways — *"every widget must
enforce authorization independently"*, *"analytics should only include data the
authenticated user is allowed to access"*, and *"aggregated metrics must never
leak unauthorized information"*. :class:`~services.dashboard_access.DashboardAccessPolicy`
is where all three are answered, and the interesting property is that it **owns no
rules**: it delegates to the policies that own the rows.

So these tests assert the delegation rather than restating the rules. If the case
policy changes, a dashboard must change with it, and the assertions below are what
would fail if it did not.
"""

from __future__ import annotations

import uuid

import pytest

from core.dashboard import QuickActionKey, WidgetKey
from core.permissions import Permission
from models.user import User, UserRole, UserStatus
from services.dashboard_access import DashboardAccessPolicy


def build_user(role: UserRole) -> User:
    """An unpersisted user, which is all a pure policy needs."""
    return User(
        id=uuid.uuid4(),
        email=f"{role.value}@example.com",
        first_name="Amina",
        last_name="Benali",
        hashed_password="x",
        role=role,
        status=UserStatus.ACTIVE,
    )


@pytest.fixture
def policy() -> DashboardAccessPolicy:
    return DashboardAccessPolicy()


class TestWidgetVisibility:
    """A widget is offered only to a caller holding every capability it names."""

    def test_an_administrator_sees_every_widget(self, policy: DashboardAccessPolicy) -> None:
        admin = build_user(UserRole.ADMINISTRATOR)
        visible = policy.visible_widgets(tuple(WidgetKey), policy.permissions_for(admin))
        assert set(visible) == set(WidgetKey)

    def test_a_lawyer_is_not_offered_the_system_widgets(
        self, policy: DashboardAccessPolicy
    ) -> None:
        """Storage, accounts, and queues are platform-wide and administrative."""
        lawyer = build_user(UserRole.LAWYER)
        visible = set(policy.visible_widgets(tuple(WidgetKey), policy.permissions_for(lawyer)))

        assert WidgetKey.STORAGE_USAGE not in visible
        assert WidgetKey.ACTIVE_USERS not in visible
        assert WidgetKey.PROCESSING_QUEUES not in visible

    def test_a_lawyer_is_offered_their_own_work(self, policy: DashboardAccessPolicy) -> None:
        lawyer = build_user(UserRole.LAWYER)
        visible = set(policy.visible_widgets(tuple(WidgetKey), policy.permissions_for(lawyer)))

        assert WidgetKey.MY_CASES in visible
        assert WidgetKey.UPCOMING_HEARINGS in visible
        assert WidgetKey.AI_REPORTS in visible

    def test_a_court_representative_is_offered_no_ai_widget(
        self, policy: DashboardAccessPolicy
    ) -> None:
        court = build_user(UserRole.COURT_REPRESENTATIVE)
        visible = set(policy.visible_widgets(tuple(WidgetKey), policy.permissions_for(court)))

        assert WidgetKey.AI_REPORTS not in visible
        assert WidgetKey.RECENT_CONVERSATIONS not in visible
        assert WidgetKey.AI_ANALYTICS not in visible

    def test_an_aggregate_widget_needs_every_capability_it_names(
        self, policy: DashboardAccessPolicy
    ) -> None:
        """**All, not any.**

        `document_analytics` reports uploads, extraction, *and* indexing. Offering
        it to somebody holding one of the three would report the other two as
        zero — and a zero is information, which is exactly what "aggregated
        metrics must never leak unauthorized information" forbids.
        """
        from core.dashboard import widget_definition

        definition = widget_definition(WidgetKey.DOCUMENT_ANALYTICS)
        partial = frozenset({Permission.DOCUMENTS_VIEW})

        assert not policy.can_view(definition, partial)
        assert policy.can_view(definition, definition.permissions)

    def test_quick_actions_is_offered_to_everyone(self, policy: DashboardAccessPolicy) -> None:
        """Its *contents* gate themselves, so the widget itself needs no capability."""
        for role in UserRole:
            user = build_user(role)
            visible = policy.visible_widgets(
                (WidgetKey.QUICK_ACTIONS,), policy.permissions_for(user)
            )
            assert visible == (WidgetKey.QUICK_ACTIONS,)

    def test_visible_widgets_preserves_the_layout_order(
        self, policy: DashboardAccessPolicy
    ) -> None:
        """The order is the role's opinion; filtering must not re-sort it."""
        admin = build_user(UserRole.ADMINISTRATOR)
        requested = (WidgetKey.OCR_STATUS, WidgetKey.MY_CASES, WidgetKey.NOTIFICATIONS)

        assert policy.visible_widgets(requested, policy.permissions_for(admin)) == requested


class TestRequireView:
    """The single-widget endpoint's gate."""

    def test_it_returns_the_definition_for_an_authorized_caller(
        self, policy: DashboardAccessPolicy
    ) -> None:
        definition = policy.require_view(WidgetKey.MY_CASES, build_user(UserRole.LAWYER))
        assert definition.key is WidgetKey.MY_CASES

    def test_it_refuses_a_widget_the_caller_does_not_hold(
        self, policy: DashboardAccessPolicy
    ) -> None:
        with pytest.raises(PermissionError):
            policy.require_view(WidgetKey.STORAGE_USAGE, build_user(UserRole.LAWYER))


class TestScopes:
    """Which rows a widget may count, delegated rather than re-derived."""

    def test_an_administrator_has_no_case_restriction(
        self, policy: DashboardAccessPolicy
    ) -> None:
        """`cases:view-all` means the queries run unrestricted."""
        assert policy.case_scope(build_user(UserRole.ADMINISTRATOR)) is None

    def test_a_lawyer_is_scoped_to_their_own_assignments(
        self, policy: DashboardAccessPolicy
    ) -> None:
        lawyer = build_user(UserRole.LAWYER)
        assert policy.case_scope(lawyer) == lawyer.id

    def test_a_court_representative_is_scoped_to_their_own_assignments(
        self, policy: DashboardAccessPolicy
    ) -> None:
        court = build_user(UserRole.COURT_REPRESENTATIVE)
        assert policy.case_scope(court) == court.id

    def test_the_case_scope_is_the_case_policys_own(
        self, policy: DashboardAccessPolicy
    ) -> None:
        """**The delegation, asserted directly.**

        If Case Management refines who sees which cases, a dashboard must follow —
        and it does, because this returns the case policy's answer rather than one
        of its own.
        """
        from services.case_access import CaseAccessPolicy

        cases = CaseAccessPolicy()
        for role in UserRole:
            user = build_user(role)
            assert policy.case_scope(user) == cases.visibility_scope(user)

    def test_the_owner_scope_is_always_the_caller(
        self, policy: DashboardAccessPolicy
    ) -> None:
        """There is no parameter by which it could be anybody else."""
        for role in UserRole:
            user = build_user(role)
            assert policy.owner_scope(user) == user.id


class TestQuickActionAuthorization:
    """A shortcut respects the permission its destination requires."""

    def test_a_lawyer_may_not_create_a_case(self, policy: DashboardAccessPolicy) -> None:
        lawyer = build_user(UserRole.LAWYER)
        actions = set(policy.quick_actions(policy.permissions_for(lawyer)))
        assert QuickActionKey.CREATE_CASE not in actions

    def test_a_lawyer_may_upload_and_ask(self, policy: DashboardAccessPolicy) -> None:
        lawyer = build_user(UserRole.LAWYER)
        actions = set(policy.quick_actions(policy.permissions_for(lawyer)))
        assert QuickActionKey.UPLOAD_DOCUMENT in actions
        assert QuickActionKey.OPEN_ASSISTANT in actions
        assert QuickActionKey.GENERATE_REPORT in actions

    def test_a_court_representative_gets_the_calendar_and_uploads(
        self, policy: DashboardAccessPolicy
    ) -> None:
        court = build_user(UserRole.COURT_REPRESENTATIVE)
        actions = set(policy.quick_actions(policy.permissions_for(court)))
        assert actions == {QuickActionKey.UPLOAD_DOCUMENT, QuickActionKey.VIEW_CALENDAR}
