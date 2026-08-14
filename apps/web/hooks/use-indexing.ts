"use client";

import * as React from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { timelineKeys } from "@/hooks/use-timeline";
import {
  fetchDocumentIndex,
  fetchDocumentIndexes,
  fetchDocumentIndexHistory,
  fetchIndexMetrics,
  reindexDocument,
} from "@/lib/api/indexing";
import { ApiError, NetworkError } from "@/lib/api/errors";
import { useErrorMessage, type ErrorCodeMap } from "@/hooks/use-error-message";
import type { DocumentIndex, IndexMetrics } from "@/types/indexing";
import type { DocumentIndexPage, IndexListQuery } from "@/types/indexing-management";

/**
 * Server state for Document Indexing.
 *
 * TanStack Query per `architecture.md`: indexing runs are server state, so they
 * are cached and polled rather than mirrored into a client store.
 *
 * Like OCR, this module **polls**, and it has to: an index is built on a
 * background worker, so a run that is `pending` when a panel renders becomes
 * `indexed` with nothing on the client causing it. The polling rule is derived
 * from the server's own `isActive` flag rather than from a second copy of "which
 * statuses are still running" — see {@link activePollInterval}.
 *
 * The interval is deliberately longer than OCR's. Embedding a document takes
 * tens of seconds to minutes where extraction takes seconds, so polling it every
 * three seconds would be dozens of requests that each say "still working".
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the indexing API.
 */

/** How often an in-flight run is re-checked, in milliseconds. */
export const INDEX_POLL_INTERVAL_MS = 5_000;

/**
 * Query keys.
 *
 * Keyed by document *and version*, because each version keeps its own index: a
 * replacement must not read back the previous version's state from cache.
 */
export const indexingKeys = {
  all: ["indexing"] as const,
  indexes: () => [...indexingKeys.all, "index"] as const,
  index: (documentId: string, version?: number) =>
    [...indexingKeys.indexes(), documentId, version ?? "current"] as const,
  histories: () => [...indexingKeys.all, "history"] as const,
  history: (documentId: string) => [...indexingKeys.histories(), documentId] as const,
  lists: () => [...indexingKeys.all, "list"] as const,
  list: (query: IndexListQuery) => [...indexingKeys.lists(), query] as const,
  metrics: (windowDays?: number) =>
    [...indexingKeys.all, "metrics", windowDays ?? "all"] as const,
};

/**
 * Translate a failure into a sentence in the reader's language.
 *
 * Branches on the API's machine-readable `code` rather than on message text —
 * which the server writes in English, with no knowledge of who is reading it.
 * `hooks/use-error-message.ts` records why that matters; the short version is
 * that an interface which is Arabic everywhere except when something goes wrong
 * is not localized. Codes with no entry here fall through to the shared
 * `errors.*` sentences and then to a generic one.
 */
const INDEXING_ERRORS: ErrorCodeMap = {
  document_index_not_found: "notIndexed",
  indexing_not_ready: "notReady",
  indexing_already_running: "alreadyRunning",
  indexing_disabled: "disabled",
  document_not_found: "documentNotFound",
  document_version_not_found: "versionNotFound",
  missing_token: "sessionExpired",
};

export function useIndexingErrorMessage(): (error: unknown) => string {
  return useErrorMessage("indexing.errors", INDEXING_ERRORS);
}

/** Whether a failure is the API saying "this document has no index record". */
export function isDocumentIndexMissing(error: unknown): boolean {
  return error instanceof ApiError && error.code === "document_index_not_found";
}

/**
 * How often to re-check a run, given what the last response said.
 *
 * `false` stops the polling. The decision reads the server's `isActive` flag
 * rather than testing the status against a client-side list of "still running"
 * states — the same reasoning as OCR's: the rule lives on the API, and a second
 * copy here would be the one that kept polling forever after a status was
 * renamed.
 */
function activePollInterval(index: DocumentIndex | undefined): number | false {
  if (!index) return false;
  return index.isActive ? INDEX_POLL_INTERVAL_MS : false;
}

// --------------------------------------------------------------------------- //
// Queries
// --------------------------------------------------------------------------- //

/**
 * A document's index status, polled while the run is in flight.
 *
 * A 404 is a legitimate answer — a document whose text has not been extracted has
 * no index — so callers should recognise it with {@link isDocumentIndexMissing}
 * and render "not indexed" rather than an error. Retrying a 404 would poll an
 * endpoint that can only ever answer the same way, so it is not retried.
 */
export function useDocumentIndex(
  documentId: string,
  options: { enabled?: boolean; version?: number } = {},
): UseQueryResult<DocumentIndex, unknown> {
  return useQuery({
    queryKey: indexingKeys.index(documentId, options.version),
    queryFn: () => fetchDocumentIndex(documentId, options.version),
    enabled: (options.enabled ?? true) && Boolean(documentId),
    refetchInterval: (query) => activePollInterval(query.state.data),
    retry: (failureCount, error) => !isDocumentIndexMissing(error) && failureCount < 2,
  });
}

/** Every index recorded for a document, oldest version first. */
export function useDocumentIndexHistory(
  documentId: string,
  options: { enabled?: boolean } = {},
): UseQueryResult<DocumentIndex[], unknown> {
  return useQuery({
    queryKey: indexingKeys.history(documentId),
    queryFn: () => fetchDocumentIndexHistory(documentId),
    enabled: (options.enabled ?? true) && Boolean(documentId),
  });
}

/**
 * One page of the indexing-run list.
 *
 * `placeholderData` keeps the previous page on screen while the next one loads,
 * so paging or filtering does not blank the table out and shift the layout.
 */
export function useDocumentIndexes(
  query: IndexListQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<DocumentIndexPage, unknown> {
  return useQuery({
    queryKey: indexingKeys.list(query),
    queryFn: () => fetchDocumentIndexes(query),
    enabled: options.enabled ?? true,
    placeholderData: (previous) => previous,
  });
}

/** Platform-wide indexing health. Requires `indexing:monitor`. */
export function useIndexMetrics(
  options: { enabled?: boolean; windowDays?: number } = {},
): UseQueryResult<IndexMetrics, unknown> {
  return useQuery({
    queryKey: indexingKeys.metrics(options.windowDays),
    queryFn: () => fetchIndexMetrics(options.windowDays),
    enabled: options.enabled ?? true,
  });
}

// --------------------------------------------------------------------------- //
// Mutations
// --------------------------------------------------------------------------- //

/** What a rebuild request identifies. */
export interface ReindexRequest {
  documentId: string;
  version?: number;
}

/**
 * Queue a fresh index for a document whose text is already extracted.
 *
 * On success the returned `pending` run is written straight into the status
 * cache, which is what restarts the poll immediately rather than after the next
 * interval. The timeline is invalidated too, because the rebuild appends an
 * `indexing_retried` event to the case's history — and the run that follows
 * appends two more, which the poll's own invalidation picks up.
 *
 * The **document and OCR caches are deliberately left alone**: a rebuild changes
 * nothing about the document, its metadata, its stored file, or its extracted
 * text.
 */
export function useReindexDocument(): UseMutationResult<
  DocumentIndex,
  unknown,
  ReindexRequest
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ documentId, version }: ReindexRequest) =>
      reindexDocument(documentId, version),
    onSuccess: async (index, { version }) => {
      queryClient.setQueryData(indexingKeys.index(index.documentId, version), index);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: indexingKeys.history(index.documentId),
        }),
        queryClient.invalidateQueries({ queryKey: indexingKeys.lists() }),
        queryClient.invalidateQueries({ queryKey: timelineKeys.all }),
      ]);
    },
  });
}

/**
 * Invalidate the timeline and the history when a run finishes.
 *
 * A completed run appends `indexing_started` and `indexing_completed` to the
 * case's history — neither of which the client caused, so nothing would otherwise
 * invalidate it. Watching the status the poll is already fetching is what turns
 * "the badge went green" into "the activity list caught up too".
 *
 * Keyed on the run's identifier *and* its status, so it fires once per
 * transition rather than on every poll tick.
 */
export function useIndexCompletionSync(index: DocumentIndex | undefined): void {
  const queryClient = useQueryClient();
  const previous = React.useRef<string | null>(null);

  const signature = index ? `${index.id}:${index.status}` : null;

  React.useEffect(() => {
    if (!index || !signature) return;
    if (previous.current === signature) return;

    const settled = previous.current !== null && index.isTerminal;
    previous.current = signature;
    if (!settled) return;

    void Promise.all([
      queryClient.invalidateQueries({ queryKey: indexingKeys.history(index.documentId) }),
      queryClient.invalidateQueries({ queryKey: indexingKeys.lists() }),
      queryClient.invalidateQueries({ queryKey: timelineKeys.all }),
    ]);
  }, [queryClient, index, signature]);
}
