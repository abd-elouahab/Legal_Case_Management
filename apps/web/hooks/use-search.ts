"use client";

import * as React from "react";
import {
  useMutation,
  useQuery,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { fetchSearchMetrics, searchDocuments } from "@/lib/api/search";
import { ApiError, NetworkError } from "@/lib/api/errors";
import {
  EMPTY_SEARCH_FILTERS,
  type SearchFilters,
  type SearchMetrics,
  type SearchQuery,
  type SearchResponse,
} from "@/types/search";

/**
 * Server state for Semantic Search.
 *
 * TanStack Query per `architecture.md`: results are server state, so they are
 * fetched and cached rather than mirrored into a client store.
 *
 * **Search is a mutation here, not a query, and that is deliberate** even though
 * it reads. Three reasons, and the first is the one that decides it:
 *
 * * a query would fire on mount and on every keystroke that changed its key,
 *   sending a request — and a query embedding — for every prefix of what someone
 *   is typing. A search is something a user *submits*;
 * * the request has a body, so it has no URL to key a cache on without
 *   serializing the whole thing;
 * * results are not cached across submissions on purpose. A legal corpus changes
 *   as documents are indexed, and a stale result list showing a passage from a
 *   document that has since been withdrawn is exactly what this feature must not
 *   do.
 *
 * The metrics view *is* a query: it is health, it is cheap, and it is read
 * rather than asked.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the search API.
 */

/** Results per page, matching the API's default. */
export const SEARCH_PAGE_SIZE = 10;

/** Query keys. */
export const searchKeys = {
  all: ["search"] as const,
  metrics: () => [...searchKeys.all, "metrics"] as const,
};

/**
 * Translate a failure into a message safe to show the user.
 *
 * Branches on the API's machine-readable `code` rather than on message text,
 * which is localizable and may change. The two dependency outages carry the
 * server's own message through verbatim, because only the server knows whether
 * the model or the vector database is the one that is down.
 */
export function searchErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "search_disabled":
        return "Semantic search is currently disabled on this platform.";
      case "embedding_unavailable":
      case "vector_store_unavailable":
        return error.message;
      case "search_filter_too_broad":
        return error.message;
      case "invalid_search_query":
        return "Enter a search query of at least two characters.";
      case "case_not_found":
        return "That case no longer exists. Clear the filter and search again.";
      case "document_not_found":
        return "That document no longer exists. Clear the filter and search again.";
      case "forbidden":
        return "You do not have access to the case or document you filtered by.";
      case "invalid_token":
      case "token_expired":
      case "missing_token":
        return "Your session has expired. Sign in again to continue.";
      default:
        return error.message || "Something went wrong. Please try again.";
    }
  }

  return "Something went wrong. Please try again.";
}

/** Whether a failure is a dependency outage rather than anything about the request. */
export function isSearchUnavailable(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    ["search_disabled", "embedding_unavailable", "vector_store_unavailable"].includes(
      error.code,
    )
  );
}

// --------------------------------------------------------------------------- //
// Searching
// --------------------------------------------------------------------------- //

/** What a submitted search asks for. `filters` and paging default to none. */
export interface SearchRequestInput {
  query: string;
  limit?: number;
  offset?: number;
  minScore?: number | null;
  filters?: SearchFilters;
}

function toQuery(input: SearchRequestInput): SearchQuery {
  return {
    query: input.query,
    limit: input.limit ?? SEARCH_PAGE_SIZE,
    offset: input.offset ?? 0,
    minScore: input.minScore ?? null,
    filters: input.filters ?? EMPTY_SEARCH_FILTERS,
  };
}

/**
 * Submit a search.
 *
 * Deliberately **not retried**. A 503 means the embedding model or the vector
 * database is unavailable, and retrying costs another query embedding to fail
 * the same way; a 403 or a 422 will never succeed on a repeat. The user retries
 * by pressing the button, which is also the only signal that they still want the
 * answer.
 */
export function useSearchDocuments(): UseMutationResult<
  SearchResponse,
  unknown,
  SearchRequestInput
> {
  return useMutation({
    mutationFn: (input: SearchRequestInput) => searchDocuments(toQuery(input)),
    retry: false,
  });
}

/**
 * A search together with the paging state a results page needs.
 *
 * Keeps the *submitted* query separate from what is currently in the input, so
 * paging re-runs the search that produced the results on screen rather than
 * whatever has been typed since. Without that split, changing the box and
 * pressing Next silently searches for something else.
 */
export interface SearchSession {
  /** The query behind the results currently shown, or `null` before the first search. */
  submitted: SearchRequestInput | null;
  response: SearchResponse | undefined;
  isSearching: boolean;
  error: unknown;
  page: number;
  run: (input: SearchRequestInput) => void;
  nextPage: () => void;
  previousPage: () => void;
  reset: () => void;
}

export function useSearchSession(): SearchSession {
  const mutation = useSearchDocuments();
  const [submitted, setSubmitted] = React.useState<SearchRequestInput | null>(null);
  const [page, setPage] = React.useState(0);

  const run = React.useCallback(
    (input: SearchRequestInput) => {
      // A new search always starts at the first page: keeping the offset would
      // open a different query on page 4, which reads as "no results".
      setSubmitted(input);
      setPage(0);
      mutation.mutate({ ...input, offset: 0 });
    },
    [mutation],
  );

  const goTo = React.useCallback(
    (next: number) => {
      if (!submitted || next < 0) return;
      const limit = submitted.limit ?? SEARCH_PAGE_SIZE;
      setPage(next);
      mutation.mutate({ ...submitted, offset: next * limit });
    },
    [mutation, submitted],
  );

  const reset = React.useCallback(() => {
    setSubmitted(null);
    setPage(0);
    mutation.reset();
  }, [mutation]);

  return {
    submitted,
    response: mutation.data,
    isSearching: mutation.isPending,
    error: mutation.error,
    page,
    run,
    nextPage: () => goTo(page + 1),
    previousPage: () => goTo(page - 1),
    reset,
  };
}

// --------------------------------------------------------------------------- //
// Monitoring
// --------------------------------------------------------------------------- //

/** Platform-wide retrieval health. Requires `search:monitor`. */
export function useSearchMetrics(
  options: { enabled?: boolean } = {},
): UseQueryResult<SearchMetrics, unknown> {
  return useQuery({
    queryKey: searchKeys.metrics(),
    queryFn: fetchSearchMetrics,
    enabled: options.enabled ?? true,
  });
}
