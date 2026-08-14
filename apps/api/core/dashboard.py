"""The platform's dashboard vocabulary: widgets, layouts, quick actions, ranges.

``19-dashboard-analytics.md`` asks for a dashboard that is **widget-based**, where
each widget *"owns its own data source, is independently refreshable, respects
authorization, and supports future customization"*, and where *"the
implementation should allow future widgets without redesign"*. This module is
where that lives, and — like :mod:`core.permissions`, :mod:`core.events`, and
:mod:`core.notifications` — it is the **single place** a widget is named.

Nothing here performs I/O, holds a session, or knows what a case is. It is pure
data plus the four derivations everything downstream depends on:

* **What a widget is** — a :class:`WidgetDefinition` in :data:`WIDGETS`: the
  capabilities it requires, the shape of data it returns, how often it is worth
  re-reading, and **which domain events make it stale**. That last field is why
  the client has no widget table of its own: the API tells it what to refresh on,
  so a widget added here appears, authorizes itself, and starts updating live
  without a line changing in `apps/web`.
* **Which widgets a role sees, and in what order** — :data:`ROLE_LAYOUTS`. The
  spec's "Role-Based Dashboard" section, expressed as a layout per role rather
  than as branches inside a service. A role listing a widget is a statement about
  *relevance*; whether it is actually shown is decided by the permissions on the
  widget itself, which is why a court representative's layout may safely mention
  a widget they turn out not to hold.
* **What a person can do from here** — :data:`QUICK_ACTIONS`, each gated on the
  permissions the action itself requires, so the shortcut and the destination can
  never disagree about who may use it.
* **What "recently" means** — :class:`DashboardRange` and :func:`resolve_window`,
  the spec's Today / Last 7 Days / Last 30 Days / Custom filter, resolved once so
  every widget in one response measures the same interval.

**Adding a widget is one entry in** :data:`WIDGETS`, one loader in
:class:`~services.dashboard.DashboardService`, and one line in whichever role
layouts should offer it. Nothing in the router, the schemas, the authorization
layer, or the frontend changes.

**No prose lives here, and that is deliberate.** A widget carries a stable
``key``; its title is a translation key on the client. `ai-workflow-rules.md`
requires every user-facing string to be localizable, and a dashboard whose labels
came from the API would be a dashboard that is only ever in one language — the
same reasoning :mod:`core.notifications` records for rendering a notification at
read time rather than storing its sentence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from core.events import DomainEventType
from core.permissions import Permission
from models.user import UserRole

# --------------------------------------------------------------------------- #
# Widget identity
# --------------------------------------------------------------------------- #


class WidgetGroup(StrEnum):
    """The area of the platform a widget reports on.

    Exactly the seven ``19-dashboard-analytics.md`` lists its minimum widgets
    under. Used for grouping in the response and for the section headings on the
    page; it carries no authorization meaning of its own — that is on the widget.
    """

    GENERAL = "general"
    CASES = "cases"
    COURT = "court"
    DOCUMENTS = "documents"
    AI = "ai"
    TIMELINE = "timeline"
    SYSTEM = "system"


class WidgetKey(StrEnum):
    """Every widget the dashboard can render, named once.

    The value is the widget's stable public identifier: it appears in API
    responses, in ``GET /dashboard/widgets/{key}``, and as the root of the
    frontend's translation keys, so it must not be renamed casually.

    The grouping below follows the spec's own widget list. Note that the two
    activity widgets are **not** duplicates of one another and are named to say
    so: :attr:`RECENT_ACTIVITY` is the list — *what just happened* — while
    :attr:`TIMELINE_ACTIVITY` is the distribution over the selected window —
    *what kind of work has been going on*. One answers "what changed?", the other
    "where is the effort going?", and a dashboard that showed the same rows twice
    would be answering neither.
    """

    # --- General ------------------------------------------------------------ #
    QUICK_ACTIONS = "quick_actions"
    NOTIFICATIONS = "notifications"
    RECENT_ACTIVITY = "recent_activity"

    # --- Cases -------------------------------------------------------------- #
    MY_CASES = "my_cases"
    RECENT_CASES = "recent_cases"
    CASE_STATUS_OVERVIEW = "case_status_overview"
    CASE_ANALYTICS = "case_analytics"

    # --- Court -------------------------------------------------------------- #
    UPCOMING_HEARINGS = "upcoming_hearings"
    HEARING_CALENDAR = "hearing_calendar"

    # --- Documents ---------------------------------------------------------- #
    RECENT_DOCUMENTS = "recent_documents"
    OCR_STATUS = "ocr_status"
    DOCUMENT_ANALYTICS = "document_analytics"

    # --- AI ----------------------------------------------------------------- #
    AI_REPORTS = "ai_reports"
    RECENT_CONVERSATIONS = "recent_conversations"
    AI_ANALYTICS = "ai_analytics"

    # --- Timeline ----------------------------------------------------------- #
    TIMELINE_ACTIVITY = "timeline_activity"

    # --- System (administrative) -------------------------------------------- #
    STORAGE_USAGE = "storage_usage"
    ACTIVE_USERS = "active_users"
    PROCESSING_QUEUES = "processing_queues"


class WidgetPayloadKind(StrEnum):
    """The shape of the data a widget returns.

    Nine shapes for nineteen widgets, and the reuse is the point: a dashboard
    whose every widget had its own response model would be nineteen models for a
    client to learn, and a twentieth for every widget added. A widget declares a
    *kind*, the API discriminates its payload on it (see
    :mod:`schemas.dashboard`), and the frontend has one renderer per kind rather
    than one per widget.

    :attr:`ACTIONS` carries nothing of its own — the quick actions the caller may
    use are resolved on the dashboard as a whole, because the header renders them
    whether or not the widget is on the page.
    """

    #: Named scalar figures — counts, sizes, percentages, durations.
    METRICS = "metrics"
    #: One total split into labelled buckets — statuses, categories, states.
    BREAKDOWN = "breakdown"
    #: A short list of cases.
    CASES = "cases"
    #: A short list of documents.
    DOCUMENTS = "documents"
    #: A short list of AI reports.
    REPORTS = "reports"
    #: A short list of AI conversations.
    CONVERSATIONS = "conversations"
    #: A short list of timeline entries.
    ACTIVITY = "activity"
    #: A short list of the caller's notifications.
    NOTIFICATIONS = "notifications"
    #: No payload; the actions live on the dashboard envelope.
    ACTIONS = "actions"


# --------------------------------------------------------------------------- #
# Widget definitions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WidgetDefinition:
    """Everything the platform knows about one widget without reading a row.

    Frozen, because it is policy rather than state: a service that could edit one
    would be a service that can widen its own authorization.
    """

    key: WidgetKey
    group: WidgetGroup
    kind: WidgetPayloadKind

    #: Capabilities the caller must hold — **all of them**, not any.
    #:
    #: All rather than any, deliberately. A widget that aggregates two features
    #: (``ai_analytics`` counts conversations *and* reports) would otherwise be
    #: offered to somebody holding one of them and quietly report the other's
    #: figures as zero, which is worse than not offering it: the spec requires
    #: that *"aggregated metrics must never leak unauthorized information"*, and a
    #: zero is information.
    #:
    #: Empty means "every authenticated caller", which is true of exactly one
    #: widget (:attr:`WidgetKey.QUICK_ACTIONS`, whose contents gate themselves).
    permissions: frozenset[Permission]

    #: Domain events after which this widget's data may have changed.
    #:
    #: The spec's "Real-Time Updates" section — *"dashboard widgets should
    #: automatically update when relevant events occur"* — declared per widget and
    #: **served to the client**, so the browser refreshes exactly the widgets an
    #: event touched rather than reloading a dashboard because a notification
    #: arrived. Empty means the widget never goes stale from an event and relies
    #: on its interval (or on nothing at all, for the static ones).
    events: frozenset[DomainEventType]

    #: How often a client should re-read this widget when nothing has happened,
    #: in seconds. ``0`` means "never on a timer" — the widget changes only when
    #: an event says so, or not at all.
    #:
    #: A *hint*, not a contract: it is advice to a client, and the API neither
    #: enforces nor remembers it. Widgets whose numbers move continuously (queue
    #: depths, connected users) name a short interval; lists that change when
    #: somebody does something name a long one and lean on :attr:`events`.
    refresh_seconds: int

    #: Whether this widget reports on the **whole platform** rather than on what
    #: the caller is party to.
    #:
    #: Load-bearing in two places. It is what makes a widget cacheable across
    #: callers (see :class:`~services.dashboard.DashboardService`) — a per-user
    #: figure must never be — and it is a standing reminder that such a widget
    #: needs a capability that means "sees everything" rather than one every role
    #: holds.
    platform_wide: bool = False

    def is_visible_to(self, permissions: frozenset[Permission]) -> bool:
        """Whether a caller holding ``permissions`` may be offered this widget."""
        return self.permissions <= permissions


def _widget(
    key: WidgetKey,
    *,
    group: WidgetGroup,
    kind: WidgetPayloadKind,
    permissions: frozenset[Permission] = frozenset(),
    events: frozenset[DomainEventType] = frozenset(),
    refresh_seconds: int = 0,
    platform_wide: bool = False,
) -> WidgetDefinition:
    """Build one widget definition, keyword-only past the key.

    A helper for the same reason :func:`~core.notifications._rule` is one:
    nineteen definitions written as raw constructor calls are nineteen chances to
    transpose two frozensets that both happen to be the right type.
    """
    return WidgetDefinition(
        key=key,
        group=group,
        kind=kind,
        permissions=permissions,
        events=events,
        refresh_seconds=refresh_seconds,
        platform_wide=platform_wide,
    )


#: Events that change *a case as a row*: its status, its priority, its assignees.
_CASE_EVENTS: Final[frozenset[DomainEventType]] = frozenset(
    {
        DomainEventType.CASE_CREATED,
        DomainEventType.CASE_UPDATED,
        DomainEventType.CASE_ARCHIVED,
        DomainEventType.CASE_RESTORED,
        DomainEventType.CASE_STATUS_CHANGED,
        DomainEventType.CASE_PRIORITY_CHANGED,
        DomainEventType.CASE_ASSIGNMENT_CHANGED,
    }
)

#: Events that change which documents exist, or which version is current.
_DOCUMENT_EVENTS: Final[frozenset[DomainEventType]] = frozenset(
    {
        DomainEventType.DOCUMENT_UPLOADED,
        DomainEventType.DOCUMENT_UPDATED,
        DomainEventType.DOCUMENT_REPLACED,
        DomainEventType.DOCUMENT_DELETED,
    }
)

#: Events from the extraction pipeline.
_OCR_EVENTS: Final[frozenset[DomainEventType]] = frozenset(
    {
        DomainEventType.OCR_STARTED,
        DomainEventType.OCR_COMPLETED,
        DomainEventType.OCR_FAILED,
    }
)

#: Events from the indexing pipeline.
_INDEXING_EVENTS: Final[frozenset[DomainEventType]] = frozenset(
    {
        DomainEventType.INDEXING_STARTED,
        DomainEventType.INDEXING_COMPLETED,
        DomainEventType.INDEXING_FAILED,
    }
)

#: Events from report generation. ``REPORT_PROGRESS`` is deliberately **absent**:
#: it fires once per section for the length of a run, and a dashboard tile that
#: counts finished reports learns nothing from a report being half done.
_REPORT_EVENTS: Final[frozenset[DomainEventType]] = frozenset(
    {
        DomainEventType.REPORT_STARTED,
        DomainEventType.REPORT_GENERATED,
        DomainEventType.REPORT_FAILED,
    }
)

#: Events about the caller's own notification feed.
_NOTIFICATION_EVENTS: Final[frozenset[DomainEventType]] = frozenset(
    {DomainEventType.NOTIFICATION_CREATED, DomainEventType.NOTIFICATION_READ}
)


#: Every widget the platform defines.
#:
#: Read-only at runtime (``MappingProxyType`` over frozen dataclasses) so a bug
#: elsewhere cannot widen a widget's audience by mutating policy in place — the
#: same protection :data:`~core.roles.ROLE_PERMISSIONS` has, and for the same
#: reason.
WIDGETS: Mapping[WidgetKey, WidgetDefinition] = MappingProxyType(
    {
        definition.key: definition
        for definition in (
            # --- General ---------------------------------------------------- #
            #
            # The only widget with no permission requirement, because it has no
            # data of its own: what it offers is decided action by action against
            # the capability each action needs (see `QUICK_ACTIONS`), so a caller
            # entitled to nothing is offered nothing and the widget is empty
            # rather than forbidden.
            _widget(
                WidgetKey.QUICK_ACTIONS,
                group=WidgetGroup.GENERAL,
                kind=WidgetPayloadKind.ACTIONS,
            ),
            _widget(
                WidgetKey.NOTIFICATIONS,
                group=WidgetGroup.GENERAL,
                kind=WidgetPayloadKind.NOTIFICATIONS,
                permissions=frozenset({Permission.NOTIFICATIONS_VIEW}),
                events=_NOTIFICATION_EVENTS,
                refresh_seconds=120,
            ),
            # The dashboard's answer to *"what changed recently?"*. Timeline
            # entries rather than domain events, because the timeline is the
            # case's durable history and an event is an ephemeral "this just
            # changed" — a feed built from the second would be empty for anybody
            # who was not connected when it happened.
            _widget(
                WidgetKey.RECENT_ACTIVITY,
                group=WidgetGroup.GENERAL,
                kind=WidgetPayloadKind.ACTIVITY,
                permissions=frozenset({Permission.TIMELINE_VIEW}),
                events=frozenset({DomainEventType.TIMELINE_UPDATED}),
                refresh_seconds=120,
            ),
            # --- Cases ------------------------------------------------------ #
            #
            # *My* cases, and it means it: assigned to the caller personally, even
            # for an administrator holding `cases:view-all`. "Every case on the
            # platform" is what `recent_cases` is for, and collapsing the two
            # would leave an administrator's dashboard answering "what requires my
            # attention?" with the entire caseload.
            _widget(
                WidgetKey.MY_CASES,
                group=WidgetGroup.CASES,
                kind=WidgetPayloadKind.CASES,
                permissions=frozenset({Permission.CASES_VIEW}),
                events=_CASE_EVENTS,
                refresh_seconds=300,
            ),
            _widget(
                WidgetKey.RECENT_CASES,
                group=WidgetGroup.CASES,
                kind=WidgetPayloadKind.CASES,
                permissions=frozenset({Permission.CASES_VIEW}),
                events=_CASE_EVENTS,
                refresh_seconds=300,
            ),
            _widget(
                WidgetKey.CASE_STATUS_OVERVIEW,
                group=WidgetGroup.CASES,
                kind=WidgetPayloadKind.BREAKDOWN,
                permissions=frozenset({Permission.CASES_VIEW}),
                events=_CASE_EVENTS,
                refresh_seconds=300,
            ),
            _widget(
                WidgetKey.CASE_ANALYTICS,
                group=WidgetGroup.CASES,
                kind=WidgetPayloadKind.METRICS,
                permissions=frozenset({Permission.CASES_VIEW}),
                events=_CASE_EVENTS,
                refresh_seconds=300,
            ),
            # --- Court ------------------------------------------------------ #
            _widget(
                WidgetKey.UPCOMING_HEARINGS,
                group=WidgetGroup.COURT,
                kind=WidgetPayloadKind.CASES,
                permissions=frozenset({Permission.CASES_VIEW}),
                events=_CASE_EVENTS,
                refresh_seconds=300,
            ),
            _widget(
                WidgetKey.HEARING_CALENDAR,
                group=WidgetGroup.COURT,
                kind=WidgetPayloadKind.METRICS,
                permissions=frozenset({Permission.CASES_VIEW}),
                events=_CASE_EVENTS,
                refresh_seconds=300,
            ),
            # --- Documents -------------------------------------------------- #
            _widget(
                WidgetKey.RECENT_DOCUMENTS,
                group=WidgetGroup.DOCUMENTS,
                kind=WidgetPayloadKind.DOCUMENTS,
                permissions=frozenset({Permission.DOCUMENTS_VIEW}),
                events=_DOCUMENT_EVENTS,
                refresh_seconds=300,
            ),
            # Gated on `ocr:view` rather than on `documents:view`: the extracted
            # text is a derived artefact with its own capability, and a role that
            # may read a filing but not operate the pipeline should not be handed
            # its failure count.
            _widget(
                WidgetKey.OCR_STATUS,
                group=WidgetGroup.DOCUMENTS,
                kind=WidgetPayloadKind.BREAKDOWN,
                permissions=frozenset({Permission.OCR_VIEW}),
                events=_OCR_EVENTS | _DOCUMENT_EVENTS,
                refresh_seconds=60,
            ),
            # Uploads, extraction, and indexing in one tile, so it needs all
            # three capabilities — see `WidgetDefinition.permissions` for why an
            # aggregate widget requires every one of them rather than any.
            _widget(
                WidgetKey.DOCUMENT_ANALYTICS,
                group=WidgetGroup.DOCUMENTS,
                kind=WidgetPayloadKind.METRICS,
                permissions=frozenset(
                    {
                        Permission.DOCUMENTS_VIEW,
                        Permission.OCR_VIEW,
                        Permission.INDEXING_VIEW,
                    }
                ),
                events=_DOCUMENT_EVENTS | _OCR_EVENTS | _INDEXING_EVENTS,
                refresh_seconds=120,
            ),
            # --- AI --------------------------------------------------------- #
            #
            # `reports:view` is not a row grant — a report belongs to the user who
            # asked for it — so this widget is the caller's own history and
            # nobody else's, exactly as `/reports` is.
            _widget(
                WidgetKey.AI_REPORTS,
                group=WidgetGroup.AI,
                kind=WidgetPayloadKind.REPORTS,
                permissions=frozenset({Permission.REPORTS_VIEW}),
                events=_REPORT_EVENTS,
                refresh_seconds=60,
            ),
            _widget(
                WidgetKey.RECENT_CONVERSATIONS,
                group=WidgetGroup.AI,
                kind=WidgetPayloadKind.CONVERSATIONS,
                permissions=frozenset({Permission.AI_CHAT}),
                refresh_seconds=300,
            ),
            _widget(
                WidgetKey.AI_ANALYTICS,
                group=WidgetGroup.AI,
                kind=WidgetPayloadKind.METRICS,
                permissions=frozenset({Permission.REPORTS_VIEW, Permission.AI_CHAT}),
                events=_REPORT_EVENTS,
                refresh_seconds=120,
            ),
            # --- Timeline --------------------------------------------------- #
            _widget(
                WidgetKey.TIMELINE_ACTIVITY,
                group=WidgetGroup.TIMELINE,
                kind=WidgetPayloadKind.BREAKDOWN,
                permissions=frozenset({Permission.TIMELINE_VIEW}),
                events=frozenset({DomainEventType.TIMELINE_UPDATED}),
                refresh_seconds=300,
            ),
            # --- System ----------------------------------------------------- #
            #
            # Three administrative widgets, and each names the capability that
            # already means what it needs rather than inventing a fourth.
            # `cases:view-all` is the platform's existing "sees everything"
            # capability, so pairing it with `documents:view` is precisely "may
            # read documents, across the whole platform" — which is what a storage
            # figure discloses.
            _widget(
                WidgetKey.STORAGE_USAGE,
                group=WidgetGroup.SYSTEM,
                kind=WidgetPayloadKind.METRICS,
                permissions=frozenset(
                    {Permission.DOCUMENTS_VIEW, Permission.CASES_VIEW_ALL}
                ),
                events=_DOCUMENT_EVENTS,
                refresh_seconds=300,
                platform_wide=True,
            ),
            _widget(
                WidgetKey.ACTIVE_USERS,
                group=WidgetGroup.SYSTEM,
                kind=WidgetPayloadKind.METRICS,
                permissions=frozenset({Permission.USERS_VIEW}),
                refresh_seconds=60,
                platform_wide=True,
            ),
            # The backlogs of the four pipelines that can accumulate one. Gated on
            # the monitoring capabilities that already exist for exactly this kind
            # of view, so a deployment that trusts somebody with `/ocr/metrics`
            # and `/indexing/metrics` is not asked to trust them again.
            _widget(
                WidgetKey.PROCESSING_QUEUES,
                group=WidgetGroup.SYSTEM,
                kind=WidgetPayloadKind.METRICS,
                permissions=frozenset(
                    {Permission.OCR_MONITOR, Permission.INDEXING_MONITOR}
                ),
                refresh_seconds=30,
                platform_wide=True,
            ),
        )
    }
)


class UnknownWidgetError(ValueError):
    """A widget key has no definition in :data:`WIDGETS`.

    A plain ``ValueError`` rather than an :class:`~core.exceptions.AppException`:
    this module is pure vocabulary and has no HTTP opinion. The router translates
    it into a 404 — a widget nobody defined and one the caller may not see are
    deliberately the same answer.
    """


def widget_definition(key: WidgetKey) -> WidgetDefinition:
    """The definition for one widget.

    Raises:
        UnknownWidgetError: the key has no entry. Guarded by an exhaustiveness
            test, so in practice this fires only for a member added to
            :class:`WidgetKey` without a definition beside it.
    """
    try:
        return WIDGETS[key]
    except KeyError as exc:  # pragma: no cover - guarded by an exhaustiveness test
        raise UnknownWidgetError(f"Widget {key.value!r} has no entry in WIDGETS.") from exc


def widget_from_value(value: str) -> WidgetKey | None:
    """Resolve a widget identifier, or ``None`` when it names nothing.

    ``None`` rather than an exception, because the caller of this is a *filter*: a
    client asking for a widget this version does not have should be told about the
    widgets that do exist, not handed a 422 for a stale bookmark.
    """
    try:
        return WidgetKey(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Role layouts
# --------------------------------------------------------------------------- #

#: Which widgets each role is offered, **in the order they are laid out**.
#:
#: The spec's "Role-Based Dashboard" section, as data. Two properties are worth
#: stating because they are what make this table safe:
#:
#: * **It never grants.** A layout is intersected with the caller's permissions,
#:   never unioned, so listing a widget a role does not hold is harmless — it
#:   simply does not appear. That is why the administrator's layout can list every
#:   widget without this table becoming a second, competing policy.
#: * **It is an ordering, and orderings are opinions.** Each role's list starts
#:   with what the spec says that role comes here for: the platform's health for
#:   an administrator, their own caseload for a lawyer, the court diary for a
#:   court representative. The dashboard's stated purpose is *"what requires my
#:   attention?"*, and the answer differs by role in exactly this way.
ROLE_LAYOUTS: Mapping[UserRole, tuple[WidgetKey, ...]] = MappingProxyType(
    {
        # System metrics, active users, storage usage, processing queues, and
        # platform statistics — the spec's administrator list, in its order —
        # before the case-level widgets an administrator also holds.
        UserRole.ADMINISTRATOR: (
            WidgetKey.QUICK_ACTIONS,
            WidgetKey.ACTIVE_USERS,
            WidgetKey.STORAGE_USAGE,
            WidgetKey.PROCESSING_QUEUES,
            WidgetKey.CASE_ANALYTICS,
            WidgetKey.CASE_STATUS_OVERVIEW,
            WidgetKey.DOCUMENT_ANALYTICS,
            WidgetKey.OCR_STATUS,
            WidgetKey.AI_ANALYTICS,
            WidgetKey.UPCOMING_HEARINGS,
            WidgetKey.HEARING_CALENDAR,
            WidgetKey.RECENT_CASES,
            WidgetKey.MY_CASES,
            WidgetKey.RECENT_DOCUMENTS,
            WidgetKey.RECENT_ACTIVITY,
            WidgetKey.TIMELINE_ACTIVITY,
            WidgetKey.NOTIFICATIONS,
            WidgetKey.AI_REPORTS,
            WidgetKey.RECENT_CONVERSATIONS,
        ),
        # Assigned cases, upcoming hearings, AI reports, recent documents, and
        # notifications — the spec's lawyer list, in its order. The platform-wide
        # widgets are absent from the layout as well as unreachable by permission:
        # a lawyer who was granted `users:view` by a future policy would still not
        # find "active users" on their dashboard, because it is not what they come
        # here for.
        UserRole.LAWYER: (
            WidgetKey.QUICK_ACTIONS,
            WidgetKey.MY_CASES,
            WidgetKey.UPCOMING_HEARINGS,
            WidgetKey.AI_REPORTS,
            WidgetKey.RECENT_DOCUMENTS,
            WidgetKey.NOTIFICATIONS,
            WidgetKey.CASE_STATUS_OVERVIEW,
            WidgetKey.HEARING_CALENDAR,
            WidgetKey.RECENT_ACTIVITY,
            WidgetKey.OCR_STATUS,
            WidgetKey.CASE_ANALYTICS,
            WidgetKey.DOCUMENT_ANALYTICS,
            WidgetKey.AI_ANALYTICS,
            WidgetKey.RECENT_CONVERSATIONS,
            WidgetKey.TIMELINE_ACTIVITY,
        ),
        # Hearings, court schedule, and assigned cases — the spec's court list, in
        # its order. Nothing AI-related appears, which costs nothing to state here
        # because `core/roles.py` withholds those capabilities anyway; it is
        # written out so the layout reads as a deliberate role dashboard rather
        # than as whatever survived the permission filter.
        UserRole.COURT_REPRESENTATIVE: (
            WidgetKey.QUICK_ACTIONS,
            WidgetKey.UPCOMING_HEARINGS,
            WidgetKey.HEARING_CALENDAR,
            WidgetKey.MY_CASES,
            WidgetKey.CASE_STATUS_OVERVIEW,
            WidgetKey.NOTIFICATIONS,
            WidgetKey.RECENT_ACTIVITY,
            WidgetKey.RECENT_DOCUMENTS,
            WidgetKey.OCR_STATUS,
            WidgetKey.CASE_ANALYTICS,
            WidgetKey.TIMELINE_ACTIVITY,
        ),
    }
)


def layout_for(role: UserRole) -> tuple[WidgetKey, ...]:
    """The widget order for ``role``.

    Falls back to **every widget in declaration order** for a role with no layout,
    which is the safe direction: the permission filter still applies, so an
    unconfigured role gets a dashboard containing exactly what it is entitled to,
    in an arbitrary order — rather than an empty page, which would look like a
    broken deployment and be reported as one.
    """
    return ROLE_LAYOUTS.get(role, tuple(WIDGETS))


# --------------------------------------------------------------------------- #
# Quick actions
# --------------------------------------------------------------------------- #


class QuickActionKey(StrEnum):
    """The shortcuts the dashboard offers.

    Exactly the five ``19-dashboard-analytics.md`` names. The value is the stable
    identifier the client maps onto a route and a label — **the API deliberately
    returns neither**. A URL is the frontend's business (``lib/routes.ts`` is its
    single source of truth) and a label is a translation key; an API that returned
    "Create Case" would be an API that has to be redeployed to fix a typo in
    Arabic.
    """

    CREATE_CASE = "create_case"
    UPLOAD_DOCUMENT = "upload_document"
    GENERATE_REPORT = "generate_report"
    OPEN_ASSISTANT = "open_assistant"
    VIEW_CALENDAR = "view_calendar"


@dataclass(frozen=True, slots=True)
class QuickActionDefinition:
    """One shortcut and the capabilities it requires."""

    key: QuickActionKey
    #: **All** of these, matching how the destination endpoint is gated. Report
    #: generation requires ``reports:generate`` *and* ``ai:generate-report``
    #: because ``POST /reports`` requires both, and a shortcut that appeared for
    #: somebody holding one of them would be a button that answers 403.
    permissions: frozenset[Permission]

    def is_available_to(self, permissions: frozenset[Permission]) -> bool:
        """Whether a caller holding ``permissions`` may use this action."""
        return self.permissions <= permissions


#: Every quick action, in the order they are offered.
#:
#: A tuple rather than a mapping, because order is the whole of the presentation
#: decision here and a dictionary would leave it to insertion luck.
QUICK_ACTIONS: tuple[QuickActionDefinition, ...] = (
    QuickActionDefinition(
        key=QuickActionKey.CREATE_CASE,
        permissions=frozenset({Permission.CASES_CREATE}),
    ),
    QuickActionDefinition(
        key=QuickActionKey.UPLOAD_DOCUMENT,
        permissions=frozenset({Permission.DOCUMENTS_UPLOAD}),
    ),
    QuickActionDefinition(
        key=QuickActionKey.GENERATE_REPORT,
        permissions=frozenset(
            {Permission.REPORTS_GENERATE, Permission.AI_GENERATE_REPORT}
        ),
    ),
    QuickActionDefinition(
        key=QuickActionKey.OPEN_ASSISTANT,
        permissions=frozenset({Permission.AI_CHAT}),
    ),
    QuickActionDefinition(
        key=QuickActionKey.VIEW_CALENDAR,
        permissions=frozenset({Permission.CASES_VIEW}),
    ),
)


def available_actions(permissions: frozenset[Permission]) -> tuple[QuickActionKey, ...]:
    """The quick actions a caller holding ``permissions`` may use."""
    return tuple(
        action.key for action in QUICK_ACTIONS if action.is_available_to(permissions)
    )


# --------------------------------------------------------------------------- #
# Time filters
# --------------------------------------------------------------------------- #


class DashboardRange(StrEnum):
    """The window analytics are measured over.

    Exactly the four the spec's "Time Filters" section names. It is resolved
    **once per request** (:func:`resolve_window`) and handed to every widget, so
    two tiles in one response can never be measuring different fortnights — which
    is the spec's *"widgets should update consistently when filters change"* made
    structural rather than promised.
    """

    TODAY = "today"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    CUSTOM = "custom"


#: How many days each fixed range covers, counting today.
_RANGE_DAYS: Mapping[DashboardRange, int] = MappingProxyType(
    {
        DashboardRange.TODAY: 1,
        DashboardRange.LAST_7_DAYS: 7,
        DashboardRange.LAST_30_DAYS: 30,
    }
)


class InvalidDashboardWindowError(ValueError):
    """A custom range is missing a bound, inverted, or longer than permitted.

    A plain ``ValueError`` for the reason :class:`UnknownWidgetError` is one; the
    schema layer turns it into a 422 before the service ever sees it, and this
    exists so the rule is enforced at the vocabulary rather than only at the edge.
    """


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """The half-open interval ``[start, end)`` a dashboard reports on.

    **Half-open, and in UTC.** Closed-at-both-ends windows double-count the
    boundary instant when two requests are made back to back, and a dashboard
    whose "today" total is one higher than its list is a dashboard nobody trusts.
    UTC because every timestamp on this platform is stored that way; presenting it
    in the reader's timezone is the client's job, and doing it here would make an
    aggregate depend on who asked for it.
    """

    start: datetime
    end: datetime
    range: DashboardRange

    @property
    def days(self) -> int:
        """Whole days the window spans, at least one."""
        return max(1, (self.end - self.start).days)


def resolve_window(
    dashboard_range: DashboardRange,
    *,
    start: date | None = None,
    end: date | None = None,
    max_days: int,
    now: datetime | None = None,
) -> TimeWindow:
    """Turn a range — and, for ``custom``, two dates — into one interval.

    The fixed ranges are anchored to **midnight UTC** rather than to "now minus
    seven days", because a filter labelled *Last 7 Days* that quietly means *the
    past 168 hours* produces a "today" bucket that shrinks as the morning goes on.
    ``today`` is therefore the current UTC day, and ``last_7_days`` is that day
    plus the six before it.

    Args:
        dashboard_range: which window.
        start: first day, inclusive. Required for :attr:`DashboardRange.CUSTOM`.
        end: last day, inclusive. Required for :attr:`DashboardRange.CUSTOM`.
        max_days: longest custom range this deployment permits.
        now: the current instant; injectable so the fixed ranges are testable
            without freezing the clock globally.

    Raises:
        InvalidDashboardWindowError: a custom range with a missing bound, an end
            before its start, or a span past ``max_days``.
    """
    moment = now or datetime.now(UTC)
    today = moment.date()

    if dashboard_range is not DashboardRange.CUSTOM:
        span = _RANGE_DAYS[dashboard_range]
        first_day = today - timedelta(days=span - 1)
        return TimeWindow(
            start=_midnight(first_day),
            # Tomorrow's midnight: the window must include everything that
            # happens for the rest of today, and an end of "now" would make a
            # dashboard loaded twice a minute apart report two different totals
            # for the same finished day.
            end=_midnight(today + timedelta(days=1)),
            range=dashboard_range,
        )

    if start is None or end is None:
        raise InvalidDashboardWindowError(
            "A custom range requires both a start and an end date."
        )
    if end < start:
        raise InvalidDashboardWindowError("The end date must not precede the start date.")

    span = (end - start).days + 1
    if span > max_days:
        raise InvalidDashboardWindowError(
            f"A custom range may cover at most {max_days} days."
        )

    return TimeWindow(
        start=_midnight(start),
        end=_midnight(end + timedelta(days=1)),
        range=DashboardRange.CUSTOM,
    )


def _midnight(day: date) -> datetime:
    """The first instant of ``day``, in UTC."""
    return datetime.combine(day, time.min, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Metrics and buckets
# --------------------------------------------------------------------------- #


class MetricUnit(StrEnum):
    """How a metric's number should be read.

    Returned beside every figure so the client formats it without a table of its
    own: a byte count becomes "1.4 GB", a percentage gets its sign, a duration
    gets its unit. Without this the frontend would need a second copy of the
    widget catalog just to know that ``storage_bytes`` is not a count.
    """

    COUNT = "count"
    BYTES = "bytes"
    PERCENT = "percent"
    DAYS = "days"
    MILLISECONDS = "milliseconds"


@dataclass(frozen=True, slots=True)
class Metric:
    """One named figure inside a metrics widget.

    ``key`` is stable and translatable; ``value`` is a number and never a
    sentence. ``None`` is a legitimate value and means *undefined* rather than
    zero — an average over no observations, a rate with no denominator — which is
    the distinction ``19-dashboard-analytics.md``'s "Analytics Data Integrity"
    section is really about: a fabricated zero reads as a fact.
    """

    key: str
    value: float | int | None
    unit: MetricUnit = MetricUnit.COUNT


@dataclass(frozen=True, slots=True)
class Bucket:
    """One labelled slice of a breakdown widget."""

    key: str
    count: int


__all__ = [
    "QUICK_ACTIONS",
    "ROLE_LAYOUTS",
    "WIDGETS",
    "Bucket",
    "DashboardRange",
    "InvalidDashboardWindowError",
    "Metric",
    "MetricUnit",
    "QuickActionDefinition",
    "QuickActionKey",
    "TimeWindow",
    "UnknownWidgetError",
    "WidgetDefinition",
    "WidgetGroup",
    "WidgetKey",
    "WidgetPayloadKind",
    "available_actions",
    "layout_for",
    "resolve_window",
    "widget_definition",
    "widget_from_value",
]
