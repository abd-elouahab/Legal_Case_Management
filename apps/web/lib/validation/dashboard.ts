/**
 * Zod schemas for the dashboard.
 *
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/dashboard.py`; where they must agree, the API is the
 * authority.
 *
 * **Two fields are deliberately loose, and the reason is the same one both
 * times: this app must not break when the server grows.** A widget's
 * `refresh_events` is parsed as a plain string array, and a case's `status` and a
 * document's `category` as plain strings — those vocabularies are open or
 * versioned on the server, and a strict enum here would turn "the backend added
 * an event type" into a dashboard that will not load. The widget `key`, `group`,
 * `kind`, and `state` are strict for the opposite reason: this app has a renderer
 * per kind, so an unrecognised one is a genuine contract break rather than a
 * newer backend being ahead of this build — the same distinction
 * `lib/validation/notification.ts` draws between a category and a priority.
 *
 * **Unknown widgets are dropped rather than fatal.** A server that ships a
 * twentieth widget before this app knows how to draw it should produce a
 * dashboard missing one card, not an error page — so the widget array is filtered
 * after parsing, in `lib/api/dashboard.ts`, and this schema stays permissive about
 * the key while the type stays strict about it.
 */

import { z } from "zod";

import {
  DASHBOARD_RANGES,
  METRIC_UNITS,
  QUICK_ACTION_KEYS,
  WIDGET_ERROR_CODES,
  WIDGET_GROUPS,
  WIDGET_PAYLOAD_KINDS,
  WIDGET_STATES,
} from "@/types/dashboard";
import { notificationSchema } from "@/lib/validation/notification";

/** Largest `list_size` the API accepts, matching `DASHBOARD_MAX_LIST_SIZE`. */
export const MAX_WIDGET_LIST_SIZE = 20;

/** Longest custom range the API accepts, matching `DASHBOARD_MAX_RANGE_DAYS`. */
export const MAX_DASHBOARD_RANGE_DAYS = 366;

// --------------------------------------------------------------------------- //
// Requests
// --------------------------------------------------------------------------- //

/**
 * The dashboard filter, validated before it becomes a query string.
 *
 * The custom-range rules are checked here as well as on the server so a filter
 * somebody typed is refused by the form rather than by a 422 — the API remains
 * the authority, and this is the copy that gives immediate feedback.
 */
export const dashboardQuerySchema = z
  .object({
    range: z.enum(DASHBOARD_RANGES).catch("last_30_days"),
    startDate: z.string().nullable().catch(null),
    endDate: z.string().nullable().catch(null),
    listSize: z.coerce.number().int().min(1).max(MAX_WIDGET_LIST_SIZE).optional(),
    language: z.string().max(10).nullable().optional(),
  })
  .refine(
    (query) =>
      query.range !== "custom" ||
      (query.startDate !== null && query.endDate !== null),
    { message: "Choose both a start and an end date.", path: ["startDate"] },
  )
  .refine(
    (query) =>
      query.range !== "custom" ||
      query.startDate === null ||
      query.endDate === null ||
      query.endDate >= query.startDate,
    { message: "The end date must not be before the start date.", path: ["endDate"] },
  );

export type DashboardQueryValues = z.input<typeof dashboardQuerySchema>;

// --------------------------------------------------------------------------- //
// Responses
// --------------------------------------------------------------------------- //

const personSchema = z.object({
  id: z.string(),
  full_name: z.string(),
});

const metricSchema = z.object({
  key: z.string(),
  // Nullable, and never coerced to 0 — see `DashboardMetric`.
  value: z.number().nullable(),
  unit: z.enum(METRIC_UNITS).catch("count"),
});

const bucketSchema = z.object({
  key: z.string(),
  count: z.number(),
});

const caseSchema = z.object({
  id: z.string(),
  case_number: z.string(),
  title: z.string(),
  // Loose on purpose — see the module note.
  status: z.string(),
  priority: z.string(),
  court_name: z.string().nullable(),
  next_hearing_date: z.string().nullable(),
  updated_at: z.string(),
  assigned_lawyer: personSchema.nullable(),
  assigned_court_representative: personSchema.nullable(),
});

const documentSchema = z.object({
  id: z.string(),
  case_id: z.string(),
  original_filename: z.string(),
  category: z.string(),
  file_extension: z.string(),
  file_size: z.number(),
  version: z.number(),
  created_at: z.string(),
});

const reportSchema = z.object({
  id: z.string(),
  case_id: z.string(),
  title: z.string(),
  report_type: z.string(),
  status: z.string(),
  sections_completed: z.number(),
  sections_total: z.number().nullable(),
  created_at: z.string(),
});

const conversationSchema = z.object({
  id: z.string(),
  title: z.string(),
  case_id: z.string().nullable(),
  message_count: z.number(),
  last_message_at: z.string().nullable(),
});

const activitySchema = z.object({
  id: z.string(),
  case_id: z.string(),
  event_type: z.string(),
  category: z.string(),
  title: z.string(),
  actor_name: z.string().nullable(),
  created_at: z.string(),
});

/** A widget's data, discriminated on `kind` exactly as the API discriminates it. */
export const widgetPayloadSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("metrics"), metrics: z.array(metricSchema) }),
  z.object({
    kind: z.literal("breakdown"),
    total: z.number(),
    buckets: z.array(bucketSchema),
  }),
  z.object({ kind: z.literal("cases"), cases: z.array(caseSchema) }),
  z.object({ kind: z.literal("documents"), documents: z.array(documentSchema) }),
  z.object({ kind: z.literal("reports"), reports: z.array(reportSchema) }),
  z.object({
    kind: z.literal("conversations"),
    conversations: z.array(conversationSchema),
  }),
  z.object({ kind: z.literal("activity"), activity: z.array(activitySchema) }),
  z.object({
    kind: z.literal("notifications"),
    notifications: z.array(notificationSchema),
  }),
  z.object({ kind: z.literal("actions") }),
]);

export const widgetDescriptorSchema = z.object({
  key: z.string(),
  group: z.enum(WIDGET_GROUPS),
  kind: z.enum(WIDGET_PAYLOAD_KINDS),
  // Loose: the server's event vocabulary grows, and an event this build has never
  // heard of is simply one no widget here reacts to.
  refresh_events: z.array(z.string()),
  refresh_interval_seconds: z.number(),
  platform_wide: z.boolean(),
});

export const widgetSchema = z.object({
  widget: widgetDescriptorSchema,
  state: z.enum(WIDGET_STATES),
  generated_at: z.string(),
  duration_ms: z.number(),
  data: widgetPayloadSchema.nullable(),
  // `catch` rather than a strict enum: a failure code added server-side must
  // still render as "this widget could not be loaded" rather than failing the
  // parse of an otherwise good dashboard.
  error_code: z.enum(WIDGET_ERROR_CODES).nullable().catch("unknown"),
});

export const dashboardSchema = z.object({
  generated_at: z.string(),
  range: z.enum(DASHBOARD_RANGES),
  window_start: z.string(),
  window_end: z.string(),
  window_days: z.number(),
  role: z.string(),
  widgets: z.array(widgetSchema),
  quick_actions: z.array(z.enum(QUICK_ACTION_KEYS)),
  failed_widgets: z.number(),
  duration_ms: z.number(),
});

export const widgetCatalogSchema = z.object({
  role: z.string(),
  widgets: z.array(widgetDescriptorSchema),
  quick_actions: z.array(z.enum(QUICK_ACTION_KEYS)),
});

export const dashboardMetricsSchema = z.object({
  since: z.string(),
  enabled: z.boolean(),

  loads: z.number(),
  refreshes: z.number(),
  widgets_loaded: z.number(),
  widgets_failed: z.number(),
  widget_success_rate: z.number(),

  average_load_ms: z.number().nullable(),
  average_widget_ms: z.number().nullable(),

  active_users: z.number(),
  active_users_capped: z.boolean(),

  average_ms_by_widget: z.record(z.string(), z.number()),
  failures_by_widget: z.record(z.string(), z.number()),
  failures_by_reason: z.record(z.string(), z.number()),
});
