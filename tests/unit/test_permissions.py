"""Tests for the centralized permission catalog.

These guard the properties every other authorization check relies on: that
identifiers are unique and well-formed, that a permission's group is derivable
from its identifier, and that unknown identifiers fail loudly instead of
silently evaluating to "not granted".
"""

from __future__ import annotations

import pytest

from core.exceptions import AuthorizationConfigurationError
from core.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_SEPARATOR,
    Permission,
    PermissionGroup,
    permission_from_value,
    permissions_in_group,
    sort_permissions,
)


class TestPermissionIdentifiers:
    def test_every_permission_has_a_unique_identifier(self) -> None:
        values = [permission.value for permission in Permission]

        assert len(values) == len(set(values))

    def test_every_identifier_is_group_colon_action(self) -> None:
        for permission in Permission:
            group, _, action = permission.value.partition(PERMISSION_SEPARATOR)

            assert group, f"{permission!r} has no group segment"
            assert action, f"{permission!r} has no action segment"
            assert PERMISSION_SEPARATOR not in action, f"{permission!r} has more than one separator"

    def test_group_and_action_are_derived_from_the_identifier(self) -> None:
        assert Permission.CASES_VIEW.group is PermissionGroup.CASES
        assert Permission.CASES_VIEW.action == "view"
        # A hyphenated action must survive the split intact.
        assert Permission.AI_GENERATE_REPORT.group is PermissionGroup.AI
        assert Permission.AI_GENERATE_REPORT.action == "generate-report"

    def test_every_group_prefix_is_a_declared_group(self) -> None:
        declared = {group.value for group in PermissionGroup}

        assert {permission.group.value for permission in Permission} <= declared

    def test_every_declared_group_has_at_least_one_permission(self) -> None:
        # An empty group means a permission was renamed or removed and its group
        # left behind.
        for group in PermissionGroup:
            assert permissions_in_group(group), f"{group!r} has no permissions"

    @pytest.mark.parametrize(
        "identifier",
        [
            "users:create",
            "users:view",
            "users:update",
            "users:delete",
            "cases:create",
            "cases:view",
            "cases:update",
            "cases:delete",
            "cases:assign",
            # Added by Case Management: the row scope of `cases:view` and the
            # narrow, court-facing half of `cases:update`.
            "cases:view-all",
            "cases:update-hearing",
            "documents:upload",
            "documents:view",
            "documents:update",
            "documents:delete",
            "timeline:view",
            "timeline:create",
            "reports:view",
            "reports:generate",
            "notifications:view",
            "notifications:manage",
            "ai:chat",
            "ai:generate-report",
            "settings:view",
            "settings:update",
        ],
    )
    def test_the_specified_permission_exists(self, identifier: str) -> None:
        # The identifiers are part of the API contract and appear in the
        # frontend's navigation config, so renaming one is a breaking change.
        assert permission_from_value(identifier).value == identifier

    def test_all_permissions_contains_every_member(self) -> None:
        assert frozenset(Permission) == ALL_PERMISSIONS


class TestPermissionLookup:
    def test_unknown_identifier_fails_as_a_configuration_error(self) -> None:
        with pytest.raises(AuthorizationConfigurationError) as excinfo:
            permission_from_value("cases:teleport")

        # 500, not 403: nobody *configured* this permission, so denying access
        # would hide a bug behind a plausible-looking authorization failure.
        assert excinfo.value.status_code == 500

    def test_the_response_message_never_names_the_unknown_permission(self) -> None:
        with pytest.raises(AuthorizationConfigurationError) as excinfo:
            permission_from_value("cases:teleport")

        assert "teleport" not in excinfo.value.message
        assert "teleport" in excinfo.value.detail

    def test_permissions_in_group_returns_only_that_group(self) -> None:
        timeline = permissions_in_group(PermissionGroup.TIMELINE)

        assert timeline == {Permission.TIMELINE_VIEW, Permission.TIMELINE_CREATE}


class TestSorting:
    def test_sorting_is_stable_and_alphabetical(self) -> None:
        unordered = {Permission.SETTINGS_VIEW, Permission.AI_CHAT, Permission.CASES_VIEW}

        assert sort_permissions(unordered) == [
            Permission.AI_CHAT,
            Permission.CASES_VIEW,
            Permission.SETTINGS_VIEW,
        ]

    def test_sorting_the_full_catalog_is_deterministic(self) -> None:
        # Sets have no order, so an unsorted response body would vary between
        # calls and make cached payloads and diffs noisy.
        assert sort_permissions(ALL_PERMISSIONS) == sort_permissions(ALL_PERMISSIONS)
