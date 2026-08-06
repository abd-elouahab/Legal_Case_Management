"""Centralized permission definitions.

Every capability the platform can grant is named exactly once, here. Nothing
else in the codebase may invent a permission string: features import a
:class:`Permission` member instead, so a typo is a static error rather than a
silent authorization hole.

Permissions are intentionally *capability* names (``cases:view``), not role
names. Roles are mapped onto sets of these in :mod:`core.roles`, which is what
keeps role policy changeable without touching the code that enforces it.

Extending the system: add a member below (and, if it opens a new area, a
:class:`PermissionGroup`), then grant it to the appropriate roles in
:mod:`core.roles`. No other module needs to change.
"""

from __future__ import annotations

from enum import StrEnum

from core.exceptions import AuthorizationConfigurationError

#: Separator between a permission's group and its action (``cases:view``).
PERMISSION_SEPARATOR = ":"


class PermissionGroup(StrEnum):
    """Functional area a permission belongs to.

    Used for grouping in the API catalog and for bulk lookups; it is derived from
    the permission identifier itself, so the two can never disagree.
    """

    USERS = "users"
    CASES = "cases"
    DOCUMENTS = "documents"
    OCR = "ocr"
    INDEXING = "indexing"
    TIMELINE = "timeline"
    REPORTS = "reports"
    NOTIFICATIONS = "notifications"
    AI = "ai"
    SETTINGS = "settings"


class Permission(StrEnum):
    """A single, uniquely identified capability.

    The value is the permission's stable public identifier: it appears in API
    responses and in the frontend's navigation configuration, so it must not be
    renamed casually.
    """

    # --- User management ---------------------------------------------------- #
    USERS_CREATE = "users:create"
    USERS_VIEW = "users:view"
    USERS_UPDATE = "users:update"
    USERS_DELETE = "users:delete"

    # --- Case management ---------------------------------------------------- #
    CASES_CREATE = "cases:create"
    CASES_VIEW = "cases:view"
    CASES_UPDATE = "cases:update"
    CASES_DELETE = "cases:delete"
    CASES_ASSIGN = "cases:assign"
    #: Lifts the per-resource restriction on ``cases:view``: the holder reads
    #: every case, not only the ones they are assigned to. Without it,
    #: ``cases:view`` still grants the case-viewing *capability* — it just scopes
    #: the rows to the caller's assignments (see :mod:`services.case_access`).
    #: Modelling "sees everything" as a capability rather than as a role check is
    #: what lets a future supervising role be added by policy alone.
    CASES_VIEW_ALL = "cases:view-all"
    #: The narrow half of ``cases:update``: the court-facing fields of a case —
    #: court name, filing date, next hearing date, and the status changes that
    #: follow from them. Held by court representatives, whose role description is
    #: "update hearing-related information", not "edit the case".
    CASES_UPDATE_HEARING = "cases:update-hearing"

    # --- Document management ------------------------------------------------ #
    DOCUMENTS_UPLOAD = "documents:upload"
    DOCUMENTS_VIEW = "documents:view"
    DOCUMENTS_UPDATE = "documents:update"
    DOCUMENTS_DELETE = "documents:delete"

    # --- OCR processing ------------------------------------------------------ #
    #: Read a document's OCR status, metadata, and extracted text. Separate from
    #: ``documents:view`` because the extracted text is a *derived* artefact with
    #: its own endpoints — but note that holding this grants nothing on its own:
    #: OCR access follows the document, which follows its case (see
    #: :mod:`services.ocr_access`).
    OCR_VIEW = "ocr:view"
    #: Re-run extraction for a document already uploaded. Narrower than
    #: ``ocr:view`` because a retry consumes real processing capacity, which is
    #: the only reason reading and re-running are two permissions rather than one.
    OCR_RETRY = "ocr:retry"
    #: Read platform-wide OCR metrics — success rate, failure rate, average
    #: processing time. Not scoped to a case, so it is deliberately an
    #: administrative capability rather than something every document reader
    #: holds.
    OCR_MONITOR = "ocr:monitor"

    # --- Document indexing --------------------------------------------------- #
    #: Read a document's search-index status and metadata. Separate from
    #: ``ocr:view`` because the index is a *further* derived artefact with its own
    #: endpoints — but note that holding this grants nothing on its own: index
    #: access follows the document, which follows its case (see
    #: :mod:`services.indexing_access`).
    INDEXING_VIEW = "indexing:view"
    #: Rebuild a document's index. Narrower than ``indexing:view`` because a
    #: re-index re-embeds every passage of the document, which is by far the most
    #: expensive operation the platform performs — the same reasoning that makes
    #: ``ocr:retry`` narrower than ``ocr:view``, only more so.
    INDEXING_REINDEX = "indexing:reindex"
    #: Read platform-wide indexing metrics — indexed documents, indexed chunks,
    #: average duration, failures. Not scoped to a case, so it is deliberately an
    #: administrative capability rather than something every document reader
    #: holds.
    INDEXING_MONITOR = "indexing:monitor"

    # --- Timeline ----------------------------------------------------------- #
    TIMELINE_VIEW = "timeline:view"
    TIMELINE_CREATE = "timeline:create"

    # --- Reports ------------------------------------------------------------ #
    REPORTS_VIEW = "reports:view"
    REPORTS_GENERATE = "reports:generate"

    # --- Notifications ------------------------------------------------------ #
    NOTIFICATIONS_VIEW = "notifications:view"
    NOTIFICATIONS_MANAGE = "notifications:manage"

    # --- AI ----------------------------------------------------------------- #
    AI_CHAT = "ai:chat"
    AI_GENERATE_REPORT = "ai:generate-report"

    # --- Settings ----------------------------------------------------------- #
    SETTINGS_VIEW = "settings:view"
    SETTINGS_UPDATE = "settings:update"

    @property
    def group(self) -> PermissionGroup:
        """The functional area this permission belongs to."""
        return PermissionGroup(self.value.split(PERMISSION_SEPARATOR, 1)[0])

    @property
    def action(self) -> str:
        """The verb part of the identifier (``view``, ``generate-report``)."""
        return self.value.split(PERMISSION_SEPARATOR, 1)[1]


#: Every permission the platform defines. An administrator holds exactly this set,
#: so new permissions are granted to administrators automatically.
ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)


def permissions_in_group(group: PermissionGroup) -> frozenset[Permission]:
    """Every permission belonging to one functional area."""
    return frozenset(permission for permission in Permission if permission.group is group)


def permission_from_value(value: str) -> Permission:
    """Resolve a permission identifier, rejecting unknown ones.

    Raises:
        AuthorizationConfigurationError: ``value`` is not a defined permission.
            This is a programming or configuration fault — a client never chooses
            a permission identifier — so it surfaces as a generic server error
            rather than telling the caller which identifiers exist.
    """
    try:
        return Permission(value)
    except ValueError as exc:
        raise AuthorizationConfigurationError(detail=f"Unknown permission {value!r}.") from exc


def sort_permissions(permissions: frozenset[Permission] | set[Permission]) -> list[Permission]:
    """Return permissions in a stable, human-readable order.

    Sets have no order, so responses would otherwise vary between calls and make
    diffs and cached payloads noisy.
    """
    return sorted(permissions, key=lambda permission: permission.value)
