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
    SEARCH = "search"
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

    # --- Semantic search ----------------------------------------------------- #
    #: Run a natural-language search over indexed documents. Separate from
    #: ``documents:view`` because retrieval is a distinct capability with its own
    #: endpoint and its own cost (a query embedding per request) — but note that
    #: holding it grants nothing on its own: every result is scoped to the cases
    #: the caller is party to, in the vector query itself (see
    #: :mod:`services.search_access`). It is *narrower* than reading a document,
    #: never wider: a search can only ever return passages of documents the
    #: caller could already open.
    SEARCH_QUERY = "search:query"
    #: Read platform-wide search metrics — search count, average latency, average
    #: relevance, failures. Not scoped to a case, so it is deliberately an
    #: administrative capability rather than something every searcher holds.
    SEARCH_MONITOR = "search:monitor"

    # --- Timeline ----------------------------------------------------------- #
    TIMELINE_VIEW = "timeline:view"
    TIMELINE_CREATE = "timeline:create"

    # --- Reports ------------------------------------------------------------ #
    #: Read, export, and delete the reports in one's **own** history. Note that
    #: holding it grants nothing on its own: every read in
    #: :mod:`repositories.report` is keyed by the requester, so a report belongs
    #: to exactly one user and is invisible to everyone else — the same shape a
    #: conversation has, and the reason there is no ``reports:view-all``.
    REPORTS_VIEW = "reports:view"
    #: Ask the platform to generate a report for a case. Held alongside
    #: ``ai:generate-report``, and **both are required** — see
    #: :mod:`api.v1.reports.router` for why. Withheld from court
    #: representatives, exactly as every other AI capability is.
    REPORTS_GENERATE = "reports:generate"
    #: Read platform-wide report metrics — reports generated, average generation
    #: time, exports, failures, average report size, token usage. Not scoped to a
    #: case or to a user, so it is deliberately an administrative capability
    #: rather than something every report author holds — exactly like
    #: ``ocr:monitor``, ``indexing:monitor``, ``search:monitor``, and
    #: ``ai:monitor``.
    REPORTS_MONITOR = "reports:monitor"

    # --- Notifications ------------------------------------------------------ #
    NOTIFICATIONS_VIEW = "notifications:view"
    NOTIFICATIONS_MANAGE = "notifications:manage"

    # --- AI ----------------------------------------------------------------- #
    #: Put a question to the RAG pipeline and receive a grounded, cited answer.
    #: Separate from ``ai:chat`` because the two are different capabilities: this
    #: one is a single stateless question against the pipeline, while ``ai:chat``
    #: is the assistant's conversational surface with its persisted history. A
    #: deployment may reasonably grant one and withhold the other.
    #:
    #: Note that holding it grants nothing on its own: the pipeline retrieves
    #: **only** through :class:`~services.search.SearchService`, so every passage
    #: an answer is built from is one the caller could already open, and a caller
    #: party to no case receives "no supporting information found" rather than an
    #: answer drawn from somebody else's file.
    AI_ASK = "ai:ask"
    AI_CHAT = "ai:chat"
    AI_GENERATE_REPORT = "ai:generate-report"
    #: Read platform-wide RAG metrics — request counts, latency, retrieval
    #: latency, token usage, grounding rate, failures. Not scoped to a case, so
    #: it is deliberately an administrative capability rather than something
    #: every questioner holds, exactly like ``search:monitor`` and
    #: ``indexing:monitor``.
    AI_MONITOR = "ai:monitor"

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
