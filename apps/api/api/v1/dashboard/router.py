"""Dashboard endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.dashboard.DashboardService`, and shape the HTTP response. No
business logic lives here, and — the part that matters for this feature — **no
route writes anything**. The dashboard owns no table, so there is no create,
update, or delete path: every one of these is a read, and turning the feature off
removes a page rather than a capability.

**Four endpoints, and the shape of the set is the spec's API section.**
``19-dashboard-analytics.md`` asks for *"an aggregated dashboard endpoint"* and
warns against *"one API request per widget"* — so ``GET /dashboard`` returns the
whole page in one response. It also asks that *"refreshing one widget should not
reload the entire dashboard"* — so ``GET /dashboard/widgets/{key}`` exists beside
it, serving the **same loader**. Those two requirements pull in opposite
directions and are both satisfied because the unit of work is a widget, not a
page: the aggregate endpoint is a loop over the single-widget one.

Authorization is layered, and the layers answer different questions:

* there is **no capability on the dashboard itself**, and that is deliberate: it
  is the landing page for every authenticated role, so ``CurrentUser`` is the
  only gate. A permission every role holds is not a permission;
* the **widget** is authorized per widget, against the capabilities that own its
  rows, by :class:`~services.dashboard_access.DashboardAccessPolicy`. An
  unauthorized widget is not filtered out of a computed response — it is never
  computed;
* the **rows** are scoped by the policies that own them: the case scope from
  :class:`~services.case_access.CaseAccessPolicy`, and identity equality for the
  private histories (reports, conversations, notifications);
* ``GET /dashboard/metrics`` is the one endpoint here with a capability, and it
  is administrative — ``dashboard:monitor``, alongside every other ``*:monitor``.

**There is deliberately no per-resource access module behind these routes**
beyond the policy named above, and that policy owns no rules of its own. A
dashboard is the first feature that reads across every module, so it is exactly
where a second, subtly different copy of "may this person see this" would appear
— and a wrong copy here would be invisible, because a dashboard shows a number
rather than a document.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, status

from api.authorization import require_permission
from api.deps import CurrentUser, DashboardServiceDep
from core.dashboard import WidgetKey
from core.permissions import Permission
from models.user import User
from schemas.dashboard import (
    DashboardMetricsRead,
    DashboardQuery,
    DashboardRead,
    WidgetCatalogRead,
    WidgetDescriptorRead,
    WidgetQuery,
    WidgetRead,
)

logger = structlog.get_logger(__name__)

#: Mounted under ``/dashboard``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

DashboardMonitor = Annotated[
    User, Depends(require_permission(Permission.DASHBOARD_MONITOR))
]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": "The account is disabled or lacks the required permission.",
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such widget is available to this caller — it does not exist, or it is "
            "one they are not authorized for. The two are deliberately "
            "indistinguishable."
        ),
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Request validation failed."}
}
_UNAVAILABLE: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "The dashboard is disabled on this deployment.",
    }
}


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #


@router.get(
    "/widgets",
    response_model=WidgetCatalogRead,
    status_code=status.HTTP_200_OK,
    summary="The widgets available to you",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_UNAVAILABLE},
)
def list_widgets(actor: CurrentUser, dashboards: DashboardServiceDep) -> WidgetCatalogRead:
    """Return this caller's widgets and quick actions, without loading any data.

    **Metadata only, and it runs no queries.** What a client reads once on mount:
    which tiles to draw placeholders for, in what order, of what shape, on what
    interval — and, the field that does the most work, **which domain events make
    each one stale**.

    That last one is why the frontend has no widget table of its own. A client
    subscribed to the real-time channel looks an incoming event up in what this
    endpoint returned and refreshes exactly the widgets it touched, so a widget
    added on the server starts updating live in a browser nobody redeployed.

    **Registered before `/{widget_key}`** so `widgets` is matched as a literal
    path segment rather than parsed as a widget key — FastAPI resolves routes in
    declaration order.

    No capability of its own: the catalog *is* the authorization answer, and it
    lists exactly what this caller may load.
    """
    return WidgetCatalogRead(
        role=actor.role.value,
        widgets=[
            WidgetDescriptorRead(
                key=definition.key,
                group=definition.group,
                kind=definition.kind,
                refresh_events=sorted(event.value for event in definition.events),
                refresh_interval_seconds=definition.refresh_seconds,
                platform_wide=definition.platform_wide,
            )
            for definition in dashboards.catalog(actor=actor)
        ],
        quick_actions=list(dashboards.quick_actions(actor=actor)),
    )


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


@router.get(
    "/metrics",
    response_model=DashboardMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Dashboard metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_dashboard_metrics(
    actor: DashboardMonitor, dashboards: DashboardServiceDep
) -> DashboardMetricsRead:
    """Return platform-wide dashboard health.

    The five figures the spec's Monitoring section names — **dashboard load time,
    widget load time, refresh frequency, failed widget requests, and active
    dashboard users** — plus the per-widget breakdowns that turn "the dashboard is
    slow" into "`storage_usage` is slow", which is the only form of that sentence
    anybody can act on.

    **Every figure carries `since`, without exception**, and that is a real
    difference from `/notifications/metrics` and `/reports/metrics`. Those split
    their numbers between SQL aggregates and process counters and say which is
    which; this one cannot, because the dashboard persists nothing — there is no
    row recording a load, so there is no exact figure to prefer and no restart-
    surviving half to report.

    `active_users` is a **distinct-person count derived from salted digests**: the
    recorder folds an identifier in and destroys it, so this can say how many
    people opened a dashboard and can never say who. See
    `services/dashboard_metrics.py` for why that was the resolution rather than
    keeping a set of user identifiers.

    An operational view, so it is gated on `dashboard:monitor` and reports
    **counts, durations, widget keys, and failure codes only** — never a case, a
    document, a figure from somebody's dashboard, or whose it was.

    **Registered before `/{widget_key}`** for the same reason `/widgets` is.
    """
    snapshot = dashboards.metrics()
    return DashboardMetricsRead(
        since=snapshot.since,
        enabled=dashboards.enabled,
        loads=snapshot.loads,
        refreshes=snapshot.refreshes,
        widgets_loaded=snapshot.widgets_loaded,
        widgets_failed=snapshot.widgets_failed,
        widget_success_rate=snapshot.widget_success_rate,
        average_load_ms=snapshot.average_load_ms,
        average_widget_ms=snapshot.average_widget_ms,
        active_users=snapshot.active_users,
        active_users_capped=snapshot.active_users_capped,
        average_ms_by_widget=snapshot.average_ms_by_widget,
        failures_by_widget=snapshot.failures_by_widget,
        failures_by_reason=snapshot.failures_by_reason,
    )


# --------------------------------------------------------------------------- #
# One widget
# --------------------------------------------------------------------------- #


@router.get(
    "/widgets/{widget_key}",
    response_model=WidgetRead,
    status_code=status.HTTP_200_OK,
    summary="Refresh one widget",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_VALIDATION, **_UNAVAILABLE},
)
def refresh_widget(
    widget_key: WidgetKey,
    actor: CurrentUser,
    dashboards: DashboardServiceDep,
    query: Annotated[WidgetQuery, Query()],
) -> WidgetRead:
    """Load exactly one widget.

    The spec's *"refreshing one widget should not reload the entire dashboard"*,
    and it runs precisely the queries that widget needs. It is the **same loader**
    the aggregated endpoint calls, so a refreshed tile cannot drift from the one
    rendered beside it — which is the failure a second code path would eventually
    produce.

    The range parameters are the same ones a full load takes, so a refresh
    measures the window the page around it is showing. A refresh that silently
    reverted to the default window would make one card disagree with its
    neighbours, and nothing on screen would say why.

    A widget key this version does not define and one this caller is not
    authorized for both answer **404**. That is deliberate: a 403 would turn this
    endpoint into an oracle for "which analytics does this deployment have that I
    am not trusted with", which is exactly what *"users must never see widgets
    they are not authorized to access"* is protecting. `GET /dashboard/widgets`
    lists what this caller may ask for, so nobody has to guess.

    **A failing widget still answers 200.** Its `state` is `unavailable` and its
    `error_code` says why — the same contract the aggregated endpoint gives, so a
    client's retry path is one path rather than two.
    """
    result = dashboards.refresh(
        widget_key,
        actor=actor,
        dashboard_range=query.range,
        start=query.start_date,
        end=query.end_date,
        list_size=query.list_size,
    )
    return WidgetRead.from_result(result, language=query.language)


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=DashboardRead,
    status_code=status.HTTP_200_OK,
    summary="Your dashboard",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION, **_UNAVAILABLE},
)
def get_dashboard(
    actor: CurrentUser,
    dashboards: DashboardServiceDep,
    query: Annotated[DashboardQuery, Query()],
) -> DashboardRead:
    """Assemble every widget this caller may see, in one response.

    **The spec's aggregated endpoint.** The layout comes from the caller's role,
    the visibility from their permissions, and the rows from the scopes their
    access policies resolve — so two people in the same role with different
    caseloads receive the same widgets containing different numbers, and a court
    representative receives neither the AI widgets nor the platform ones.

    `range` is resolved **once** and handed to every widget, which is what makes
    the spec's *"widgets should update consistently when filters change"*
    structural: two tiles in one response cannot be measuring different
    fortnights, because there is only one window.

    `widgets` narrows the page and can never widen it: it is intersected with the
    role layout and the permission filter, in that order, so an unknown or
    unauthorized key contributes nothing rather than producing an error.

    **A failing widget does not fail the request.** It comes back with
    `state: unavailable` and a code, the rest of the page arrives, and
    `failed_widgets` on the envelope says how many — the spec's *"one failing
    widget must not prevent the dashboard from loading"*. A dashboard that runs
    out of its time budget sheds the widgets it has not reached the same way, with
    `budget_exhausted`, so a slow page degrades into a partial one instead of a
    hung request.

    **No capability is required**, which is the one endpoint-level decision worth
    stating: every authenticated role has a dashboard, and what it contains is
    decided widget by widget. A permission here would be one every role holds.

    **Every query parameter lives on `DashboardQuery`, including `language`.**
    FastAPI expands a Pydantic query model into individual parameters only when it
    is the endpoint's *sole* query parameter, so one declared beside it would turn
    the model into a single required parameter named `query` — and every
    unqualified `GET /dashboard` into a 422.
    """
    dashboard = dashboards.load(
        actor=actor,
        dashboard_range=query.range,
        start=query.start_date,
        end=query.end_date,
        only=query.widgets,
        list_size=query.list_size,
    )
    return DashboardRead.from_dashboard(dashboard, language=query.language)


__all__ = ["router"]
