"use client";

import * as React from "react";

import { timelineQuerySchema } from "@/lib/validation/timeline";
import {
  DEFAULT_TIMELINE_QUERY,
  type SortOrder,
  type TimelineQuery,
} from "@/types/timeline-management";

/**
 * A case timeline's query state: search term, filters, sort, and page.
 *
 * Kept in one hook rather than in the component so the rule that ties them
 * together lives in exactly one place: **any change other than the page itself
 * resets to page 1**. Without that, typing a search while on page 4 asks for the
 * fourth page of a two-page result and shows an empty list.
 *
 * The search box is debounced separately from the committed query, so typing does
 * not fire a request per keystroke while the input still updates instantly.
 *
 * Deliberately parallel to `use-document-list-query.ts` rather than shared with
 * it: the two carry different filters, and a generic "list query" hook would take
 * a shape parameter that every caller then has to spell out — more machinery, not
 * less. What they *do* share is this rule, stated the same way in both.
 */

/** Delay before a typed search term is sent, in milliseconds. */
export const SEARCH_DEBOUNCE_MS = 300;

export interface TimelineQueryState {
  /** The committed query — what should actually be requested. */
  query: TimelineQuery;
  /** Live value of the search input, which may be ahead of `query`. */
  searchInput: string;
  setSearch: (value: string) => void;
  setEventType: (eventType: string | null) => void;
  setActor: (actorId: string | null) => void;
  setDateRange: (from: string, to: string) => void;
  /** Flip between newest-first and oldest-first. */
  toggleSortOrder: () => void;
  setSortOrder: (order: SortOrder) => void;
  setPage: (page: number) => void;
  reset: () => void;
  /** Whether any search term or filter is applied — drives the empty state copy. */
  isFiltered: boolean;
}

export function useTimelineQuery(initial: Partial<TimelineQuery> = {}): TimelineQueryState {
  const initialQuery = React.useMemo(
    () => timelineQuerySchema.parse({ ...DEFAULT_TIMELINE_QUERY, ...initial }),
    // Parsed once: this is a starting point, not a controlled prop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const [query, setQuery] = React.useState<TimelineQuery>(initialQuery);
  const [searchInput, setSearchInput] = React.useState(initialQuery.search);

  /** Apply a change and return to the first page, since the result set moved. */
  const amend = React.useCallback((changes: Partial<TimelineQuery>) => {
    setQuery((current) => ({ ...current, ...changes, page: 1 }));
  }, []);

  // Debounce the committed search term. The input stays responsive; only the
  // request waits.
  React.useEffect(() => {
    if (searchInput === query.search) return;

    const timer = setTimeout(() => amend({ search: searchInput }), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchInput, query.search, amend]);

  const reset = React.useCallback(() => {
    setSearchInput("");
    setQuery(DEFAULT_TIMELINE_QUERY);
  }, []);

  return {
    query,
    searchInput,
    setSearch: setSearchInput,
    setEventType: React.useCallback(
      (eventType: string | null) => amend({ eventType }),
      [amend],
    ),
    setActor: React.useCallback((actorId: string | null) => amend({ actorId }), [amend]),
    setDateRange: React.useCallback(
      (dateFrom: string, dateTo: string) => amend({ dateFrom, dateTo }),
      [amend],
    ),
    toggleSortOrder: React.useCallback(
      // Changing the direction re-orders the whole result, so page 1 is the only
      // page that still means anything.
      () => amend({ sortOrder: query.sortOrder === "desc" ? "asc" : "desc" }),
      [amend, query.sortOrder],
    ),
    setSortOrder: React.useCallback((sortOrder: SortOrder) => amend({ sortOrder }), [amend]),
    // The page is the one change that must *not* reset the page.
    setPage: React.useCallback(
      (page: number) => setQuery((current) => ({ ...current, page: Math.max(1, page) })),
      [],
    ),
    reset,
    isFiltered: Boolean(
      query.search || query.eventType || query.actorId || query.dateFrom || query.dateTo,
    ),
  };
}
