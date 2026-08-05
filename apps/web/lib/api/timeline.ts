/**
 * Timeline API calls.
 *
 * Thin, typed wrappers over the two timeline endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape.
 *
 * Read-only, like the API: there is no create, update, or delete here because
 * there is nothing to call.
 */

import { apiRequest } from "@/lib/api/client";
import { TIMELINE_ENDPOINTS } from "@/lib/api/config";
import { timelineEventSchema, timelinePageSchema } from "@/lib/validation/timeline";
import type { TimelineEvent } from "@/types/timeline";
import type { TimelinePage, TimelineQuery } from "@/types/timeline-management";

type TimelineEventWire = ReturnType<typeof timelineEventSchema.parse>;

/** Map one API event record onto the app's {@link TimelineEvent}. */
function toTimelineEvent(payload: TimelineEventWire): TimelineEvent {
  return {
    id: payload.id,
    caseId: payload.case_id,
    eventType: payload.event_type,
    category: payload.category,
    title: payload.title,
    description: payload.description,
    actorId: payload.actor_id,
    actorName: payload.actor_name,
    actorRole: payload.actor_role,
    metadata: payload.metadata,
    createdAt: payload.created_at,
  };
}

/**
 * Build the query string for a timeline request.
 *
 * Empty search terms, blank dates, and "any" filters are omitted rather than sent
 * as blanks, so the request URL reflects what is actually being asked — which also
 * makes the query a stable cache key for TanStack Query.
 */
export function buildTimelineParams(query: TimelineQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
    sort_by: query.sortBy,
    sort_order: query.sortOrder,
  });

  const optional: Array<[string, string | null]> = [
    ["search", query.search.trim() || null],
    ["event_type", query.eventType],
    ["actor_id", query.actorId],
    ["date_from", query.dateFrom || null],
    ["date_to", query.dateTo || null],
  ];

  for (const [key, value] of optional) {
    if (value) params.set(key, value);
  }

  return params.toString();
}

/** Fetch one page of a case's timeline. */
export async function fetchCaseTimeline(
  caseId: string,
  query: TimelineQuery,
): Promise<TimelinePage> {
  const raw = await apiRequest<unknown>(
    `${TIMELINE_ENDPOINTS.caseTimeline(caseId)}?${buildTimelineParams(query)}`,
  );
  const data = timelinePageSchema.parse(raw);

  return {
    items: data.items.map(toTimelineEvent),
    totalRecords: data.total_records,
    page: data.page,
    pageSize: data.page_size,
    totalPages: data.total_pages,
  };
}

/** Fetch one timeline event by identifier. */
export async function fetchTimelineEvent(eventId: string): Promise<TimelineEvent> {
  const raw = await apiRequest<unknown>(TIMELINE_ENDPOINTS.detail(eventId));
  return toTimelineEvent(timelineEventSchema.parse(raw));
}
