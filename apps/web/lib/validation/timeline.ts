/**
 * Zod schemas for the Timeline.
 *
 * API responses are external input too, so they are parsed before entering
 * application state — a backend change that alters the payload fails here,
 * loudly, instead of surfacing as `undefined` in an entry.
 *
 * There are **no form schemas**: the timeline is read-only. Events are published
 * by the services that cause them, and there is no endpoint through which a
 * client could write one.
 *
 * The rules deliberately mirror `apps/api/schemas/timeline.py`. Note where they
 * are *tolerant* rather than strict — `event_type` is a plain string and
 * `metadata` an open record — because the API's registry is an open set and a
 * strict enum here would turn a valid response from a newer backend into a client
 * error. `category` is the exception: the server computes it and can only ever
 * send one of five values.
 */

import { z } from "zod";

import { SORT_ORDERS } from "@/types/case-management";
import { TIMELINE_CATEGORIES } from "@/types/timeline";
import { TIMELINE_SORT_FIELDS } from "@/types/timeline-management";

/** Matches `MAX_EVENT_TYPE_LENGTH` in `apps/api/schemas/timeline.py`. */
export const MAX_EVENT_TYPE_LENGTH = 60;

/** A calendar date as `<input type="date">` produces it, and as the API expects it. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

// --------------------------------------------------------------------------- //
// Response schemas
// --------------------------------------------------------------------------- //

export const timelineEventSchema = z.object({
  id: z.string(),
  case_id: z.string(),

  event_type: z.string(),
  // Computed server-side from the event type, and defaulted there for anything
  // unrecognised — so an unknown value here would mean the *contract* changed,
  // not that a new event type shipped.
  category: z.enum(TIMELINE_CATEGORIES).catch("case"),

  title: z.string(),
  description: z.string().nullable().default(null),

  actor_id: z.string().nullable().default(null),
  actor_name: z.string().nullable().default(null),
  actor_role: z.string().nullable().default(null),

  // Open by design: the shape depends on the event type, and a future module
  // attaches its own keys without a client release.
  metadata: z.record(z.string(), z.unknown()).default({}),

  created_at: z.string(),
});

export const timelinePageSchema = z.object({
  items: z.array(timelineEventSchema),
  total_records: z.number().int().nonnegative(),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total_pages: z.number().int().nonnegative(),
});

// --------------------------------------------------------------------------- //
// Query validation
// --------------------------------------------------------------------------- //

/**
 * The timeline query, validated before it becomes a query string.
 *
 * The values come from UI state rather than from a user typing a URL, but they
 * are still the input to a request — and validating here means an out-of-range
 * page from a stale link is corrected rather than sent to the API to be rejected.
 */
export const timelineQuerySchema = z.object({
  page: z.coerce.number().int().min(1).catch(1),
  pageSize: z.coerce.number().int().min(1).max(100).catch(20),
  search: z.string().trim().max(200).catch(""),
  eventType: z.string().trim().max(MAX_EVENT_TYPE_LENGTH).nullable().catch(null),
  actorId: z.string().nullable().catch(null),
  dateFrom: z.string().regex(ISO_DATE).or(z.literal("")).catch(""),
  dateTo: z.string().regex(ISO_DATE).or(z.literal("")).catch(""),
  sortBy: z.enum(TIMELINE_SORT_FIELDS).catch("created_at"),
  sortOrder: z.enum(SORT_ORDERS).catch("desc"),
});
