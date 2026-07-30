"use client";

import * as React from "react";

import { documentListQuerySchema } from "@/lib/validation/document";
import type { DocumentCategory } from "@/types/document";
import {
  DEFAULT_DOCUMENT_LIST_QUERY,
  type DocumentListQuery,
  type DocumentSortField,
  type SortOrder,
} from "@/types/document-management";

/**
 * The document list's query state: search term, filters, sort, and page.
 *
 * Kept in one hook rather than in the page component so the rule that ties them
 * together lives in exactly one place: **any change other than the page itself
 * resets to page 1**. Without that, typing a search while on page 4 asks for the
 * fourth page of a two-page result and shows an empty table — a bug that is easy
 * to reintroduce if each control sets its own state.
 *
 * The search box is debounced separately from the committed query, so typing does
 * not fire a request per keystroke while the input still updates instantly.
 */

/** Delay before a typed search term is sent, in milliseconds. */
export const SEARCH_DEBOUNCE_MS = 300;

export interface DocumentListQueryState {
  /** The committed query — what should actually be requested. */
  query: DocumentListQuery;
  /** Live value of the search input, which may be ahead of `query`. */
  searchInput: string;
  setSearch: (value: string) => void;
  setCategory: (category: DocumentCategory | null) => void;
  setUploadedBy: (id: string | null) => void;
  setFileExtension: (extension: string | null) => void;
  setUploadedRange: (from: string, to: string) => void;
  setSort: (sortBy: DocumentSortField, sortOrder: SortOrder) => void;
  /** Toggle the direction of `column`, or sort by it ascending if it is new. */
  toggleSort: (column: DocumentSortField) => void;
  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
  reset: () => void;
  /** Whether any search term or filter is applied — drives the empty state copy. */
  isFiltered: boolean;
}

/**
 * @param initial Starting values. When the list is embedded in a case workspace,
 *   `caseId` is passed here and **pinned**: `reset` restores it rather than
 *   clearing it, because "clear filters" on a case's document list must not
 *   silently widen the view to every case on the platform.
 */
export function useDocumentListQuery(
  initial: Partial<DocumentListQuery> = {},
): DocumentListQueryState {
  const initialQuery = React.useMemo(
    () => documentListQuerySchema.parse({ ...DEFAULT_DOCUMENT_LIST_QUERY, ...initial }),
    // Parsed once: this is a starting point, not a controlled prop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // The case scope is not a user-adjustable filter, so it is held apart from the
  // rest and never cleared.
  const pinnedCaseId = initialQuery.caseId;

  const [query, setQuery] = React.useState<DocumentListQuery>(initialQuery);
  const [searchInput, setSearchInput] = React.useState(initialQuery.search);

  /** Apply a change and return to the first page, since the result set moved. */
  const amend = React.useCallback((changes: Partial<DocumentListQuery>) => {
    setQuery((current) => ({ ...current, ...changes, page: 1 }));
  }, []);

  // Debounce the committed search term. The input stays responsive; only the
  // request waits.
  React.useEffect(() => {
    if (searchInput === query.search) return;

    const timer = setTimeout(() => amend({ search: searchInput }), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchInput, query.search, amend]);

  const toggleSort = React.useCallback(
    (column: DocumentSortField) => {
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
    setSearchInput("");
    setQuery({ ...DEFAULT_DOCUMENT_LIST_QUERY, caseId: pinnedCaseId });
  }, [pinnedCaseId]);

  return {
    query,
    searchInput,
    setSearch: setSearchInput,
    setCategory: React.useCallback(
      (category: DocumentCategory | null) => amend({ category }),
      [amend],
    ),
    setUploadedBy: React.useCallback(
      (uploadedBy: string | null) => amend({ uploadedBy }),
      [amend],
    ),
    setFileExtension: React.useCallback(
      (fileExtension: string | null) => amend({ fileExtension }),
      [amend],
    ),
    setUploadedRange: React.useCallback(
      (uploadedFrom: string, uploadedTo: string) => amend({ uploadedFrom, uploadedTo }),
      [amend],
    ),
    setSort: React.useCallback(
      (sortBy: DocumentSortField, sortOrder: SortOrder) => amend({ sortBy, sortOrder }),
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
        query.category ||
        query.uploadedBy ||
        query.fileExtension ||
        query.uploadedFrom ||
        query.uploadedTo,
    ),
  };
}
