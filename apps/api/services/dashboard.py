"""The dashboard: assembling widgets, independently and safely.

``19-dashboard-analytics.md`` describes a page whose properties are mostly
*negative* — a widget must not depend on another widget, must not be able to see
past its owner's authorization, must not take the page down when it fails, and
must not require the whole dashboard to be reloaded to refresh itself. This
service is where those become true, and every one of them is a consequence of the
same structural decision: **a widget is a function of a context, registered by
key.**

.. code-block:: text

    WidgetKey ──▶ loader(WidgetContext) ──▶ WidgetPayload
                        │
                        └── scope + window + limits, resolved once for the request

From that shape the spec's requirements follow rather than being implemented:

* **Independence** — a loader receives a context and returns a payload. It cannot
  reach another widget's result, because it is never given one, and it cannot
  reorder itself, because it does not know it is on a page.
* **Authorization** — the context carries the scopes
  :class:`~services.dashboard_access.DashboardAccessPolicy` resolved, and a
  loader has no way to obtain a wider one. The permission check happens
  *before* the loader is called, so an unauthorized widget is not computed and
  filtered; it is never computed.
* **Independent failure** — each loader is called inside its own ``try``. A
  failure marks that widget unavailable, is counted, is logged, and the page
  continues. The spec's *"one failing widget must not prevent the dashboard from
  loading"* is therefore not a behaviour to remember: it is the only behaviour
  the loop can have.
* **Independent refresh** — the same loader serves ``GET /dashboard`` and
  ``GET /dashboard/widgets/{key}``, so refreshing one widget runs exactly the
  queries that widget needs and nothing else.
* **Timeouts** — widgets are loaded in order against a wall-clock budget. Once it
  is spent, the rest come back unavailable rather than being attempted, so a slow
  dashboard degrades into a partial one instead of a hung request.

**Adding a widget is one entry in** :data:`~core.dashboard.WIDGETS` **and one
loader below.** Nothing in the router, the schemas, the access policy, or the
frontend changes — the client is told the widget's shape, its refresh interval,
and the events that make it stale, all from the catalog.

**Nothing here is stored, and nothing here is invented.** There is no dashboard
table: the service reads through :class:`~repositories.dashboard.DashboardRepository`
and returns. Where a figure has no observations it is ``None``, and where a set
is empty it is empty — the spec's "Analytics Data Integrity" section forbids
placeholder statistics, and the way to honour that is to have nowhere for one to
come from.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

import structlog

from core.config import settings
from core.dashboard import (
    Bucket,
    DashboardRange,
    Metric,
    MetricUnit,
    QuickActionKey,
    TimeWindow,
    WidgetDefinition,
    WidgetKey,
    WidgetPayloadKind,
    layout_for,
    resolve_window,
    widget_definition,
)
from core.exceptions import DashboardDisabledError, DashboardWidgetNotFoundError
from models.case import Case
from models.conversation import Conversation
from models.document import Document
from models.notification import Notification
from models.report import Report
from models.timeline import TimelineEvent
from models.user import User
from repositories.dashboard import DashboardRepository
from repositories.notification import NotificationRepository
from schemas.case import SortOrder
from schemas.notification import NotificationListQuery, NotificationSortField
from services.dashboard_access import DashboardAccessPolicy
from services.dashboard_metrics import (
    DashboardMetricsRecorder,
    DashboardMetricsSnapshot,
    NullDashboardMetrics,
    WidgetFailureReason,
)

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# What a widget is given, and what it returns
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WidgetContext:
    """Everything a widget loader is allowed to know.

    Frozen, and deliberately small. Note what is **not** here: there is no
    :class:`~models.user.User`, no permission set, and no request. A loader
    therefore cannot re-decide an authorization question that was already
    answered — the two scopes below are the *answers*, and the only identifiers it
    has are ones the policy handed it.

    ``case_scope`` is ``None`` for a caller who reads every case, which is the
    same convention :meth:`~services.case_access.CaseAccessPolicy.visibility_scope`
    uses; ``owner_id`` is always the caller and never anybody else.
    """

    case_scope: uuid.UUID | None
    owner_id: uuid.UUID
    window: TimeWindow
    list_size: int


@dataclass(frozen=True, slots=True)
class WidgetPayload:
    """One widget's data, in whichever of the nine shapes it declared.

    A single class with optional fields rather than nine subclasses, because the
    consumer is a Pydantic schema that discriminates on
    :class:`~core.dashboard.WidgetPayloadKind` anyway — and a hierarchy would put
    an ``isinstance`` ladder in the one place this feature is trying not to have
    one. Exactly one field is populated for any given kind.
    """

    kind: WidgetPayloadKind
    metrics: tuple[Metric, ...] = ()
    buckets: tuple[Bucket, ...] = ()
    total: int = 0
    cases: tuple[Case, ...] = ()
    documents: tuple[Document, ...] = ()
    reports: tuple[Report, ...] = ()
    conversations: tuple[Conversation, ...] = ()
    activity: tuple[TimelineEvent, ...] = ()
    notifications: tuple[Notification, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether this widget has nothing to show.

        Used to mark a widget ``empty`` rather than ``ready``, so a client can
        render the spec's "empty states" without inspecting a payload it would
        otherwise have to understand the shape of. A metrics widget is **never**
        empty: zero cases is a fact about the platform, and hiding it behind an
        empty state would be the one thing "Analytics Data Integrity" rules out.
        """
        if self.kind in (WidgetPayloadKind.METRICS, WidgetPayloadKind.ACTIONS):
            return False
        return not (
            self.buckets
            or self.cases
            or self.documents
            or self.reports
            or self.conversations
            or self.activity
            or self.notifications
        )


class WidgetStateValue(StrEnum):
    """How a widget turned out.

    Three states, and the distinction between the last two is the useful one:
    ``empty`` means *the query ran and there is nothing*, while ``unavailable``
    means *the query did not run*. A client renders an invitation for the first
    and a retry for the second, and conflating them would show somebody "no cases
    yet" when the database was down.
    """

    #: Loaded, with data.
    READY = "ready"
    #: Loaded, and there is nothing to show. A measured emptiness.
    EMPTY = "empty"
    #: Not loaded. See ``error_code`` for why.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class WidgetResult:
    """One widget as the API reports it: its state, its data, and what it cost."""

    definition: WidgetDefinition
    state: WidgetStateValue
    generated_at: datetime
    duration_ms: float
    payload: WidgetPayload | None = None
    #: Why it is unavailable. ``None`` on every successful state, and never a
    #: message — a closed vocabulary the client can translate, for the reason
    #: :class:`~services.dashboard_metrics.WidgetFailureReason` is one.
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class Dashboard:
    """One assembled dashboard."""

    generated_at: datetime
    window: TimeWindow
    role: str
    widgets: tuple[WidgetResult, ...]
    quick_actions: tuple[QuickActionKey, ...]
    duration_ms: float

    @property
    def failed_widgets(self) -> int:
        """How many widgets could not be produced.

        Reported on the envelope so a client can show one "some widgets could not
        be loaded" banner instead of nineteen error cards — and so an operator
        watching a screen recording can tell a degraded dashboard from a healthy
        one at a glance.
        """
        return sum(
            1 for widget in self.widgets if widget.state is WidgetStateValue.UNAVAILABLE
        )


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #

#: A widget loader: context in, payload out. Registered by key in
#: :attr:`DashboardService._loaders`.
WidgetLoader = Callable[[WidgetContext], WidgetPayload]


@dataclass
class _CacheEntry:
    """One cached platform-wide payload and when it expires."""

    payload: WidgetPayload
    expires_at: float


class DashboardService:
    """Assembles dashboards, one independent widget at a time."""

    def __init__(
        self,
        dashboards: DashboardRepository,
        notifications: NotificationRepository,
        *,
        access: DashboardAccessPolicy | None = None,
        metrics: DashboardMetricsRecorder | None = None,
    ) -> None:
        self._dashboards = dashboards
        self._notifications = notifications
        self._access = access or DashboardAccessPolicy()
        self._metrics = metrics or NullDashboardMetrics()
        self._loaders: Mapping[WidgetKey, WidgetLoader] = self._build_loaders()

    # ------------------------------------------------------------ the page #

    def load(
        self,
        *,
        actor: User,
        dashboard_range: DashboardRange = DashboardRange.LAST_30_DAYS,
        start: date | None = None,
        end: date | None = None,
        only: Sequence[WidgetKey] | None = None,
        list_size: int | None = None,
    ) -> Dashboard:
        """Assemble every widget this caller may see, in their role's order.

        **One request, one response**, which is the spec's *"avoid one API request
        per widget"*: the client asks once and receives the whole page, including
        the widgets that failed and the reason each one did.

        ``only`` narrows the page to a subset — what a client sends when it is
        re-reading a group rather than the page — and it can never *widen* one: it
        is intersected with the role layout and the permission filter, in that
        order, so an unknown or unauthorized key contributes nothing rather than
        producing an error.

        Raises:
            DashboardDisabledError: the feature is switched off for this
                deployment.
            InvalidDashboardWindowError: a custom range that is missing a bound,
                inverted, or too long. Raised by
                :func:`~core.dashboard.resolve_window` and normally caught by the
                schema layer long before this.
        """
        self._require_enabled()
        started = time.perf_counter()

        window = resolve_window(
            dashboard_range,
            start=start,
            end=end,
            max_days=settings.DASHBOARD_MAX_RANGE_DAYS,
        )
        permissions = self._access.permissions_for(actor)
        keys = self._access.visible_widgets(layout_for(actor.role), permissions)
        if only is not None:
            requested = set(only)
            keys = tuple(key for key in keys if key in requested)

        context = WidgetContext(
            case_scope=self._access.case_scope(actor),
            owner_id=self._access.owner_scope(actor),
            window=window,
            list_size=self._resolve_list_size(list_size),
        )

        budget = settings.DASHBOARD_BUDGET_SECONDS
        results: list[WidgetResult] = []
        for key in keys:
            if time.perf_counter() - started >= budget:
                results.append(
                    self._unavailable(key, reason=WidgetFailureReason.BUDGET_EXHAUSTED)
                )
                continue
            results.append(self._load_widget(key, context))

        elapsed_ms = (time.perf_counter() - started) * 1000
        self._metrics.record_load(duration_ms=elapsed_ms, user_id=actor.id)

        dashboard = Dashboard(
            generated_at=datetime.now(UTC),
            window=window,
            role=actor.role.value,
            widgets=tuple(results),
            quick_actions=self._access.quick_actions(permissions),
            duration_ms=round(elapsed_ms, 2),
        )

        # Counts and identifiers only. A dashboard load names a person and the
        # widgets they were shown; it must never name a case, a document, or a
        # figure — "never log confidential case or document contents" applies to
        # the numbers derived from them as much as to the text.
        logger.info(
            "dashboard_loaded",
            user_id=str(actor.id),
            role=actor.role.value,
            range=window.range.value,
            window_days=window.days,
            widgets=len(dashboard.widgets),
            failed_widgets=dashboard.failed_widgets,
            duration_ms=dashboard.duration_ms,
        )
        return dashboard

    def refresh(
        self,
        key: WidgetKey,
        *,
        actor: User,
        dashboard_range: DashboardRange = DashboardRange.LAST_30_DAYS,
        start: date | None = None,
        end: date | None = None,
        list_size: int | None = None,
    ) -> WidgetResult:
        """Load exactly one widget.

        The spec's *"refreshing one widget should not reload the entire
        dashboard"*, and it is the **same loader** the page uses — so a refreshed
        tile cannot drift from the one that was rendered a moment ago, which is
        the failure mode a second code path would eventually produce.

        Raises:
            DashboardDisabledError: the feature is switched off.
            DashboardWidgetNotFoundError: the key names no widget, or names one
                this caller may not see. The two are deliberately the same
                answer — see the exception for why.
        """
        self._require_enabled()

        try:
            self._access.require_view(key, actor)
        except PermissionError as exc:
            raise DashboardWidgetNotFoundError from exc

        window = resolve_window(
            dashboard_range,
            start=start,
            end=end,
            max_days=settings.DASHBOARD_MAX_RANGE_DAYS,
        )
        context = WidgetContext(
            case_scope=self._access.case_scope(actor),
            owner_id=self._access.owner_scope(actor),
            window=window,
            list_size=self._resolve_list_size(list_size),
        )

        self._metrics.record_refresh(user_id=actor.id)
        result = self._load_widget(key, context)

        logger.info(
            "dashboard_widget_refreshed",
            user_id=str(actor.id),
            role=actor.role.value,
            widget=key.value,
            state=result.state.value,
            range=window.range.value,
            duration_ms=result.duration_ms,
        )
        return result

    def catalog(self, *, actor: User) -> tuple[WidgetDefinition, ...]:
        """The widgets this caller may refresh, in their role's order.

        Metadata only — no queries are run. What a client reads once to know which
        tiles to draw placeholders for, and which events make each of them stale.
        """
        permissions = self._access.permissions_for(actor)
        return tuple(
            widget_definition(key)
            for key in self._access.visible_widgets(layout_for(actor.role), permissions)
        )

    def quick_actions(self, *, actor: User) -> tuple[QuickActionKey, ...]:
        """The shortcuts this caller may use."""
        return self._access.quick_actions(self._access.permissions_for(actor))

    def metrics(self) -> DashboardMetricsSnapshot:
        """The process's dashboard counters, as one consistent snapshot."""
        return self._metrics.snapshot()

    @property
    def enabled(self) -> bool:
        """Whether the dashboard is switched on for this deployment."""
        return settings.DASHBOARD_ENABLED

    # ------------------------------------------------------- the widget loop #

    def _load_widget(self, key: WidgetKey, context: WidgetContext) -> WidgetResult:
        """Run one widget's loader, and never raise.

        The heart of "widgets fail independently": every path out of this method
        is a :class:`WidgetResult`. A loader that raises produces an unavailable
        widget with a code, a log line, and a counter — and the caller's loop does
        not know anything happened.
        """
        definition = widget_definition(key)
        started = time.perf_counter()

        cached = self._cached(definition, context)
        if cached is not None:
            return self._ready(definition, cached, duration_ms=0.0)

        try:
            payload = self._loaders[key](context)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            # `exception` rather than `error`: the traceback is the only way to
            # find out which query failed, and it stays server-side. The response
            # carries a code and nothing else — `code-standards.md` forbids
            # exposing internals, and a widget is not an exception to that.
            logger.exception(
                "dashboard_widget_failed",
                widget=key.value,
                reason=WidgetFailureReason.QUERY_FAILED.value,
                duration_ms=round(elapsed_ms, 2),
            )
            self._metrics.record_widget_failure(
                key.value, WidgetFailureReason.QUERY_FAILED
            )
            return self._unavailable(
                key, reason=WidgetFailureReason.QUERY_FAILED, duration_ms=elapsed_ms
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        self._metrics.record_widget(key.value, duration_ms=elapsed_ms)
        self._remember(definition, context, payload)

        logger.debug(
            "dashboard_widget_loaded",
            widget=key.value,
            duration_ms=round(elapsed_ms, 2),
        )
        return self._ready(definition, payload, duration_ms=elapsed_ms)

    @staticmethod
    def _ready(
        definition: WidgetDefinition, payload: WidgetPayload, *, duration_ms: float
    ) -> WidgetResult:
        """Wrap a loaded payload, marking it empty when it has nothing in it."""
        return WidgetResult(
            definition=definition,
            state=WidgetStateValue.EMPTY if payload.is_empty else WidgetStateValue.READY,
            generated_at=datetime.now(UTC),
            duration_ms=round(duration_ms, 2),
            payload=payload,
        )

    def _unavailable(
        self,
        key: WidgetKey,
        *,
        reason: WidgetFailureReason,
        duration_ms: float = 0.0,
    ) -> WidgetResult:
        """Build the result for a widget that was not produced."""
        if reason is WidgetFailureReason.BUDGET_EXHAUSTED:
            # Not logged per widget: a dashboard that runs out of budget sheds
            # every remaining tile, and one line per shed widget would bury the
            # one line that matters. The counter carries the shape of it.
            self._metrics.record_widget_failure(key.value, reason)

        return WidgetResult(
            definition=widget_definition(key),
            state=WidgetStateValue.UNAVAILABLE,
            generated_at=datetime.now(UTC),
            duration_ms=round(duration_ms, 2),
            error_code=reason.value,
        )

    @staticmethod
    def _require_enabled() -> None:
        """Refuse the request when the dashboard is switched off."""
        if not settings.DASHBOARD_ENABLED:
            raise DashboardDisabledError

    @staticmethod
    def _resolve_list_size(requested: int | None) -> int:
        """Clamp a requested row count to the configured bounds."""
        if requested is None:
            return settings.DASHBOARD_LIST_SIZE
        return max(1, min(requested, settings.DASHBOARD_MAX_LIST_SIZE))

    # --------------------------------------------------------------- caching #
    #
    # The spec asks the dashboard to "cache expensive computations when
    # appropriate", and the whole of the design here is in what "appropriate"
    # excludes. Only widgets declared `platform_wide` are eligible, because those
    # are the only ones whose answer does not depend on who asked — which makes a
    # shared entry incapable of showing one caller another's data, rather than
    # merely unlikely to. Nothing scoped to a case, a user, or an owner is ever
    # cached, and there is no setting that would make it so.

    def _cache_key(
        self, definition: WidgetDefinition, context: WidgetContext
    ) -> tuple[str, str, str] | None:
        """The cache key for a platform-wide widget, or ``None`` if not cacheable.

        A widget is cacheable only when it is platform-wide **and** the caller
        reads every case — the second condition is redundant today (every
        platform-wide widget requires a capability that implies it) and is checked
        anyway, because "redundant today" is how an authorization hole arrives.
        """
        if not definition.platform_wide or settings.DASHBOARD_CACHE_SECONDS <= 0:
            return None
        if context.case_scope is not None:
            return None
        return (
            definition.key.value,
            context.window.start.isoformat(),
            context.window.end.isoformat(),
        )

    def _cached(
        self, definition: WidgetDefinition, context: WidgetContext
    ) -> WidgetPayload | None:
        """Return a fresh cached payload, or ``None``."""
        key = self._cache_key(definition, context)
        if key is None:
            return None

        entry = _PLATFORM_CACHE.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            _PLATFORM_CACHE.pop(key, None)
            return None
        return entry.payload

    def _remember(
        self, definition: WidgetDefinition, context: WidgetContext, payload: WidgetPayload
    ) -> None:
        """Store a platform-wide payload for the configured TTL."""
        key = self._cache_key(definition, context)
        if key is None:
            return

        if len(_PLATFORM_CACHE) >= settings.DASHBOARD_CACHE_SIZE:
            # Oldest-first eviction. The cache holds a handful of entries by
            # construction, so a plain scan is cheaper than maintaining an
            # ordering structure to avoid one.
            oldest = min(_PLATFORM_CACHE, key=lambda k: _PLATFORM_CACHE[k].expires_at)
            _PLATFORM_CACHE.pop(oldest, None)

        _PLATFORM_CACHE[key] = _CacheEntry(
            payload=payload,
            expires_at=time.monotonic() + settings.DASHBOARD_CACHE_SECONDS,
        )

    # --------------------------------------------------------------- loaders #

    def _build_loaders(self) -> Mapping[WidgetKey, WidgetLoader]:
        """Bind each widget key to the method that produces its payload.

        A registry rather than a match statement, so that a widget added to
        :data:`~core.dashboard.WIDGETS` without a loader fails **at construction**
        (the exhaustiveness check below) rather than at the moment somebody with
        the right role opens the page.
        """
        loaders: dict[WidgetKey, WidgetLoader] = {
            WidgetKey.QUICK_ACTIONS: self._load_quick_actions,
            WidgetKey.NOTIFICATIONS: self._load_notifications,
            WidgetKey.RECENT_ACTIVITY: self._load_recent_activity,
            WidgetKey.MY_CASES: self._load_my_cases,
            WidgetKey.RECENT_CASES: self._load_recent_cases,
            WidgetKey.CASE_STATUS_OVERVIEW: self._load_case_status,
            WidgetKey.CASE_ANALYTICS: self._load_case_analytics,
            WidgetKey.UPCOMING_HEARINGS: self._load_upcoming_hearings,
            WidgetKey.HEARING_CALENDAR: self._load_hearing_calendar,
            WidgetKey.RECENT_DOCUMENTS: self._load_recent_documents,
            WidgetKey.OCR_STATUS: self._load_ocr_status,
            WidgetKey.DOCUMENT_ANALYTICS: self._load_document_analytics,
            WidgetKey.AI_REPORTS: self._load_ai_reports,
            WidgetKey.RECENT_CONVERSATIONS: self._load_recent_conversations,
            WidgetKey.AI_ANALYTICS: self._load_ai_analytics,
            WidgetKey.TIMELINE_ACTIVITY: self._load_timeline_activity,
            WidgetKey.STORAGE_USAGE: self._load_storage_usage,
            WidgetKey.ACTIVE_USERS: self._load_active_users,
            WidgetKey.PROCESSING_QUEUES: self._load_processing_queues,
        }

        missing = sorted(key.value for key in WidgetKey if key not in loaders)
        if missing:  # pragma: no cover - guarded by an exhaustiveness test
            raise RuntimeError(f"Dashboard widgets without a loader: {missing}")
        return loaders

    # --- General ------------------------------------------------------------ #

    @staticmethod
    def _load_quick_actions(context: WidgetContext) -> WidgetPayload:
        """The quick-actions tile carries no data of its own.

        The actions themselves live on the dashboard envelope, because the header
        renders them whether or not this widget is on the page — and resolving
        them twice would be two places for the authorization filter to be applied
        slightly differently.
        """
        return WidgetPayload(kind=WidgetPayloadKind.ACTIONS)

    def _load_notifications(self, context: WidgetContext) -> WidgetPayload:
        """The caller's newest notifications, unread first.

        Read through :class:`~repositories.notification.NotificationRepository`
        rather than through the dashboard's own repository, and that is the one
        place this feature reuses another module's data access. The reason is that
        every read there is keyed by recipient with no unscoped variant, so a
        dashboard cannot become the first way to see somebody else's feed.

        The rows are returned unrendered: a notification stores no prose, and the
        title and message are produced in the reader's language by the schema
        layer — exactly as ``GET /notifications`` does.
        """
        page, _ = self._notifications.list_notifications(
            _unread_first_query(context.list_size),
            recipient_id=context.owner_id,
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.NOTIFICATIONS, notifications=tuple(page)
        )

    def _load_recent_activity(self, context: WidgetContext) -> WidgetPayload:
        """The newest timeline entries across the caller's cases."""
        events = self._dashboards.recent_activity(
            visible_to=context.case_scope, limit=context.list_size
        )
        return WidgetPayload(kind=WidgetPayloadKind.ACTIVITY, activity=tuple(events))

    # --- Cases -------------------------------------------------------------- #

    def _load_my_cases(self, context: WidgetContext) -> WidgetPayload:
        """The cases the caller is personally assigned to."""
        cases = self._dashboards.assigned_cases(
            user_id=context.owner_id, limit=context.list_size
        )
        return WidgetPayload(kind=WidgetPayloadKind.CASES, cases=tuple(cases))

    def _load_recent_cases(self, context: WidgetContext) -> WidgetPayload:
        """The most recently updated cases in the caller's scope."""
        cases = self._dashboards.recent_cases(
            visible_to=context.case_scope, limit=context.list_size
        )
        return WidgetPayload(kind=WidgetPayloadKind.CASES, cases=tuple(cases))

    def _load_case_status(self, context: WidgetContext) -> WidgetPayload:
        """How the caller's caseload is distributed across lifecycle states."""
        buckets = self._dashboards.case_status_breakdown(visible_to=context.case_scope)
        return WidgetPayload(
            kind=WidgetPayloadKind.BREAKDOWN,
            buckets=buckets,
            total=sum(bucket.count for bucket in buckets),
        )

    def _load_case_analytics(self, context: WidgetContext) -> WidgetPayload:
        """Active, closed, and newly created cases — the spec's case analytics."""
        analytics = self._dashboards.case_analytics(
            visible_to=context.case_scope,
            start=context.window.start,
            end=context.window.end,
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.METRICS,
            metrics=(
                Metric(key="total_cases", value=analytics.total),
                Metric(key="active_cases", value=analytics.active),
                Metric(key="closed_cases", value=analytics.closed),
                Metric(key="archived_cases", value=analytics.archived),
                Metric(key="created_in_window", value=analytics.created_in_window),
                Metric(key="closed_in_window", value=analytics.closed_in_window),
            ),
        )

    # --- Court -------------------------------------------------------------- #

    def _load_upcoming_hearings(self, context: WidgetContext) -> WidgetPayload:
        """Cases with a hearing between today and the configured horizon."""
        cases = self._dashboards.upcoming_hearings(
            visible_to=context.case_scope,
            today=datetime.now(UTC).date(),
            horizon_days=settings.DASHBOARD_HEARING_HORIZON_DAYS,
            limit=context.list_size,
        )
        return WidgetPayload(kind=WidgetPayloadKind.CASES, cases=tuple(cases))

    def _load_hearing_calendar(self, context: WidgetContext) -> WidgetPayload:
        """The shape of the hearing diary — the spec's "Hearing Calendar Summary".

        Deliberately counts rather than a calendar grid: a month of dates is a
        page, and what a dashboard is for is the one number that makes somebody
        open it.
        """
        summary = self._dashboards.hearing_summary(
            visible_to=context.case_scope, today=datetime.now(UTC).date()
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.METRICS,
            metrics=(
                Metric(key="hearings_today", value=summary.today),
                Metric(key="hearings_next_7_days", value=summary.next_7_days),
                Metric(key="hearings_next_30_days", value=summary.next_30_days),
                Metric(key="hearings_overdue", value=summary.overdue),
            ),
        )

    # --- Documents ---------------------------------------------------------- #

    def _load_recent_documents(self, context: WidgetContext) -> WidgetPayload:
        """The newest documents in the caller's cases."""
        documents = self._dashboards.recent_documents(
            visible_to=context.case_scope, limit=context.list_size
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.DOCUMENTS, documents=tuple(documents)
        )

    def _load_ocr_status(self, context: WidgetContext) -> WidgetPayload:
        """Where the caller's documents stand in the extraction pipeline."""
        buckets = self._dashboards.ocr_status_breakdown(visible_to=context.case_scope)
        return WidgetPayload(
            kind=WidgetPayloadKind.BREAKDOWN,
            buckets=buckets,
            total=sum(bucket.count for bucket in buckets),
        )

    def _load_document_analytics(self, context: WidgetContext) -> WidgetPayload:
        """Uploads, extraction, and indexing — the spec's document analytics."""
        analytics = self._dashboards.document_analytics(
            visible_to=context.case_scope,
            start=context.window.start,
            end=context.window.end,
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.METRICS,
            metrics=(
                Metric(key="total_documents", value=analytics.total),
                Metric(key="uploaded_in_window", value=analytics.uploaded_in_window),
                Metric(key="ocr_completed", value=analytics.ocr_completed),
                Metric(key="ocr_failed", value=analytics.ocr_failed),
                Metric(key="indexing_completed", value=analytics.indexed),
                Metric(key="indexing_failed", value=analytics.indexing_failed),
                Metric(
                    key="storage_bytes",
                    value=analytics.total_bytes,
                    unit=MetricUnit.BYTES,
                ),
            ),
        )

    # --- AI ----------------------------------------------------------------- #

    def _load_ai_reports(self, context: WidgetContext) -> WidgetPayload:
        """The caller's newest AI reports."""
        reports = self._dashboards.recent_reports(
            requested_by=context.owner_id, limit=context.list_size
        )
        return WidgetPayload(kind=WidgetPayloadKind.REPORTS, reports=tuple(reports))

    def _load_recent_conversations(self, context: WidgetContext) -> WidgetPayload:
        """The caller's newest assistant threads."""
        conversations = self._dashboards.recent_conversations(
            owner_id=context.owner_id, limit=context.list_size
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.CONVERSATIONS, conversations=tuple(conversations)
        )

    def _load_ai_analytics(self, context: WidgetContext) -> WidgetPayload:
        """Conversations and reports — the spec's AI analytics.

        The caller's own, on both halves. There is no platform-wide variant here
        even for an administrator: a report and a conversation belong to the
        person who produced them, and an aggregate over everybody's would be a
        count of other people's private research.
        """
        reports = self._dashboards.report_analytics(
            requested_by=context.owner_id,
            start=context.window.start,
            end=context.window.end,
        )
        conversations = self._dashboards.conversation_analytics(
            owner_id=context.owner_id,
            start=context.window.start,
            end=context.window.end,
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.METRICS,
            metrics=(
                Metric(key="conversations", value=conversations.total),
                Metric(key="active_conversations", value=conversations.active),
                Metric(
                    key="conversations_in_window",
                    value=conversations.started_in_window,
                ),
                Metric(key="assistant_messages", value=conversations.messages),
                Metric(key="reports_total", value=reports.total),
                Metric(key="reports_generated", value=reports.completed),
                Metric(
                    key="reports_in_window", value=reports.generated_in_window
                ),
                Metric(key="reports_failed", value=reports.failed),
                Metric(key="reports_in_progress", value=reports.in_progress),
            ),
        )

    # --- Timeline ----------------------------------------------------------- #

    def _load_timeline_activity(self, context: WidgetContext) -> WidgetPayload:
        """Where the window's activity went, by timeline category."""
        total, buckets = self._dashboards.activity_breakdown(
            visible_to=context.case_scope,
            start=context.window.start,
            end=context.window.end,
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.BREAKDOWN, buckets=buckets, total=total
        )

    # --- System ------------------------------------------------------------- #

    def _load_storage_usage(self, context: WidgetContext) -> WidgetPayload:
        """What the platform is storing. Administrative."""
        usage = self._dashboards.storage_usage(visible_to=context.case_scope)
        return WidgetPayload(
            kind=WidgetPayloadKind.METRICS,
            metrics=(
                Metric(key="documents", value=usage.document_count),
                Metric(
                    key="storage_bytes", value=usage.total_bytes, unit=MetricUnit.BYTES
                ),
                Metric(
                    key="version_bytes",
                    value=usage.version_bytes,
                    unit=MetricUnit.BYTES,
                ),
                Metric(
                    key="average_document_bytes",
                    value=usage.average_bytes,
                    unit=MetricUnit.BYTES,
                ),
            ),
        )

    def _load_active_users(self, context: WidgetContext) -> WidgetPayload:
        """Who has an account and who has used it lately. Administrative."""
        activity = self._dashboards.user_activity(
            active_since=datetime.now(UTC)
            - timedelta(minutes=settings.DASHBOARD_ACTIVE_USER_MINUTES)
        )
        return WidgetPayload(
            kind=WidgetPayloadKind.METRICS,
            metrics=(
                Metric(key="active_users", value=activity.recently_signed_in),
                Metric(key="total_accounts", value=activity.total),
                Metric(key="enabled_accounts", value=activity.active),
                Metric(key="disabled_accounts", value=activity.inactive),
                Metric(key="suspended_accounts", value=activity.suspended),
                *(
                    Metric(key=f"accounts_{bucket.key}", value=bucket.count)
                    for bucket in activity.by_role
                ),
            ),
        )

    def _load_processing_queues(self, context: WidgetContext) -> WidgetPayload:
        """Outstanding pipeline work. Administrative."""
        depths = self._dashboards.queue_depths()
        return WidgetPayload(
            kind=WidgetPayloadKind.METRICS,
            metrics=(
                Metric(key="queued_total", value=depths.total),
                Metric(key="ocr_pending", value=depths.ocr_pending),
                Metric(key="ocr_processing", value=depths.ocr_processing),
                Metric(key="indexing_pending", value=depths.indexing_pending),
                Metric(key="indexing_processing", value=depths.indexing_processing),
                Metric(key="reports_pending", value=depths.report_pending),
                Metric(key="reports_processing", value=depths.report_processing),
            ),
        )


#: The process-wide cache for platform-wide widget payloads.
#:
#: Module-level rather than per service instance, because a service is built per
#: request: a cache rebuilt with it would never be read. Only entries whose key
#: :meth:`DashboardService._cache_key` produced can reach it, which is the whole
#: of the safety argument — nothing user-scoped has a key.
_PLATFORM_CACHE: dict[tuple[str, str, str], _CacheEntry] = {}


def clear_dashboard_cache() -> None:
    """Empty the platform-wide widget cache. For tests, and for an operator."""
    _PLATFORM_CACHE.clear()


def _unread_first_query(limit: int) -> NotificationListQuery:
    """The query the notifications widget sends: newest first, one short page.

    Built here rather than in the repository because it is a *presentation*
    decision — a dashboard tile shows a glance of the feed — and the repository's
    job is to execute a query rather than to have an opinion about what a widget
    wants.
    """
    return NotificationListQuery(
        page=1,
        page_size=limit,
        sort_by=NotificationSortField.CREATED_AT,
        sort_order=SortOrder.DESC,
    )


__all__ = [
    "Dashboard",
    "DashboardService",
    "WidgetContext",
    "WidgetLoader",
    "WidgetPayload",
    "WidgetResult",
    "WidgetStateValue",
    "clear_dashboard_cache",
]
