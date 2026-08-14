"use client";

import * as React from "react";
import {
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  fetchDashboard,
  fetchDashboardMetrics,
  fetchWidget,
  fetchWidgetCatalog,
} from "@/lib/api/dashboard";
import { useRealtimeEvents } from "@/hooks/use-realtime";
import {
  useCodeMessage,
  useErrorMessage,
  type ErrorCodeMap,
} from "@/hooks/use-error-message";
import type {
  Dashboard,
  DashboardMetrics,
  DashboardQuery,
  DashboardWidget,
  WidgetCatalog,
  WidgetKey,
} from "@/types/dashboard";
import { isStaleAfter } from "@/types/dashboard";

/**
 * Server state for the dashboard.
 *
 * TanStack Query per `architecture.md`: a dashboard is server state, so it is
 * cached and invalidated rather than mirrored into a client store.
 *
 * **This module is the one place on the platform whose live updates are
 * server-described.** Every other feature's staleness rules live in
 * `lib/realtime/sync.ts`, a table this app maintains. A dashboard cannot have
 * one: its widgets are decided by the server, and a nineteen-row table here would
 * have to be edited every time a widget was added — which is exactly the redesign
 * `19-dashboard-analytics.md` says a new widget must not require. So each widget
 * arrives carrying the events that make *it* stale, and
 * {@link useDashboardRealtime} refreshes what an event touched, one widget at a
 * time.
 *
 * **It polls as well**, and the polling is deliberate rather than a fallback.
 * `15-real-time-synchronization.md`'s rule that *"nothing may depend on the
 * channel"* applies here as much as anywhere: a deployment with real-time off, a
 * refused connection, and a browser that blocks WebSockets must all still show a
 * lawyer their hearings. The interval is slow, and the channel is what makes the
 * page feel immediate.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the dashboard API.
 */

/** How often the whole page re-reads when the channel is not carrying it. */
export const DASHBOARD_POLL_INTERVAL_MS = 120_000;

/** How often the administrative metrics view re-reads. */
const MONITOR_POLL_INTERVAL_MS = 10_000;

/** Query keys. */
export const dashboardKeys = {
  all: ["dashboard"] as const,
  catalog: () => [...dashboardKeys.all, "catalog"] as const,
  pages: () => [...dashboardKeys.all, "page"] as const,
  page: (query: DashboardQuery) => [...dashboardKeys.pages(), query] as const,
  widgets: () => [...dashboardKeys.all, "widget"] as const,
  widget: (key: WidgetKey, query: DashboardQuery) =>
    [...dashboardKeys.widgets(), key, query] as const,
  metrics: () => [...dashboardKeys.all, "metrics"] as const,
};

/**
 * Translate a failure into a message safe to show the user.
 *
 * Branches on the API's machine-readable `code` rather than on message text,
 * which is localizable and may change — the same rule every other hook module
 * here follows.
 *
 * Note what is **not** handled: a widget that failed server-side. That is not an
 * error — it arrives inside a successful response with its own `errorCode`, and
 * it is rendered by the card rather than by the page. This function is for the
 * dashboard request itself failing.
 */
const DASHBOARD_ERRORS: ErrorCodeMap = {
  dashboard_disabled: "disabled",
  dashboard_widget_not_found: "widgetNotFound",
  forbidden: "noAccess",
};

export function useDashboardErrorMessage(): (error: unknown) => string {
  return useErrorMessage("dashboard.errors", DASHBOARD_ERRORS);
}

/**
 * Why a widget could not be loaded, in words.
 *
 * A code rather than a server message, so the sentence can be translated and so
 * nothing internal reaches the screen — the two reasons the API returns a code at
 * all. It arrives on a **successful** response, which is why this reads a bare
 * code rather than unwrapping an `Error`.
 */
const WIDGET_ERRORS: ErrorCodeMap = {
  budget_exhausted: "budgetExhausted",
  query_failed: "queryFailed",
};

export function useWidgetErrorMessage(): (code: string | null) => string {
  return useCodeMessage("dashboard.errors.widget", WIDGET_ERRORS);
}

// --------------------------------------------------------------------------- //
// Reads
// --------------------------------------------------------------------------- //

/**
 * The whole dashboard, in one request.
 *
 * The spec's aggregated endpoint: one round trip for the page rather than one per
 * widget. Widgets that failed server-side are part of a *successful* response, so
 * `isError` here means the page could not be read at all — which is the only case
 * that deserves a full-page error state.
 */
export function useDashboard(
  query: DashboardQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<Dashboard, unknown> {
  return useQuery({
    queryKey: dashboardKeys.page(query),
    queryFn: () => fetchDashboard(query),
    enabled: options.enabled ?? true,
    refetchInterval: DASHBOARD_POLL_INTERVAL_MS,
    // A dashboard is a glance at the present, so a cached page shown while a
    // fresh one loads is right — but only briefly, or a tab left open overnight
    // renders yesterday for a moment on focus.
    staleTime: 30_000,
  });
}

/**
 * One widget, on demand.
 *
 * Disabled by default: this is what a refresh button turns on, not something a
 * card fetches for itself. A widget that fetched itself would defeat the
 * aggregated endpoint — nineteen tiles mounting would be nineteen requests, which
 * is precisely what the spec warns against.
 */
export function useWidget(
  key: WidgetKey,
  query: DashboardQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<DashboardWidget, unknown> {
  return useQuery({
    queryKey: dashboardKeys.widget(key, query),
    queryFn: () => fetchWidget(key, query),
    enabled: options.enabled ?? false,
  });
}

/**
 * The widget catalog: what this caller may load, and what makes each stale.
 *
 * Rarely needed on its own, because a full dashboard already carries a descriptor
 * per widget. It exists for a client that wants to lay out placeholders before
 * the first page arrives, and it runs no queries server-side.
 */
export function useWidgetCatalog(
  options: { enabled?: boolean } = {},
): UseQueryResult<WidgetCatalog, unknown> {
  return useQuery({
    queryKey: dashboardKeys.catalog(),
    queryFn: fetchWidgetCatalog,
    enabled: options.enabled ?? true,
    // The catalog changes when the deployment changes, not when data does.
    staleTime: 10 * 60_000,
  });
}

/**
 * Platform-wide dashboard health. Requires `dashboard:monitor`.
 *
 * Polled rather than pushed, for the reason `useRealtimeMetrics` records: a panel
 * that updated from the thing it measures would stop updating precisely when that
 * thing broke, which is the moment somebody is looking at it.
 */
export function useDashboardMetrics(
  options: { enabled?: boolean } = {},
): UseQueryResult<DashboardMetrics, unknown> {
  return useQuery({
    queryKey: dashboardKeys.metrics(),
    queryFn: fetchDashboardMetrics,
    enabled: options.enabled ?? true,
    refetchInterval: MONITOR_POLL_INTERVAL_MS,
  });
}

// --------------------------------------------------------------------------- //
// Live updates
// --------------------------------------------------------------------------- //

/**
 * Refresh the widgets an incoming event makes stale.
 *
 * **The whole of the dashboard's real-time behaviour, and it holds no rules.**
 * Each widget in `dashboard.widgets` carries the event types the server says
 * make it stale; this listens to the channel and, when one arrives, refetches
 * the page **only if some widget on it cares**. A widget added on the server
 * therefore starts updating live here without a line changing.
 *
 * It refetches the aggregated page rather than the individual widgets, and that
 * is a deliberate trade: a burst of events during a bulk upload would otherwise
 * fire one request per affected widget per event, where one page read costs a
 * single round trip and returns them consistent with each other. Per-widget
 * refresh stays available for the thing a person actually does one at a time —
 * pressing refresh on a card.
 *
 * A subscription is never required: with the channel closed, the poll above is
 * what keeps the page current, exactly as it was before this feature.
 */
export function useDashboardRealtime(
  dashboard: Dashboard | undefined,
  query: DashboardQuery,
): void {
  const client = useQueryClient();

  // Held in a ref so an arriving event reads the current descriptors without the
  // listener being torn down and rebuilt on every dashboard refetch.
  const descriptors = React.useRef(dashboard?.widgets ?? []);
  React.useEffect(() => {
    descriptors.current = dashboard?.widgets ?? [];
  }, [dashboard]);

  useRealtimeEvents(
    React.useCallback(
      (event) => {
        const affected = descriptors.current.some((widget) =>
          isStaleAfter(widget.widget, event.event),
        );
        if (!affected) return;

        void client.invalidateQueries({ queryKey: dashboardKeys.page(query) });
      },
      [client, query],
    ),
  );
}

/**
 * Refresh one widget by hand.
 *
 * What a card's refresh button calls. It invalidates that widget's own cache
 * entry and the page it came from — the second because the page response is where
 * the card's data actually lives, and leaving it stale would make the tile revert
 * on the next background poll.
 */
export function useRefreshWidget(
  query: DashboardQuery,
): (key: WidgetKey) => Promise<void> {
  const client = useQueryClient();

  return React.useCallback(
    async (key: WidgetKey) => {
      const widget = await fetchWidget(key, query);

      client.setQueryData<Dashboard>(dashboardKeys.page(query), (current) =>
        current
          ? {
              ...current,
              widgets: current.widgets.map((existing) =>
                existing.widget.key === key ? widget : existing,
              ),
              // Recomputed rather than carried over: a widget that just recovered
              // must not leave the page's "some widgets failed" banner up.
              failedWidgets: current.widgets.filter((existing) =>
                existing.widget.key === key
                  ? widget.state === "unavailable"
                  : existing.state === "unavailable",
              ).length,
            }
          : current,
      );
      client.setQueryData(dashboardKeys.widget(key, query), widget);
    },
    [client, query],
  );
}
