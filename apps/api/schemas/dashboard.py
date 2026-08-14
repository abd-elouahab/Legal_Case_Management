"""Dashboard request and response schemas.

Two responsibilities, both required by ``code-standards.md`` ("validate every
request using Pydantic models", "return standardized API response structures"):

* **Validation** — the time filter and its custom bounds, the requested widget
  subset, and the row count are enforced here, so routes stay thin and every
  rejection comes back in the same envelope.
* **Serialization** — a widget's payload is projected onto one of nine
  :class:`~core.dashboard.WidgetPayloadKind` shapes, discriminated on ``kind``.
  A client written against those nine renders a widget added next year without a
  change, which is the spec's *"the implementation should support additional
  widgets later"* extended from the server to the browser.

**Three things are deliberately absent from every response here.**

*No prose.* A widget carries a stable ``key``, a metric carries a stable ``key``,
and a bucket carries a stable ``key``; the words are translation keys on the
client. `ai-workflow-rules.md` requires every user-facing string to be
localizable, and a dashboard whose labels arrived from the API would be a
dashboard that is only ever in one language.

*No sentence-shaped errors.* An unavailable widget carries a
:class:`~services.dashboard_metrics.WidgetFailureReason` code, never a message,
so the client can explain a failure in the reader's language and the server never
leaks what went wrong internally.

*No content.* The summaries below carry identifiers, statuses, dates, and names
that already appear in list views the caller can open. A case's description, a
document's extracted text, a report's sections, and a conversation's messages are
**not** on a dashboard: a widget is an index, and the modules that own those
resources are where they are read — under their own authorization, one at a time.

Business rules that need the database — which widgets a role sees, what each one
counts, whose rows it may count — live in :mod:`core.dashboard`,
:mod:`services.dashboard_access`, and :mod:`services.dashboard` and cannot be
expressed here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.config import settings
from core.dashboard import (
    DashboardRange,
    MetricUnit,
    QuickActionKey,
    WidgetGroup,
    WidgetKey,
    WidgetPayloadKind,
)
from core.timeline import category_for
from models.case import Case, CasePriority, CaseStatus
from models.document import DocumentCategory
from models.report import ReportStatus, ReportType
from models.timeline import TimelineEvent, TimelineEventCategory
from models.user import User
from schemas.notification import NotificationRead
from services.dashboard import Dashboard, WidgetPayload, WidgetResult, WidgetStateValue

# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class DashboardQuery(BaseModel):
    """Validated query parameters for ``GET /dashboard``.

    Note what is **not** here: there is no ``user_id``, no ``role``, and no
    ``case_id`` scope. A dashboard is the caller's own by construction — the
    scopes come from :class:`~services.dashboard_access.DashboardAccessPolicy`
    and are never supplied — so a parameter naming somebody else would either be
    ignored or be a request the API must refuse, and offering it would suggest
    the second is possible. The same reasoning
    :class:`~schemas.notification.NotificationListQuery` records.
    """

    model_config = ConfigDict(extra="forbid")

    range: DashboardRange = Field(
        default=DashboardRange.LAST_30_DAYS,
        description=(
            "Window the analytics widgets measure. `custom` additionally requires "
            "`start_date` and `end_date`."
        ),
    )
    start_date: date | None = Field(
        default=None, description="First day of a custom range, inclusive."
    )
    end_date: date | None = Field(
        default=None, description="Last day of a custom range, inclusive."
    )
    widgets: list[WidgetKey] | None = Field(
        default=None,
        description=(
            "Load only these widgets. Intersected with the caller's role layout and "
            "permissions, so it can never widen a dashboard — what a client sends when "
            "it is re-reading part of a page rather than all of it."
        ),
    )
    list_size: int | None = Field(
        default=None,
        ge=1,
        le=settings.DASHBOARD_MAX_LIST_SIZE,
        description=(
            f"Rows each list widget returns (max {settings.DASHBOARD_MAX_LIST_SIZE}). "
            f"Defaults to {settings.DASHBOARD_LIST_SIZE}."
        ),
    )
    #: A **field on this model rather than a parameter beside it**, and that is
    #: not a style choice: FastAPI expands a Pydantic query model into individual
    #: parameters only when it is the *sole* query parameter of the endpoint, so a
    #: `language` declared next to it would silently turn this whole model into
    #: one required parameter named `query`. Keeping every query field here is
    #: what keeps `?range=today` a valid request.
    language: str | None = Field(
        default=None,
        max_length=10,
        description=(
            "ISO 639-1 code the notifications widget renders its titles in (`ar`, `fr`, "
            "or `en`). Ignored by every other widget, because no other widget returns "
            "prose."
        ),
    )

    @model_validator(mode="after")
    def _validate_custom_range(self) -> DashboardQuery:
        """Reject a custom range that is incomplete, inverted, or too long.

        Enforced here as well as in :func:`~core.dashboard.resolve_window` so the
        caller gets a **422 with a field name** rather than a 500 — the vocabulary
        module raises a plain ``ValueError`` because it has no HTTP opinion, and
        this is the layer that does.
        """
        if self.range is DashboardRange.CUSTOM:
            if self.start_date is None or self.end_date is None:
                raise ValueError(
                    "A custom range requires both start_date and end_date."
                )
            if self.end_date < self.start_date:
                raise ValueError("end_date must not precede start_date.")
            span = (self.end_date - self.start_date).days + 1
            if span > settings.DASHBOARD_MAX_RANGE_DAYS:
                raise ValueError(
                    f"A custom range may cover at most "
                    f"{settings.DASHBOARD_MAX_RANGE_DAYS} days."
                )
        return self


class WidgetQuery(BaseModel):
    """Validated query parameters for ``GET /dashboard/widgets/{widget_key}``.

    The same filter as a full load minus the widget subset, because the path
    already names one. Sharing the range parameters is what lets a refreshed tile
    be measured over the interval the page around it is showing — a refresh that
    silently reverted to the default window would make one card disagree with its
    neighbours.

    Like :class:`DashboardQuery`, it carries **every** query field the endpoint
    accepts, ``language`` included — see that model for why one declared beside it
    would stop FastAPI expanding either of them.
    """

    model_config = ConfigDict(extra="forbid")

    range: DashboardRange = Field(default=DashboardRange.LAST_30_DAYS)
    start_date: date | None = Field(default=None)
    end_date: date | None = Field(default=None)
    list_size: int | None = Field(
        default=None, ge=1, le=settings.DASHBOARD_MAX_LIST_SIZE
    )
    language: str | None = Field(
        default=None,
        max_length=10,
        description=(
            "ISO 639-1 code the notifications widget renders its titles in. Ignored by "
            "every other widget, because no other widget returns prose."
        ),
    )

    @model_validator(mode="after")
    def _validate_custom_range(self) -> WidgetQuery:
        """Apply the same custom-range rules a full dashboard load applies."""
        DashboardQuery(
            range=self.range, start_date=self.start_date, end_date=self.end_date
        )
        return self


# --------------------------------------------------------------------------- #
# Payload elements
# --------------------------------------------------------------------------- #


class MetricRead(BaseModel):
    """One named figure.

    ``value`` is ``None`` when the figure is **undefined** rather than zero — an
    average with no observations, a rate with no denominator. The distinction is
    the whole of the spec's "Analytics Data Integrity" requirement: returning
    ``0`` for "we have not measured this" is a fabricated statistic, and it is the
    one this feature would most easily have produced by accident.
    """

    key: str = Field(description="Stable identifier; the root of the client's label key.")
    value: float | int | None = Field(
        default=None, description="The measurement, or null when it is undefined."
    )
    unit: MetricUnit = Field(
        default=MetricUnit.COUNT, description="How the number should be formatted."
    )


class BucketRead(BaseModel):
    """One labelled slice of a breakdown."""

    key: str = Field(description="Stable identifier; the root of the client's label key.")
    count: int = Field(description="Rows in this slice. A measured zero is still returned.")


class DashboardUserRead(BaseModel):
    """The minimum needed to name a person on a card.

    Identifier and display name only — no email, no role, no status. A dashboard
    shows who a case is assigned to; the user directory is where an account is
    read, and it has its own permission.
    """

    id: uuid.UUID
    full_name: str


class DashboardCaseRead(BaseModel):
    """A case, as a widget shows it.

    Deliberately **not** :class:`~schemas.case.CaseRead`. A case summary here
    carries no description and no audit columns: a dashboard card shows a number,
    a title, a status, and a date, and returning the full record for five cases on
    every page load would put client-confidential prose into a response nobody
    asked to read.
    """

    id: uuid.UUID
    case_number: str
    title: str
    status: CaseStatus
    priority: CasePriority
    court_name: str | None = None
    next_hearing_date: date | None = None
    updated_at: datetime
    assigned_lawyer: DashboardUserRead | None = None
    assigned_court_representative: DashboardUserRead | None = None


class DashboardDocumentRead(BaseModel):
    """A document, as a widget shows it."""

    id: uuid.UUID
    case_id: uuid.UUID
    original_filename: str
    category: DocumentCategory
    file_extension: str
    file_size: int
    version: int
    created_at: datetime


class DashboardReportRead(BaseModel):
    """One of the caller's reports, as a widget shows it."""

    id: uuid.UUID
    case_id: uuid.UUID
    title: str
    report_type: ReportType
    status: ReportStatus
    sections_completed: int
    sections_total: int | None = None
    created_at: datetime


class DashboardConversationRead(BaseModel):
    """One of the caller's assistant threads, as a widget shows it.

    The **title only** — never ``last_message_preview``, even though the column
    exists and the conversation list uses it. A preview is a fragment of a legal
    question, and a dashboard is the one screen somebody may have open on a shared
    display in a meeting room.
    """

    id: uuid.UUID
    title: str
    case_id: uuid.UUID | None = None
    message_count: int
    last_message_at: datetime | None = None


class DashboardActivityRead(BaseModel):
    """One timeline entry, as the activity widget shows it.

    ``category`` is derived from the event type by
    :func:`~core.timeline.category_for` rather than stored, so the icon a client
    draws cannot disagree with the event it is drawn beside.
    """

    id: uuid.UUID
    case_id: uuid.UUID
    event_type: str
    category: TimelineEventCategory
    title: str
    actor_name: str | None = None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #
#
# Nine models discriminated on `kind`, matching `WidgetPayloadKind`. A client
# switches on that one field and has a renderer per shape rather than per widget
# — which is what makes a nineteenth, twentieth, and thirtieth widget free on the
# frontend as well as on the server.


class MetricsPayload(BaseModel):
    """Named figures."""

    kind: Literal[WidgetPayloadKind.METRICS] = WidgetPayloadKind.METRICS
    metrics: list[MetricRead]


class BreakdownPayload(BaseModel):
    """One total split into labelled buckets."""

    kind: Literal[WidgetPayloadKind.BREAKDOWN] = WidgetPayloadKind.BREAKDOWN
    total: int
    buckets: list[BucketRead]


class CasesPayload(BaseModel):
    """A short list of cases."""

    kind: Literal[WidgetPayloadKind.CASES] = WidgetPayloadKind.CASES
    cases: list[DashboardCaseRead]


class DocumentsPayload(BaseModel):
    """A short list of documents."""

    kind: Literal[WidgetPayloadKind.DOCUMENTS] = WidgetPayloadKind.DOCUMENTS
    documents: list[DashboardDocumentRead]


class ReportsPayload(BaseModel):
    """A short list of the caller's reports."""

    kind: Literal[WidgetPayloadKind.REPORTS] = WidgetPayloadKind.REPORTS
    reports: list[DashboardReportRead]


class ConversationsPayload(BaseModel):
    """A short list of the caller's conversations."""

    kind: Literal[WidgetPayloadKind.CONVERSATIONS] = WidgetPayloadKind.CONVERSATIONS
    conversations: list[DashboardConversationRead]


class ActivityPayload(BaseModel):
    """A short list of timeline entries."""

    kind: Literal[WidgetPayloadKind.ACTIVITY] = WidgetPayloadKind.ACTIVITY
    activity: list[DashboardActivityRead]


class NotificationsPayload(BaseModel):
    """A short list of the caller's notifications.

    Reuses :class:`~schemas.notification.NotificationRead` rather than defining a
    dashboard variant, and that is the one place this feature copies another
    module's response shape on purpose: a notification's title and message are
    *rendered* from its rule in the reader's language, and a second projection
    would be a second place for the platform's wording to drift.
    """

    kind: Literal[WidgetPayloadKind.NOTIFICATIONS] = WidgetPayloadKind.NOTIFICATIONS
    notifications: list[NotificationRead]


class ActionsPayload(BaseModel):
    """No data; the quick actions live on the dashboard envelope."""

    kind: Literal[WidgetPayloadKind.ACTIONS] = WidgetPayloadKind.ACTIONS


WidgetPayloadRead = Annotated[
    MetricsPayload
    | BreakdownPayload
    | CasesPayload
    | DocumentsPayload
    | ReportsPayload
    | ConversationsPayload
    | ActivityPayload
    | NotificationsPayload
    | ActionsPayload,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# Widgets and the dashboard
# --------------------------------------------------------------------------- #


class WidgetDescriptorRead(BaseModel):
    """What a client needs to draw a widget before its data arrives.

    Served both on its own (``GET /dashboard/widgets``) and inside every widget of
    a full load, so a client can render placeholders, lay out sections, and wire
    up live refresh from one response.

    ``refresh_events`` is the field that makes this more than metadata: it is the
    list of domain event types after which this widget is stale, straight from
    :data:`~core.dashboard.WIDGETS`. A client subscribed to the real-time channel
    refreshes exactly the widgets an event touched, and **has no table of its
    own** — so a widget added on the server starts updating live in a browser
    nobody redeployed.
    """

    key: WidgetKey
    group: WidgetGroup
    kind: WidgetPayloadKind
    refresh_events: list[str] = Field(
        description="Domain event types after which this widget should be re-read."
    )
    refresh_interval_seconds: int = Field(
        description="Suggested polling interval; 0 means event-driven or static."
    )
    platform_wide: bool = Field(
        description=(
            "Whether the widget reports on the whole platform rather than on the "
            "caller's own cases."
        )
    )


class WidgetRead(BaseModel):
    """One widget's state, data, and cost.

    Every widget in a dashboard response carries its own ``state`` and
    ``generated_at``, which is what makes independent refresh visible to a user:
    a tile refreshed a moment ago and one loaded with the page are distinguishable
    without the client tracking it.
    """

    widget: WidgetDescriptorRead
    state: WidgetStateValue
    generated_at: datetime
    duration_ms: float = Field(description="Server time spent producing this widget.")
    data: WidgetPayloadRead | None = Field(
        default=None, description="Absent when the widget is unavailable."
    )
    error_code: str | None = Field(
        default=None,
        description=(
            "Why the widget is unavailable — `query_failed` or `budget_exhausted`. "
            "A code rather than a message, so the client explains it in the reader's "
            "language."
        ),
    )

    @classmethod
    def from_result(
        cls, result: WidgetResult, *, language: str | None = None
    ) -> WidgetRead:
        """Project a service result onto the response shape."""
        return cls(
            widget=_descriptor(result),
            state=result.state,
            generated_at=result.generated_at,
            duration_ms=result.duration_ms,
            data=(
                _payload(result.payload, language=language)
                if result.payload is not None
                else None
            ),
            error_code=result.error_code,
        )


class DashboardRead(BaseModel):
    """One assembled dashboard.

    **The one response a client needs to render the page**, which is the spec's
    *"the frontend should receive a single dashboard response whenever
    possible"*: the widgets, their data, their metadata, the resolved window, and
    the quick actions, in one round trip.

    ``failed_widgets`` is on the envelope so a client shows one banner rather than
    counting error cards, and ``duration_ms`` is here so a slow dashboard is
    visible to whoever is looking at it rather than only to whoever reads the
    metrics endpoint.
    """

    generated_at: datetime
    range: DashboardRange
    window_start: datetime
    window_end: datetime
    window_days: int
    role: str = Field(description="The caller's role, which decided the layout.")
    widgets: list[WidgetRead]
    quick_actions: list[QuickActionKey] = Field(
        description=(
            "Shortcuts this caller may use. On the envelope rather than inside the "
            "quick-actions widget, because the page header renders them whether or not "
            "that widget is on the page."
        )
    )
    failed_widgets: int
    duration_ms: float

    @classmethod
    def from_dashboard(
        cls, dashboard: Dashboard, *, language: str | None = None
    ) -> DashboardRead:
        """Project an assembled dashboard onto the response shape."""
        return cls(
            generated_at=dashboard.generated_at,
            range=dashboard.window.range,
            window_start=dashboard.window.start,
            window_end=dashboard.window.end,
            window_days=dashboard.window.days,
            role=dashboard.role,
            widgets=[
                WidgetRead.from_result(result, language=language)
                for result in dashboard.widgets
            ],
            quick_actions=list(dashboard.quick_actions),
            failed_widgets=dashboard.failed_widgets,
            duration_ms=dashboard.duration_ms,
        )


class WidgetCatalogRead(BaseModel):
    """The widgets this caller may load, in their role's order.

    Metadata only, and it runs no queries — what a client reads once on mount to
    lay out placeholders and register its live-refresh table.
    """

    role: str
    widgets: list[WidgetDescriptorRead]
    quick_actions: list[QuickActionKey]


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class DashboardMetricsRead(BaseModel):
    """Platform-wide dashboard health.

    Every figure here is counted **in this process** and carries ``since``,
    without exception — unlike the notification, email, and WhatsApp metrics
    views, which split their figures between SQL aggregates and in-memory
    counters. The reason is that the dashboard persists nothing at all: there is
    no row that records a load, so there is no exact figure to prefer.
    """

    since: datetime
    enabled: bool

    loads: int
    refreshes: int
    widgets_loaded: int
    widgets_failed: int
    widget_success_rate: float

    average_load_ms: float | None
    average_widget_ms: float | None

    active_users: int = Field(
        description=(
            "Distinct people who have opened a dashboard in this process, counted from "
            "salted digests that cannot be reversed — see `services/dashboard_metrics.py`."
        )
    )
    active_users_capped: bool

    average_ms_by_widget: dict[str, float]
    failures_by_widget: dict[str, int]
    failures_by_reason: dict[str, int]


# --------------------------------------------------------------------------- #
# Projection helpers
# --------------------------------------------------------------------------- #


def _descriptor(result: WidgetResult) -> WidgetDescriptorRead:
    """Build a widget's metadata from its definition.

    The events are sorted so a response is byte-stable between calls: a set has no
    order, and an unstable payload makes diffs and cached responses noisy for no
    reason — the same treatment
    :func:`~core.permissions.sort_permissions` gives a permission set.
    """
    definition = result.definition
    return WidgetDescriptorRead(
        key=definition.key,
        group=definition.group,
        kind=definition.kind,
        refresh_events=sorted(event.value for event in definition.events),
        refresh_interval_seconds=definition.refresh_seconds,
        platform_wide=definition.platform_wide,
    )


def _payload(payload: WidgetPayload, *, language: str | None) -> WidgetPayloadRead:
    """Project one widget payload onto its discriminated response model.

    A mapping from kind to builder rather than a chain of ``if``s, so a payload
    kind added to :class:`~core.dashboard.WidgetPayloadKind` without a projection
    raises here — loudly, in a test — instead of silently serializing as the wrong
    shape.
    """
    builders = {
        WidgetPayloadKind.METRICS: lambda: MetricsPayload(
            metrics=[
                MetricRead(key=metric.key, value=metric.value, unit=metric.unit)
                for metric in payload.metrics
            ]
        ),
        WidgetPayloadKind.BREAKDOWN: lambda: BreakdownPayload(
            total=payload.total,
            buckets=[
                BucketRead(key=bucket.key, count=bucket.count)
                for bucket in payload.buckets
            ],
        ),
        WidgetPayloadKind.CASES: lambda: CasesPayload(
            cases=[_case(legal_case) for legal_case in payload.cases]
        ),
        WidgetPayloadKind.DOCUMENTS: lambda: DocumentsPayload(
            documents=[
                DashboardDocumentRead(
                    id=document.id,
                    case_id=document.case_id,
                    original_filename=document.original_filename,
                    category=document.category,
                    file_extension=document.file_extension,
                    file_size=document.file_size,
                    version=document.version,
                    created_at=document.created_at,
                )
                for document in payload.documents
            ]
        ),
        WidgetPayloadKind.REPORTS: lambda: ReportsPayload(
            reports=[
                DashboardReportRead(
                    id=report.id,
                    case_id=report.case_id,
                    title=report.title,
                    report_type=report.report_type,
                    status=report.status,
                    sections_completed=report.sections_completed,
                    sections_total=report.sections_total,
                    created_at=report.created_at,
                )
                for report in payload.reports
            ]
        ),
        WidgetPayloadKind.CONVERSATIONS: lambda: ConversationsPayload(
            conversations=[
                DashboardConversationRead(
                    id=conversation.id,
                    title=conversation.title,
                    case_id=conversation.case_id,
                    message_count=conversation.message_count,
                    last_message_at=conversation.last_message_at,
                )
                for conversation in payload.conversations
            ]
        ),
        WidgetPayloadKind.ACTIVITY: lambda: ActivityPayload(
            activity=[_activity(event) for event in payload.activity]
        ),
        WidgetPayloadKind.NOTIFICATIONS: lambda: NotificationsPayload(
            notifications=[
                NotificationRead.from_row(notification, language=language)
                for notification in payload.notifications
            ]
        ),
        WidgetPayloadKind.ACTIONS: ActionsPayload,
    }

    try:
        build = builders[payload.kind]
    except KeyError as exc:  # pragma: no cover - guarded by an exhaustiveness test
        raise ValueError(f"No projection for widget payload kind {payload.kind!r}.") from exc
    return build()


def _case(legal_case: Case) -> DashboardCaseRead:
    """Project one case row onto its summary."""
    return DashboardCaseRead(
        id=legal_case.id,
        case_number=legal_case.case_number,
        title=legal_case.title,
        status=legal_case.status,
        priority=legal_case.priority,
        court_name=legal_case.court_name,
        next_hearing_date=legal_case.next_hearing_date,
        updated_at=legal_case.updated_at,
        assigned_lawyer=_person(legal_case.assigned_lawyer),
        assigned_court_representative=_person(legal_case.assigned_court_representative),
    )


def _person(user: User | None) -> DashboardUserRead | None:
    """Project an assignee onto the two fields a card shows."""
    if user is None:
        return None
    return DashboardUserRead(id=user.id, full_name=user.full_name)


def _activity(event: TimelineEvent) -> DashboardActivityRead:
    """Project one timeline entry onto its summary."""
    return DashboardActivityRead(
        id=event.id,
        case_id=event.case_id,
        event_type=event.event_type,
        category=category_for(event.event_type),
        title=event.title,
        actor_name=event.actor_name,
        created_at=event.created_at,
    )


__all__ = [
    "ActionsPayload",
    "ActivityPayload",
    "BreakdownPayload",
    "BucketRead",
    "CasesPayload",
    "ConversationsPayload",
    "DashboardActivityRead",
    "DashboardCaseRead",
    "DashboardConversationRead",
    "DashboardDocumentRead",
    "DashboardMetricsRead",
    "DashboardQuery",
    "DashboardRead",
    "DashboardReportRead",
    "DashboardUserRead",
    "DocumentsPayload",
    "MetricRead",
    "MetricsPayload",
    "NotificationsPayload",
    "ReportsPayload",
    "WidgetCatalogRead",
    "WidgetDescriptorRead",
    "WidgetQuery",
    "WidgetRead",
]
