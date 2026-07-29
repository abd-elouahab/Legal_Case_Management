"use client";

import * as React from "react";

import { caseListQuerySchema } from "@/lib/validation/case";
import type { CasePriority, CaseStatus } from "@/types/case";
import {
  DEFAULT_CASE_LIST_QUERY,
  type CaseListQuery,
  type CaseSortField,
  type SortOrder,
} from "@/types/case-management";

/**
 * The case list's query state: search term, filters, sort, and page.
 *
 * Kept in one hook rather than in the page component so the rule that ties them
 * together lives in exactly one place: **any change other than the page itself
 * resets to page 1**. Without that, typing a search while on page 4 asks for the
 * fourth page of a two-page result and shows an empty table — a bug that is easy
 * to reintroduce if each control sets its own state.
 *
 * The two free-text inputs are debounced separately from the committed query, so
 * typing does not fire a request per keystroke while the inputs still update
 * instantly.
 */

/** Delay before a typed search term is sent, in milliseconds. */
export const SEARCH_DEBOUNCE_MS = 300;

/** The filters that are typed rather than chosen, and therefore debounced. */
export interface CaseTextFilters {
  search: string;
  courtName: string;
}

export interface CaseListQueryState {
  /** The committed query — what should actually be requested. */
  query: CaseListQuery;
  /** Live values of the typed inputs, which may be ahead of `query`. */
  textInput: CaseTextFilters;
  setSearch: (value: string) => void;
  setCourtName: (value: string) => void;
  setStatus: (status: CaseStatus | null) => void;
  setPriority: (priority: CasePriority | null) => void;
  setAssignedLawyerId: (id: string | null) => void;
  setAssignedCourtRepresentativeId: (id: string | null) => void;
  setFilingDateRange: (from: string, to: string) => void;
  setHearingDateRange: (from: string, to: string) => void;
  setSort: (sortBy: CaseSortField, sortOrder: SortOrder) => void;
  /** Toggle the direction of `column`, or sort by it ascending if it is new. */
  toggleSort: (column: CaseSortField) => void;
  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
  reset: () => void;
  /** Whether any search term or filter is applied — drives the empty state copy. */
  isFiltered: boolean;
}

export function useCaseListQuery(initial: Partial<CaseListQuery> = {}): CaseListQueryState {
  const initialQuery = React.useMemo(
    () => caseListQuerySchema.parse({ ...DEFAULT_CASE_LIST_QUERY, ...initial }),
    // Parsed once: this is a starting point, not a controlled prop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const [query, setQuery] = React.useState<CaseListQuery>(initialQuery);
  const [textInput, setTextInput] = React.useState<CaseTextFilters>({
    search: initialQuery.search,
    courtName: initialQuery.courtName,
  });

  /** Apply a change and return to the first page, since the result set moved. */
  const amend = React.useCallback((changes: Partial<CaseListQuery>) => {
    setQuery((current) => ({ ...current, ...changes, page: 1 }));
  }, []);

  // Debounce the committed text filters. The inputs stay responsive; only the
  // request waits.
  React.useEffect(() => {
    if (textInput.search === query.search && textInput.courtName === query.courtName) return;

    const timer = setTimeout(() => {
      amend({ search: textInput.search, courtName: textInput.courtName });
    }, SEARCH_DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [textInput, query.search, query.courtName, amend]);

  const toggleSort = React.useCallback(
    (column: CaseSortField) => {
      amend({
        sortBy: column,
        // Re-selecting the current column flips direction; a new column starts
        // ascending, which is the conventional first click.
        sortOrder: query.sortBy === column && query.sortOrder === "asc" ? "desc" : "asc",
      });
    },
    [amend, query.sortBy, query.sortOrder],
  );

  const reset = React.useCallback(() => {
    setTextInput({ search: "", courtName: "" });
    setQuery({ ...DEFAULT_CASE_LIST_QUERY });
  }, []);

  return {
    query,
    textInput,
    setSearch: React.useCallback(
      (value: string) => setTextInput((current) => ({ ...current, search: value })),
      [],
    ),
    setCourtName: React.useCallback(
      (value: string) => setTextInput((current) => ({ ...current, courtName: value })),
      [],
    ),
    setStatus: React.useCallback((status: CaseStatus | null) => amend({ status }), [amend]),
    setPriority: React.useCallback(
      (priority: CasePriority | null) => amend({ priority }),
      [amend],
    ),
    setAssignedLawyerId: React.useCallback(
      (assignedLawyerId: string | null) => amend({ assignedLawyerId }),
      [amend],
    ),
    setAssignedCourtRepresentativeId: React.useCallback(
      (assignedCourtRepresentativeId: string | null) =>
        amend({ assignedCourtRepresentativeId }),
      [amend],
    ),
    setFilingDateRange: React.useCallback(
      (filingDateFrom: string, filingDateTo: string) =>
        amend({ filingDateFrom, filingDateTo }),
      [amend],
    ),
    setHearingDateRange: React.useCallback(
      (hearingDateFrom: string, hearingDateTo: string) =>
        amend({ hearingDateFrom, hearingDateTo }),
      [amend],
    ),
    setSort: React.useCallback(
      (sortBy: CaseSortField, sortOrder: SortOrder) => amend({ sortBy, sortOrder }),
      [amend],
    ),
    toggleSort,
    // The page is the one change that must *not* reset the page.
    setPage: React.useCallback(
      (page: number) => setQuery((current) => ({ ...current, page: Math.max(1, page) })),
      [],
    ),
    setPageSize: React.useCallback((pageSize: number) => amend({ pageSize }), [amend]),
    reset,
    isFiltered: Boolean(
      query.search ||
        query.courtName ||
        query.status ||
        query.priority ||
        query.assignedLawyerId ||
        query.assignedCourtRepresentativeId ||
        query.filingDateFrom ||
        query.filingDateTo ||
        query.hearingDateFrom ||
        query.hearingDateTo,
    ),
  };
}
