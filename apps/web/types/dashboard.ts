/**
 * Dashboard types.
 *
 * Mirror the API's dashboard payloads (`apps/api/schemas/dashboard.py`) and the
 * vocabularies defined in `apps/api/core/dashboard.py`. Union types rather than
 * magic strings, per the code standards: referencing a widget the platform does
 * not define is a compile error.
 *
 * **The widget catalog is server-driven, and that is the point of this module.**
 * A dashboard response carries, for every widget, its group, the shape of its
 * data, its suggested refresh interval, and — the field that does the most work —
 * the domain events after which it is stale. So this app has **no table mapping
 * widgets to events**: it reads one from the API and refreshes exactly what an
 * incoming event touched. A widget added on the server therefore appears,
 * authorizes itself, and starts updating live in a browser nobody redeployed.
 *
 * The nine payload *kinds* are the one thing that is closed here, and
 * deliberately: they are what a component renders, so a tenth is a real change to
 * this app rather than a configuration one. Nineteen widgets share nine
 * renderers, which is what keeps the twentieth free.
 *
 * **Nothing here describes a label.** A widget, a metric, and a bucket each carry
 * a stable `key`; the words come from this app's own copy (see
 * `components/dashboard/labels.ts`), because the API must never be the place a
 * translation lives.
 */

import type { Notification } from "@/types/notification";
import type { DomainEventType } from "@/types/realtime";

// --------------------------------------------------------------------------- //
// Vocabulary
// --------------------------------------------------------------------------- //

/** Every widget the platform defines. Mirrors `WidgetKey`. */
export const WIDGET_KEYS = [
  "quick_actions",
  "notifications",
  "recent_activity",
  "my_cases",
  "recent_cases",
  "case_status_overview",
  "case_analytics",
  "upcoming_hearings",
  "hearing_calendar",
  "recent_documents",
  "ocr_status",
  "document_analytics",
  "ai_reports",
  "recent_conversations",
  "ai_analytics",
  "timeline_activity",
  "storage_usage",
  "active_users",
  "processing_queues",
] as const;
export type WidgetKey = (typeof WIDGET_KEYS)[number];

/** The area of the platform a widget reports on. Mirrors `WidgetGroup`. */
export const WIDGET_GROUPS = [
  "general",
  "cases",
  "court",
  "documents",
  "ai",
  "timeline",
  "system",
] as const;
export type WidgetGroup = (typeof WIDGET_GROUPS)[number];

/** The shape of a widget's data. Mirrors `WidgetPayloadKind`. */
export const WIDGET_PAYLOAD_KINDS = [
  "metrics",
  "breakdown",
  "cases",
  "documents",
  "reports",
  "conversations",
  "activity",
  "notifications",
  "actions",
] as const;
export type WidgetPayloadKind = (typeof WIDGET_PAYLOAD_KINDS)[number];

/**
 * How a widget turned out.
 *
 * `empty` and `unavailable` are **not** interchangeable, and treating them as one
 * is the mistake this union exists to prevent: the first means the query ran and
 * there is nothing, the second means the query did not run. A component renders
 * an invitation for one and a retry for the other.
 */
export const WIDGET_STATES = ["ready", "empty", "unavailable"] as const;
export type WidgetState = (typeof WIDGET_STATES)[number];

/** Why a widget is unavailable. Mirrors `WidgetFailureReason`. */
export const WIDGET_ERROR_CODES = ["query_failed", "budget_exhausted", "unknown"] as const;
export type WidgetErrorCode = (typeof WIDGET_ERROR_CODES)[number];

/** How a metric's number should be read. Mirrors `MetricUnit`. */
export const METRIC_UNITS = [
  "count",
  "bytes",
  "percent",
  "days",
  "milliseconds",
] as const;
export type MetricUnit = (typeof METRIC_UNITS)[number];

/** The window analytics are measured over. Mirrors `DashboardRange`. */
export const DASHBOARD_RANGES = [
  "today",
  "last_7_days",
  "last_30_days",
  "custom",
] as const;
export type DashboardRange = (typeof DASHBOARD_RANGES)[number];

/** The shortcuts the dashboard offers. Mirrors `QuickActionKey`. */
export const QUICK_ACTION_KEYS = [
  "create_case",
  "upload_document",
  "generate_report",
  "open_assistant",
  "view_calendar",
] as const;
export type QuickActionKey = (typeof QUICK_ACTION_KEYS)[number];

// --------------------------------------------------------------------------- //
// Payload elements
// --------------------------------------------------------------------------- //

/**
 * One named figure.
 *
 * `value` is `null` when the figure is **undefined** rather than zero — an
 * average with no observations, a rate with no denominator. Rendering `null` as
 * `0` would be inventing a statistic, which is exactly what the spec's "Analytics
 * Data Integrity" section rules out, so the formatter shows an em dash.
 */
export interface DashboardMetric {
  key: string;
  value: number | null;
  unit: MetricUnit;
}

/** One labelled slice of a breakdown. */
export interface DashboardBucket {
  key: string;
  count: number;
}

/** The minimum needed to name a person on a card. */
export interface DashboardPerson {
  id: string;
  fullName: string;
}

/** A case, as a widget shows it. */
export interface DashboardCase {
  id: string;
  caseNumber: string;
  title: string;
  status: string;
  priority: string;
  courtName: string | null;
  nextHearingDate: string | null;
  updatedAt: string;
  assignedLawyer: DashboardPerson | null;
  assignedCourtRepresentative: DashboardPerson | null;
}

/** A document, as a widget shows it. */
export interface DashboardDocument {
  id: string;
  caseId: string;
  originalFilename: string;
  category: string;
  fileExtension: string;
  fileSize: number;
  version: number;
  createdAt: string;
}

/** One of the caller's reports, as a widget shows it. */
export interface DashboardReport {
  id: string;
  caseId: string;
  title: string;
  reportType: string;
  status: string;
  sectionsCompleted: number;
  sectionsTotal: number | null;
  createdAt: string;
}

/** One of the caller's assistant threads, as a widget shows it. */
export interface DashboardConversation {
  id: string;
  title: string;
  caseId: string | null;
  messageCount: number;
  lastMessageAt: string | null;
}

/** One timeline entry, as the activity widget shows it. */
export interface DashboardActivity {
  id: string;
  caseId: string;
  eventType: string;
  category: string;
  title: string;
  actorName: string | null;
  createdAt: string;
}

// --------------------------------------------------------------------------- //
// Payloads
// --------------------------------------------------------------------------- //

/**
 * A widget's data, discriminated on `kind`.
 *
 * A component switches on that one field and renders one of nine shapes. This is
 * what makes a new widget cost nothing on the frontend: it arrives with a `kind`
 * this app already draws.
 */
export type WidgetPayload =
  | { kind: "metrics"; metrics: DashboardMetric[] }
  | { kind: "breakdown"; total: number; buckets: DashboardBucket[] }
  | { kind: "cases"; cases: DashboardCase[] }
  | { kind: "documents"; documents: DashboardDocument[] }
  | { kind: "reports"; reports: DashboardReport[] }
  | { kind: "conversations"; conversations: DashboardConversation[] }
  | { kind: "activity"; activity: DashboardActivity[] }
  | { kind: "notifications"; notifications: Notification[] }
  | { kind: "actions" };

// --------------------------------------------------------------------------- //
// Widgets and the dashboard
// --------------------------------------------------------------------------- //

/**
 * What a client needs to draw a widget before its data arrives.
 *
 * `refreshEvents` is the server telling this app what makes the widget stale.
 * Nothing in `apps/web` decides that, which is why adding a widget touches no
 * file here.
 */
export interface WidgetDescriptor {
  key: WidgetKey;
  group: WidgetGroup;
  kind: WidgetPayloadKind;
  refreshEvents: DomainEventType[];
  refreshIntervalSeconds: number;
  platformWide: boolean;
}

/** One widget's state, data, and cost. */
export interface DashboardWidget {
  widget: WidgetDescriptor;
  state: WidgetState;
  generatedAt: string;
  durationMs: number;
  data: WidgetPayload | null;
  errorCode: WidgetErrorCode | null;
}

/** One assembled dashboard — everything the page needs, in one response. */
export interface Dashboard {
  generatedAt: string;
  range: DashboardRange;
  windowStart: string;
  windowEnd: string;
  windowDays: number;
  role: string;
  widgets: DashboardWidget[];
  quickActions: QuickActionKey[];
  failedWidgets: number;
  durationMs: number;
}

/** The widgets this caller may load, without their data. */
export interface WidgetCatalog {
  role: string;
  widgets: WidgetDescriptor[];
  quickActions: QuickActionKey[];
}

/** Platform-wide dashboard health. Requires `dashboard:monitor`. */
export interface DashboardMetrics {
  since: string;
  enabled: boolean;
  loads: number;
  refreshes: number;
  widgetsLoaded: number;
  widgetsFailed: number;
  widgetSuccessRate: number;
  averageLoadMs: number | null;
  averageWidgetMs: number | null;
  activeUsers: number;
  activeUsersCapped: boolean;
  averageMsByWidget: Record<string, number>;
  failuresByWidget: Record<string, number>;
  failuresByReason: Record<string, number>;
}

// --------------------------------------------------------------------------- //
// Requests
// --------------------------------------------------------------------------- //

/**
 * The filter a dashboard is read with.
 *
 * `startDate` and `endDate` are only meaningful for `range: "custom"`, and the
 * API rejects a custom range without both — validated here too, so a malformed
 * filter never becomes a request.
 */
export interface DashboardQuery {
  range: DashboardRange;
  startDate: string | null;
  endDate: string | null;
  /** Load only these widgets. Never widens a dashboard; the server intersects. */
  widgets?: readonly WidgetKey[];
  listSize?: number;
  /** ISO 639-1 code the notifications widget renders its titles in. */
  language?: string | null;
}

/** The filter one widget is refreshed with. */
export type WidgetQuery = Omit<DashboardQuery, "widgets">;

/** What a dashboard is read with when nothing has been chosen. */
export const DEFAULT_DASHBOARD_QUERY: DashboardQuery = {
  range: "last_30_days",
  startDate: null,
  endDate: null,
};

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

/** Whether a value is a widget this build knows how to draw. */
export function isKnownWidget(value: string): value is WidgetKey {
  return (WIDGET_KEYS as readonly string[]).includes(value);
}

/**
 * Whether a widget should be re-read after this event.
 *
 * The whole of the frontend's real-time dashboard logic, and it is a lookup in
 * data the server sent rather than a table this app maintains.
 */
export function isStaleAfter(
  descriptor: WidgetDescriptor,
  event: DomainEventType,
): boolean {
  return descriptor.refreshEvents.includes(event);
}
