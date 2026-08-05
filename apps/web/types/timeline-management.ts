/**
 * Timeline DTOs.
 *
 * The request and result shapes of the timeline endpoints, in the app's camelCase
 * domain vocabulary. Mapping to and from the API's snake_case wire format happens
 * once, in `lib/api/timeline.ts`, so nothing above that layer ever sees a wire
 * shape.
 */

import type { SortOrder } from "@/types/case-management";
import type { TimelineEvent } from "@/types/timeline";

export type { SortOrder };

/**
 * Columns the timeline can be ordered by.
 *
 * One, because the spec names one: *Event Date*. Declared as a list rather than
 * hard-coded so a second key is an addition rather than a signature change.
 */
export const TIMELINE_SORT_FIELDS = ["created_at"] as const;
export type TimelineSortField = (typeof TIMELINE_SORT_FIELDS)[number];

/** Default page size, matching `DEFAULT_PAGE_SIZE` in `apps/api/schemas/timeline.py`. */
export const DEFAULT_PAGE_SIZE = 20;

/**
 * The full query state of a case timeline: search, filters, sort, and page.
 *
 * Held as one object so a change to any part can reset the page in a single
 * place — searching while on page 4 would otherwise show an empty result. `null`
 * means "any" for every filter, which is how the Selects express "All".
 *
 * The case is **not** part of this: a timeline is always read for one case, which
 * is a property of the URL rather than a filter a user can widen.
 */
export interface TimelineQuery {
  page: number;
  pageSize: number;
  search: string;
  eventType: string | null;
  actorId: string | null;
  dateFrom: string;
  dateTo: string;
  sortBy: TimelineSortField;
  sortOrder: SortOrder;
}

export const DEFAULT_TIMELINE_QUERY: TimelineQuery = {
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  search: "",
  eventType: null,
  actorId: null,
  dateFrom: "",
  dateTo: "",
  sortBy: "created_at",
  // Newest first: reverse chronological is how a timeline is read, and it is
  // what the spec's display requires.
  sortOrder: "desc",
};

/** One page of a timeline, with the totals needed to render pagination. */
export interface TimelinePage {
  items: TimelineEvent[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}
