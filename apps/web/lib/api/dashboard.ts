/**
 * Dashboard API calls.
 *
 * Thin, typed wrappers over the `/dashboard` endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape —
 * and a backend change that alters a payload fails here, loudly, instead of
 * surfacing as `undefined` in a tile.
 *
 * **There is no `createDashboard` here, and there will not be one.** The
 * dashboard owns no data and saves no layout: it is a view assembled per request
 * from the modules that do own data, under their own authorization. Every
 * function below is a read.
 *
 * **Unknown widgets are dropped, not fatal.** A server that ships a widget this
 * build has never heard of should produce a dashboard missing one card rather
 * than an error page — the spec asks the widget system to accept future widgets
 * "without redesign", and this is that requirement seen from the browser. The
 * filtering happens here, once, so no component ever has to hold a key it cannot
 * draw.
 */

import { apiRequest } from "@/lib/api/client";
import { DASHBOARD_ENDPOINTS } from "@/lib/api/config";
import { toNotification } from "@/lib/api/notifications";
import {
  dashboardMetricsSchema,
  dashboardSchema,
  widgetCatalogSchema,
  widgetSchema,
} from "@/lib/validation/dashboard";
import {
  isKnownWidget,
  type Dashboard,
  type DashboardMetrics,
  type DashboardQuery,
  type DashboardWidget,
  type WidgetCatalog,
  type WidgetDescriptor,
  type WidgetKey,
  type WidgetPayload,
  type WidgetQuery,
} from "@/types/dashboard";
import type { DomainEventType } from "@/types/realtime";

type DashboardWire = ReturnType<typeof dashboardSchema.parse>;
type WidgetWire = ReturnType<typeof widgetSchema.parse>;
type DescriptorWire = WidgetWire["widget"];
type PayloadWire = NonNullable<WidgetWire["data"]>;
type MetricsWire = ReturnType<typeof dashboardMetricsSchema.parse>;

// --------------------------------------------------------------------------- //
// Wire → domain
// --------------------------------------------------------------------------- //

/** Map one widget descriptor, or `null` when this build cannot draw it. */
function toDescriptor(payload: DescriptorWire): WidgetDescriptor | null {
  if (!isKnownWidget(payload.key)) return null;

  return {
    key: payload.key,
    group: payload.group,
    kind: payload.kind,
    // Cast rather than filter: an event this build has never heard of is simply
    // one no widget here reacts to, and dropping it would silently shorten a
    // list whose only use is membership testing.
    refreshEvents: payload.refresh_events as DomainEventType[],
    refreshIntervalSeconds: payload.refresh_interval_seconds,
    platformWide: payload.platform_wide,
  };
}

/** Map one widget payload onto its discriminated domain shape. */
function toPayload(payload: PayloadWire): WidgetPayload {
  switch (payload.kind) {
    case "metrics":
      return { kind: "metrics", metrics: payload.metrics };
    case "breakdown":
      return { kind: "breakdown", total: payload.total, buckets: payload.buckets };
    case "cases":
      return {
        kind: "cases",
        cases: payload.cases.map((item) => ({
          id: item.id,
          caseNumber: item.case_number,
          title: item.title,
          status: item.status,
          priority: item.priority,
          courtName: item.court_name,
          nextHearingDate: item.next_hearing_date,
          updatedAt: item.updated_at,
          assignedLawyer: item.assigned_lawyer
            ? { id: item.assigned_lawyer.id, fullName: item.assigned_lawyer.full_name }
            : null,
          assignedCourtRepresentative: item.assigned_court_representative
            ? {
                id: item.assigned_court_representative.id,
                fullName: item.assigned_court_representative.full_name,
              }
            : null,
        })),
      };
    case "documents":
      return {
        kind: "documents",
        documents: payload.documents.map((item) => ({
          id: item.id,
          caseId: item.case_id,
          originalFilename: item.original_filename,
          category: item.category,
          fileExtension: item.file_extension,
          fileSize: item.file_size,
          version: item.version,
          createdAt: item.created_at,
        })),
      };
    case "reports":
      return {
        kind: "reports",
        reports: payload.reports.map((item) => ({
          id: item.id,
          caseId: item.case_id,
          title: item.title,
          reportType: item.report_type,
          status: item.status,
          sectionsCompleted: item.sections_completed,
          sectionsTotal: item.sections_total,
          createdAt: item.created_at,
        })),
      };
    case "conversations":
      return {
        kind: "conversations",
        conversations: payload.conversations.map((item) => ({
          id: item.id,
          title: item.title,
          caseId: item.case_id,
          messageCount: item.message_count,
          lastMessageAt: item.last_message_at,
        })),
      };
    case "activity":
      return {
        kind: "activity",
        activity: payload.activity.map((item) => ({
          id: item.id,
          caseId: item.case_id,
          eventType: item.event_type,
          category: item.category,
          title: item.title,
          actorName: item.actor_name,
          createdAt: item.created_at,
        })),
      };
    case "notifications":
      // Reuses the notification mapper rather than repeating it: a notification's
      // title and message are *rendered* by the server in the reader's language,
      // and a second projection would be a second place for that to go wrong.
      return {
        kind: "notifications",
        notifications: payload.notifications.map(toNotification),
      };
    case "actions":
      return { kind: "actions" };
  }
}

/** Map one widget, or `null` when this build cannot draw it. */
function toWidget(payload: WidgetWire): DashboardWidget | null {
  const descriptor = toDescriptor(payload.widget);
  if (descriptor === null) return null;

  return {
    widget: descriptor,
    state: payload.state,
    generatedAt: payload.generated_at,
    durationMs: payload.duration_ms,
    data: payload.data ? toPayload(payload.data) : null,
    errorCode: payload.error_code,
  };
}

function toDashboard(payload: DashboardWire): Dashboard {
  return {
    generatedAt: payload.generated_at,
    range: payload.range,
    windowStart: payload.window_start,
    windowEnd: payload.window_end,
    windowDays: payload.window_days,
    role: payload.role,
    widgets: payload.widgets
      .map(toWidget)
      .filter((widget): widget is DashboardWidget => widget !== null),
    quickActions: payload.quick_actions,
    failedWidgets: payload.failed_widgets,
    durationMs: payload.duration_ms,
  };
}

function toMetrics(payload: MetricsWire): DashboardMetrics {
  return {
    since: payload.since,
    enabled: payload.enabled,
    loads: payload.loads,
    refreshes: payload.refreshes,
    widgetsLoaded: payload.widgets_loaded,
    widgetsFailed: payload.widgets_failed,
    widgetSuccessRate: payload.widget_success_rate,
    averageLoadMs: payload.average_load_ms,
    averageWidgetMs: payload.average_widget_ms,
    activeUsers: payload.active_users,
    activeUsersCapped: payload.active_users_capped,
    averageMsByWidget: payload.average_ms_by_widget,
    failuresByWidget: payload.failures_by_widget,
    failuresByReason: payload.failures_by_reason,
  };
}

// --------------------------------------------------------------------------- //
// Requests
// --------------------------------------------------------------------------- //

/**
 * Build the query string for a dashboard read.
 *
 * The date bounds are sent **only** for a custom range, so the request URL
 * reflects what is actually being asked — which also makes the query a stable
 * cache key for TanStack Query, and keeps a stale date left in a form from
 * changing what a fixed range means.
 */
export function buildDashboardParams(query: DashboardQuery): string {
  const params = new URLSearchParams({ range: query.range });

  if (query.range === "custom") {
    if (query.startDate) params.set("start_date", query.startDate);
    if (query.endDate) params.set("end_date", query.endDate);
  }
  if (query.listSize) params.set("list_size", String(query.listSize));
  if (query.language) params.set("language", query.language);
  for (const key of query.widgets ?? []) params.append("widgets", key);

  return params.toString();
}

/**
 * Fetch the whole dashboard.
 *
 * **One request for the page**, which is the spec's aggregated endpoint: the
 * widgets, their data, their metadata, the resolved window, and the quick
 * actions, in one round trip.
 *
 * A widget that failed server-side arrives with `state: "unavailable"` and a
 * code rather than failing this call — so there is no error path here for a
 * partial dashboard, which is precisely the point of the design.
 */
export async function fetchDashboard(query: DashboardQuery): Promise<Dashboard> {
  const raw = await apiRequest<unknown>(
    `${DASHBOARD_ENDPOINTS.dashboard}?${buildDashboardParams(query)}`,
  );
  return toDashboard(dashboardSchema.parse(raw));
}

/**
 * Fetch one widget.
 *
 * What a refresh button and a live event both call. It runs exactly the queries
 * that widget needs, so refreshing a tile costs a tile.
 */
export async function fetchWidget(
  key: WidgetKey,
  query: WidgetQuery,
): Promise<DashboardWidget> {
  const raw = await apiRequest<unknown>(
    `${DASHBOARD_ENDPOINTS.widget(key)}?${buildDashboardParams(query)}`,
  );
  const widget = toWidget(widgetSchema.parse(raw));
  if (widget === null) {
    // Only reachable if the server answered with a different widget than the one
    // asked for, which would be a contract break rather than a missing feature.
    throw new Error(`The API returned an unrecognised widget for "${key}".`);
  }
  return widget;
}

/**
 * Fetch the widget catalog.
 *
 * Metadata only, and it runs no queries server-side: which tiles this caller may
 * load, in what order, and which events make each stale.
 */
export async function fetchWidgetCatalog(): Promise<WidgetCatalog> {
  const raw = await apiRequest<unknown>(DASHBOARD_ENDPOINTS.widgets);
  const data = widgetCatalogSchema.parse(raw);

  return {
    role: data.role,
    widgets: data.widgets
      .map(toDescriptor)
      .filter((descriptor): descriptor is WidgetDescriptor => descriptor !== null),
    quickActions: data.quick_actions,
  };
}

/** Fetch platform-wide dashboard health. Requires `dashboard:monitor`. */
export async function fetchDashboardMetrics(): Promise<DashboardMetrics> {
  const raw = await apiRequest<unknown>(DASHBOARD_ENDPOINTS.metrics);
  return toMetrics(dashboardMetricsSchema.parse(raw));
}
