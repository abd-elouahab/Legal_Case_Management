"""Unit tests for the dashboard's vocabulary.

:mod:`core.dashboard` is pure data plus four derivations, and each of them is a
requirement of ``19-dashboard-analytics.md`` made mechanical — so each is
asserted here rather than left to the integration suite to discover:

* the **widget catalog** is exhaustive and internally consistent, because a
  widget with no definition is a page that raises for whoever holds the role that
  lists it;
* the **role layouts** never grant, only order — the property that makes them
  safe to edit without reviewing them as security policy;
* the **quick actions** require exactly what their destinations require;
* the **time filter** resolves to one window per request, anchored to whole days.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from core.dashboard import (
    QUICK_ACTIONS,
    ROLE_LAYOUTS,
    WIDGETS,
    DashboardRange,
    InvalidDashboardWindowError,
    MetricUnit,
    QuickActionKey,
    WidgetKey,
    WidgetPayloadKind,
    available_actions,
    layout_for,
    resolve_window,
    widget_definition,
    widget_from_value,
)
from core.permissions import ALL_PERMISSIONS, Permission
from core.roles import permissions_for_role
from models.user import UserRole


class TestWidgetCatalog:
    """Every widget is defined, and every definition is usable."""

    def test_every_widget_key_has_a_definition(self) -> None:
        """A key with no definition is a 500 for whoever holds that role."""
        missing = [key.value for key in WidgetKey if key not in WIDGETS]
        assert missing == []

    def test_definitions_are_keyed_by_their_own_key(self) -> None:
        for key, definition in WIDGETS.items():
            assert definition.key is key

    def test_every_widget_declares_a_known_payload_kind(self) -> None:
        for definition in WIDGETS.values():
            assert isinstance(definition.kind, WidgetPayloadKind)

    def test_every_declared_permission_exists(self) -> None:
        """A widget gated on a permission nobody defines is a widget nobody sees."""
        for definition in WIDGETS.values():
            assert definition.permissions <= ALL_PERMISSIONS

    def test_only_quick_actions_is_ungated(self) -> None:
        """The one widget every authenticated caller gets, and it carries no data."""
        ungated = {
            definition.key for definition in WIDGETS.values() if not definition.permissions
        }
        assert ungated == {WidgetKey.QUICK_ACTIONS}

    def test_platform_wide_widgets_require_a_platform_wide_capability(self) -> None:
        """A cross-platform figure must not be reachable with a per-case grant.

        The three system widgets are the only ones whose numbers are not scoped to
        the caller's cases, so each must require a capability that means "sees
        everything" or "administers accounts" — never a capability a lawyer holds.
        """
        lawyer = permissions_for_role(UserRole.LAWYER)

        for definition in WIDGETS.values():
            if definition.platform_wide:
                assert not definition.is_visible_to(lawyer), definition.key

    def test_widget_from_value_resolves_known_keys(self) -> None:
        assert widget_from_value("my_cases") is WidgetKey.MY_CASES

    def test_widget_from_value_returns_none_for_an_unknown_key(self) -> None:
        """A stale bookmark is filtered out, not answered with a 422."""
        assert widget_from_value("crystal_ball") is None

    def test_widget_definition_returns_the_entry(self) -> None:
        assert widget_definition(WidgetKey.OCR_STATUS).key is WidgetKey.OCR_STATUS


class TestRoleLayouts:
    """A layout orders widgets; it never grants one."""

    @pytest.mark.parametrize("role", list(UserRole))
    def test_every_role_has_a_layout(self, role: UserRole) -> None:
        assert role in ROLE_LAYOUTS

    @pytest.mark.parametrize("role", list(UserRole))
    def test_layouts_reference_only_defined_widgets(self, role: UserRole) -> None:
        for key in layout_for(role):
            assert key in WIDGETS

    @pytest.mark.parametrize("role", list(UserRole))
    def test_layouts_have_no_duplicates(self, role: UserRole) -> None:
        """A widget listed twice would be rendered twice."""
        layout = layout_for(role)
        assert len(layout) == len(set(layout))

    def test_a_layout_cannot_widen_a_role(self) -> None:
        """**The property that makes layouts safe to edit.**

        A layout is intersected with the caller's permissions, so listing a widget
        a role does not hold is harmless. This asserts the *filter* rather than the
        list: whatever the layout says, what a role can actually see is bounded by
        what it holds.
        """
        for role in UserRole:
            granted = permissions_for_role(role)
            visible = [
                key for key in layout_for(role) if widget_definition(key).is_visible_to(granted)
            ]
            for key in visible:
                assert widget_definition(key).permissions <= granted

    def test_the_administrator_layout_leads_with_the_platform(self) -> None:
        """The spec's administrator dashboard is system metrics first."""
        layout = layout_for(UserRole.ADMINISTRATOR)
        system = [WidgetKey.ACTIVE_USERS, WidgetKey.STORAGE_USAGE, WidgetKey.PROCESSING_QUEUES]
        assert layout[1 : 1 + len(system)] == tuple(system)

    def test_the_lawyer_layout_leads_with_their_own_work(self) -> None:
        layout = layout_for(UserRole.LAWYER)
        assert layout[1] is WidgetKey.MY_CASES
        assert layout[2] is WidgetKey.UPCOMING_HEARINGS

    def test_the_court_layout_leads_with_hearings(self) -> None:
        layout = layout_for(UserRole.COURT_REPRESENTATIVE)
        assert layout[1] is WidgetKey.UPCOMING_HEARINGS
        assert layout[2] is WidgetKey.HEARING_CALENDAR

    def test_court_representatives_are_offered_no_ai_widgets(self) -> None:
        """They hold no AI capability, so no AI widget may survive the filter."""
        granted = permissions_for_role(UserRole.COURT_REPRESENTATIVE)
        visible = [
            key
            for key in layout_for(UserRole.COURT_REPRESENTATIVE)
            if widget_definition(key).is_visible_to(granted)
        ]
        assert WidgetKey.AI_REPORTS not in visible
        assert WidgetKey.RECENT_CONVERSATIONS not in visible
        assert WidgetKey.AI_ANALYTICS not in visible


class TestQuickActions:
    """A shortcut requires exactly what its destination requires."""

    def test_every_action_declares_known_permissions(self) -> None:
        for action in QUICK_ACTIONS:
            assert action.permissions <= ALL_PERMISSIONS
            assert action.permissions, action.key

    def test_report_generation_requires_both_capabilities(self) -> None:
        """`POST /reports` requires both, so a shortcut that needed one would 403."""
        action = next(a for a in QUICK_ACTIONS if a.key is QuickActionKey.GENERATE_REPORT)
        assert action.permissions == frozenset(
            {Permission.REPORTS_GENERATE, Permission.AI_GENERATE_REPORT}
        )

    def test_an_administrator_is_offered_every_action(self) -> None:
        assert set(available_actions(ALL_PERMISSIONS)) == {a.key for a in QUICK_ACTIONS}

    def test_a_court_representative_is_offered_neither_ai_action(self) -> None:
        actions = set(available_actions(permissions_for_role(UserRole.COURT_REPRESENTATIVE)))
        assert QuickActionKey.OPEN_ASSISTANT not in actions
        assert QuickActionKey.GENERATE_REPORT not in actions

    def test_actions_are_returned_in_declaration_order(self) -> None:
        """Order is the whole presentation decision here, so it is not incidental."""
        actions = available_actions(ALL_PERMISSIONS)
        assert list(actions) == [a.key for a in QUICK_ACTIONS]

    def test_a_caller_with_nothing_is_offered_nothing(self) -> None:
        assert available_actions(frozenset()) == ()


class TestTimeWindows:
    """One window per request, anchored to whole UTC days."""

    NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

    def test_today_spans_the_current_utc_day(self) -> None:
        window = resolve_window(DashboardRange.TODAY, max_days=366, now=self.NOW)
        assert window.start == datetime(2026, 8, 10, tzinfo=UTC)
        assert window.end == datetime(2026, 8, 11, tzinfo=UTC)

    def test_last_7_days_includes_today(self) -> None:
        """Six days back plus today, not "168 hours ago"."""
        window = resolve_window(DashboardRange.LAST_7_DAYS, max_days=366, now=self.NOW)
        assert window.start == datetime(2026, 8, 4, tzinfo=UTC)
        assert window.end == datetime(2026, 8, 11, tzinfo=UTC)
        assert window.days == 7

    def test_last_30_days_includes_today(self) -> None:
        window = resolve_window(DashboardRange.LAST_30_DAYS, max_days=366, now=self.NOW)
        assert window.days == 30

    def test_a_fixed_window_does_not_move_within_a_day(self) -> None:
        """Two loads an hour apart must report the same totals for a finished day."""
        morning = resolve_window(
            DashboardRange.LAST_7_DAYS,
            max_days=366,
            now=datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
        )
        evening = resolve_window(
            DashboardRange.LAST_7_DAYS,
            max_days=366,
            now=datetime(2026, 8, 10, 20, 0, tzinfo=UTC),
        )
        assert morning == evening

    def test_a_custom_window_is_inclusive_of_both_days(self) -> None:
        window = resolve_window(
            DashboardRange.CUSTOM,
            start=date(2026, 8, 1),
            end=date(2026, 8, 3),
            max_days=366,
        )
        assert window.start == datetime(2026, 8, 1, tzinfo=UTC)
        assert window.end == datetime(2026, 8, 4, tzinfo=UTC)
        assert window.days == 3

    def test_a_custom_window_needs_both_bounds(self) -> None:
        with pytest.raises(InvalidDashboardWindowError):
            resolve_window(DashboardRange.CUSTOM, start=date(2026, 8, 1), max_days=366)

    def test_a_custom_window_may_not_be_inverted(self) -> None:
        with pytest.raises(InvalidDashboardWindowError):
            resolve_window(
                DashboardRange.CUSTOM,
                start=date(2026, 8, 5),
                end=date(2026, 8, 1),
                max_days=366,
            )

    def test_a_custom_window_is_bounded(self) -> None:
        with pytest.raises(InvalidDashboardWindowError):
            resolve_window(
                DashboardRange.CUSTOM,
                start=date(2020, 1, 1),
                end=date(2026, 1, 1),
                max_days=366,
            )

    def test_a_single_day_custom_window_is_permitted(self) -> None:
        window = resolve_window(
            DashboardRange.CUSTOM,
            start=date(2026, 8, 1),
            end=date(2026, 8, 1),
            max_days=366,
        )
        assert window.days == 1


class TestMetricUnits:
    """The unit is what lets a client format a figure without a table of its own."""

    def test_every_unit_is_a_plain_string(self) -> None:
        for unit in MetricUnit:
            assert isinstance(unit.value, str)
