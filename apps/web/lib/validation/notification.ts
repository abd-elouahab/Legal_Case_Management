/**
 * Zod schemas for notifications.
 *
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/notification.py`; where they must agree, the API is the
 * authority.
 *
 * **One field is deliberately a loose string.** `category` is a `VARCHAR` on the
 * server because `16-notifications.md` requires future categories *"without
 * redesign"*, so parsing it as a strict enum here would mean a category added
 * server-side turned somebody's notification feed into a parse error. `type` and
 * `priority` are strict for the opposite reason: both are PostgreSQL enums, so an
 * unrecognised value is a genuine contract break rather than a newer backend
 * being ahead of this build — the same distinction `lib/validation/report.ts`
 * draws between a report status and an error code.
 */

import { z } from "zod";

import {
  ANNOUNCEMENT_KINDS,
  NOTIFICATION_PREFERENCE_KEYS,
  NOTIFICATION_PRIORITIES,
  NOTIFICATION_SORT_FIELDS,
  NOTIFICATION_TYPES,
} from "@/types/notification";

/** Longest announcement the API accepts, matching `NOTIFICATION_ANNOUNCEMENT_MAX_LENGTH`. */
export const MAX_ANNOUNCEMENT_LENGTH = 500;

/** Most notifications one `PATCH /notifications/read` may mark. */
export const MAX_BULK_READ = 200;

// --------------------------------------------------------------------------- //
// Requests
// --------------------------------------------------------------------------- //

/** The list query, validated before it becomes a query string. */
export const notificationListQuerySchema = z.object({
  page: z.coerce.number().int().min(1).catch(1),
  pageSize: z.coerce.number().int().min(1).max(100).catch(20),
  unreadOnly: z.coerce.boolean().catch(false),
  category: z.string().max(50).nullable().catch(null),
  notificationType: z.enum(NOTIFICATION_TYPES).nullable().catch(null),
  priority: z.enum(NOTIFICATION_PRIORITIES).nullable().catch(null),
  caseId: z.string().nullable().catch(null),
  sortBy: z.enum(NOTIFICATION_SORT_FIELDS).catch("created_at"),
  sortOrder: z.enum(["asc", "desc"]).catch("desc"),
  language: z.string().max(10).nullable().catch(null),
});

/**
 * The announcement form.
 *
 * The one place in this feature where a human types the words a notification
 * will carry, so it is the one place a length limit is about input rather than
 * about rendering.
 */
export const announcementSchema = z.object({
  kind: z.enum(ANNOUNCEMENT_KINDS).default("announcement"),
  message: z
    .string()
    .trim()
    .min(1, "Write what you want everyone to see.")
    .max(
      MAX_ANNOUNCEMENT_LENGTH,
      `Keep the announcement under ${MAX_ANNOUNCEMENT_LENGTH} characters.`,
    ),
});

export type AnnouncementValues = z.input<typeof announcementSchema>;

// --------------------------------------------------------------------------- //
// Responses
// --------------------------------------------------------------------------- //

const targetSchema = z.object({
  target_type: z.string(),
  target_id: z.string().nullable(),
});

const actorSchema = z.object({
  id: z.string(),
  full_name: z.string(),
  role: z.string(),
});

/** One notification as the API renders it for this reader. */
export const notificationSchema = z.object({
  id: z.string(),
  // Loose on purpose — see the module note.
  category: z.string(),
  notification_type: z.enum(NOTIFICATION_TYPES),
  priority: z.enum(NOTIFICATION_PRIORITIES),

  title: z.string(),
  message: z.string(),
  language: z.string(),

  event_type: z.string().nullable(),
  rule_key: z.string(),

  case_id: z.string().nullable(),
  actor: actorSchema.nullable(),
  target: targetSchema.nullable(),

  read_at: z.string().nullable(),
  is_read: z.boolean(),
  created_at: z.string(),
});

export const notificationPageSchema = z.object({
  items: z.array(notificationSchema),
  total_records: z.number(),
  unread_count: z.number(),
  unread_count_capped: z.boolean(),
  page: z.number(),
  page_size: z.number(),
  total_pages: z.number(),
});

export const notificationSummarySchema = z.object({
  unread_count: z.number(),
  unread_count_capped: z.boolean(),
  total_count: z.number(),
  unread_by_category: z.record(z.string(), z.number()),
  highest_unread_priority: z.enum(NOTIFICATION_PRIORITIES).nullable(),
});

export const notificationPreferencesSchema = z.object({
  preferences: z.array(
    z.object({
      preference_key: z.enum(NOTIFICATION_PREFERENCE_KEYS),
      in_app: z.boolean(),
      email: z.boolean(),
      is_default: z.boolean(),
    }),
  ),
});

export const announcementResultSchema = z.object({
  recipients: z.number(),
  skipped: z.number(),
  kind: z.enum(ANNOUNCEMENT_KINDS),
});

export const notificationMetricsSchema = z.object({
  since: z.string(),
  enabled: z.boolean(),

  total_notifications: z.number(),
  unread_notifications: z.number(),
  read_notifications: z.number(),
  read_rate: z.number(),
  recipients: z.number(),

  created: z.number(),
  delivered: z.number(),
  failed: z.number(),
  suppressed_by_preference: z.number(),
  deduplicated: z.number(),
  dropped: z.number(),
  pending: z.number(),

  average_delivery_latency_ms: z.number().nullable(),

  notifications_by_category: z.record(z.string(), z.number()),
  created_by_rule: z.record(z.string(), z.number()),
  failures_by_reason: z.record(z.string(), z.number()),

  window_days: z.number().nullable(),
});
