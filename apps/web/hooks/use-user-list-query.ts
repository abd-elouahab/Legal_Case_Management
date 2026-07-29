"use client";

import * as React from "react";

import { userListQuerySchema } from "@/lib/validation/user";
import {
  DEFAULT_USER_LIST_QUERY,
  type SortOrder,
  type UserListQuery,
  type UserSortField,
} from "@/types/user-management";
import type { UserRole, UserStatus } from "@/types/user";

/**
 * The user list's query state: search term, filters, sort, and page.
 *
 * Kept in one hook rather than in the page component so the rule that ties them
 * together lives in exactly one place: **any change other than the page itself
 * resets to page 1**. Without that, typing a search while on page 4 asks for the
 * fourth page of a two-page result and shows an empty table — a bug that is easy
 * to reintroduce if each control sets its own state.
 *
 * The search term is debounced separately from the committed query, so typing
 * does not fire a request per keystroke while the input still updates instantly.
 */

/** Delay before a typed search term is sent, in milliseconds. */
export const SEARCH_DEBOUNCE_MS = 300;

export interface UserListQueryState {
  /** The committed query — what should actually be requested. */
  query: UserListQuery;
  /** The live value of the search input, which may be ahead of `query.search`. */
  searchInput: string;
  setSearch: (value: string) => void;
  setRole: (role: UserRole | null) => void;
  setStatus: (status: UserStatus | null) => void;
  setSort: (sortBy: UserSortField, sortOrder: SortOrder) => void;
  /** Toggle the direction of `column`, or sort by it ascending if it is new. */
  toggleSort: (column: UserSortField) => void;
  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
  reset: () => void;
  /** Whether any search term or filter is applied — drives the empty state copy. */
  isFiltered: boolean;
}

export function useUserListQuery(
  initial: Partial<UserListQuery> = {},
): UserListQueryState {
  const initialQuery = React.useMemo(
    () => userListQuerySchema.parse({ ...DEFAULT_USER_LIST_QUERY, ...initial }),
    // Parsed once: this is a starting point, not a controlled prop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const [query, setQuery] = React.useState<UserListQuery>(initialQuery);
  const [searchInput, setSearchInput] = React.useState(initialQuery.search);

  /** Apply a change and return to the first page, since the result set moved. */
  const amend = React.useCallback((changes: Partial<UserListQuery>) => {
    setQuery((current) => ({ ...current, ...changes, page: 1 }));
  }, []);

  // Debounce the committed search term. The input stays responsive; only the
  // request waits.
  React.useEffect(() => {
    if (searchInput === query.search) return;

    const timer = setTimeout(() => {
      amend({ search: searchInput });
    }, SEARCH_DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [searchInput, query.search, amend]);

  const toggleSort = React.useCallback(
    (column: UserSortField) => {
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
    setQuery({ ...DEFAULT_USER_LIST_QUERY });
  }, []);

  return {
    query,
    searchInput,
    setSearch: setSearchInput,
    setRole: React.useCallback((role: UserRole | null) => amend({ role }), [amend]),
    setStatus: React.useCallback((status: UserStatus | null) => amend({ status }), [amend]),
    setSort: React.useCallback(
      (sortBy: UserSortField, sortOrder: SortOrder) => amend({ sortBy, sortOrder }),
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
    isFiltered: Boolean(query.search || query.role || query.status),
  };
}
