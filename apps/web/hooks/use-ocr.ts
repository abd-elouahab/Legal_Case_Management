"use client";

import * as React from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { documentKeys } from "@/hooks/use-documents";
import { timelineKeys } from "@/hooks/use-timeline";
import {
  fetchOcrHistory,
  fetchOcrMetrics,
  fetchOcrResult,
  fetchOcrResults,
  fetchOcrText,
  retryOcr,
} from "@/lib/api/ocr";
import { ApiError, NetworkError } from "@/lib/api/errors";
import { useErrorMessage, type ErrorCodeMap } from "@/hooks/use-error-message";
import type { OcrMetrics, OcrResult, OcrText } from "@/types/ocr";
import type { OcrListQuery, OcrResultPage } from "@/types/ocr-management";

/**
 * Server state for OCR Processing.
 *
 * TanStack Query per `architecture.md`: extraction runs are server state, so they
 * are cached and polled rather than mirrored into a client store.
 *
 * The one thing this module does that the other feature hooks do not is **poll**,
 * and it has to: extraction happens on a background worker, so a run that is
 * `pending` when a page renders becomes `completed` with nothing on the client
 * causing it. The polling rule is derived from the server's own `isActive` flag
 * rather than from a second copy of "which statuses are still running" — see
 * {@link activePollInterval}.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the OCR API.
 */

/** How often an in-flight run is re-checked, in milliseconds. */
export const OCR_POLL_INTERVAL_MS = 3_000;

/**
 * Query keys.
 *
 * Keyed by document *and version*, because each version keeps its own run: a
 * replacement must not read back the previous version's text from cache.
 */
export const ocrKeys = {
  all: ["ocr"] as const,
  results: () => [...ocrKeys.all, "result"] as const,
  result: (documentId: string, version?: number) =>
    [...ocrKeys.results(), documentId, version ?? "current"] as const,
  texts: () => [...ocrKeys.all, "text"] as const,
  text: (documentId: string, version?: number) =>
    [...ocrKeys.texts(), documentId, version ?? "current"] as const,
  histories: () => [...ocrKeys.all, "history"] as const,
  history: (documentId: string) => [...ocrKeys.histories(), documentId] as const,
  lists: () => [...ocrKeys.all, "list"] as const,
  list: (query: OcrListQuery) => [...ocrKeys.lists(), query] as const,
  metrics: (windowDays?: number) => [...ocrKeys.all, "metrics", windowDays ?? "all"] as const,
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
const OCR_ERRORS: ErrorCodeMap = {
  ocr_result_not_found: "notProcessed",
  ocr_already_running: "alreadyRunning",
  ocr_unsupported_format: "unsupportedFormat",
  ocr_disabled: "disabled",
  document_not_found: "documentNotFound",
  document_version_not_found: "versionNotFound",
  missing_token: "sessionExpired",
};

export function useOcrErrorMessage(): (error: unknown) => string {
  return useErrorMessage("ocr.errors", OCR_ERRORS);
}

/** Whether a failure is the API saying "this document has no extraction record". */
export function isOcrResultMissing(error: unknown): boolean {
  return error instanceof ApiError && error.code === "ocr_result_not_found";
}

/**
 * How often to re-check a run, given what the last response said.
 *
 * `false` stops the polling. The decision reads the server's `isActive` flag
 * rather than testing the status against a client-side list of "still running"
 * states — the same reasoning as `isPreviewable` on a document: the rule lives on
 * the API, and a second copy here would be the one that kept polling forever
 * after a status was renamed.
 */
function activePollInterval(result: OcrResult | undefined): number | false {
  if (!result) return false;
  return result.isActive ? OCR_POLL_INTERVAL_MS : false;
}

// --------------------------------------------------------------------------- //
// Queries
// --------------------------------------------------------------------------- //

/**
 * A document's extraction status, polled while the run is in flight.
 *
 * A 404 is a legitimate answer — a Word file will never have a run — so callers
 * should recognise it with {@link isOcrResultMissing} and render "not applicable"
 * rather than an error. Retrying a 404 would poll an endpoint that can only ever
 * answer the same way, so it is not retried.
 */
export function useOcrResult(
  documentId: string,
  options: { enabled?: boolean; version?: number } = {},
): UseQueryResult<OcrResult, unknown> {
  return useQuery({
    queryKey: ocrKeys.result(documentId, options.version),
    queryFn: () => fetchOcrResult(documentId, options.version),
    enabled: (options.enabled ?? true) && Boolean(documentId),
    refetchInterval: (query) => activePollInterval(query.state.data),
    retry: (failureCount, error) => !isOcrResultMissing(error) && failureCount < 2,
  });
}

/**
 * A document's extracted text.
 *
 * Fetched separately from the status, mirroring the API: the status is what a
 * panel polls, and the text is what it loads once — usually only when the reader
 * opens it. Callers pass `enabled: false` until then.
 */
export function useOcrText(
  documentId: string,
  options: { enabled?: boolean; version?: number } = {},
): UseQueryResult<OcrText, unknown> {
  return useQuery({
    queryKey: ocrKeys.text(documentId, options.version),
    queryFn: () => fetchOcrText(documentId, options.version),
    enabled: (options.enabled ?? true) && Boolean(documentId),
    retry: (failureCount, error) => !isOcrResultMissing(error) && failureCount < 2,
  });
}

/** Every extraction recorded for a document, oldest version first. */
export function useOcrHistory(
  documentId: string,
  options: { enabled?: boolean } = {},
): UseQueryResult<OcrResult[], unknown> {
  return useQuery({
    queryKey: ocrKeys.history(documentId),
    queryFn: () => fetchOcrHistory(documentId),
    enabled: (options.enabled ?? true) && Boolean(documentId),
  });
}

/**
 * One page of the extraction-run list.
 *
 * `placeholderData` keeps the previous page on screen while the next one loads,
 * so paging or filtering does not blank the table out and shift the layout.
 */
export function useOcrResults(
  query: OcrListQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<OcrResultPage, unknown> {
  return useQuery({
    queryKey: ocrKeys.list(query),
    queryFn: () => fetchOcrResults(query),
    enabled: options.enabled ?? true,
    placeholderData: (previous) => previous,
  });
}

/** Platform-wide extraction health. Requires `ocr:monitor`. */
export function useOcrMetrics(
  options: { enabled?: boolean; windowDays?: number } = {},
): UseQueryResult<OcrMetrics, unknown> {
  return useQuery({
    queryKey: ocrKeys.metrics(options.windowDays),
    queryFn: () => fetchOcrMetrics(options.windowDays),
    enabled: options.enabled ?? true,
  });
}

// --------------------------------------------------------------------------- //
// Mutations
// --------------------------------------------------------------------------- //

/** What a retry request identifies. */
export interface OcrRetryRequest {
  documentId: string;
  version?: number;
}

/**
 * Queue a fresh extraction for a document already uploaded.
 *
 * On success the returned `pending` run is written straight into the status
 * cache, which is what restarts the poll immediately rather than after the next
 * interval. The timeline is invalidated too, because the retry appends an
 * `ocr_retried` event to the case's history — and the run that follows appends
 * two more, which the poll's own invalidation picks up.
 *
 * The **document cache is deliberately left alone**: a retry changes nothing
 * about the document, its metadata, or its stored file.
 */
export function useRetryOcr(): UseMutationResult<OcrResult, unknown, OcrRetryRequest> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ documentId, version }: OcrRetryRequest) => retryOcr(documentId, version),
    onSuccess: async (result, { version }) => {
      queryClient.setQueryData(ocrKeys.result(result.documentId, version), result);
      await Promise.all([
        // The previous attempt's pages are gone; whatever a reader has open is
        // now stale.
        queryClient.invalidateQueries({ queryKey: ocrKeys.text(result.documentId, version) }),
        queryClient.invalidateQueries({ queryKey: ocrKeys.history(result.documentId) }),
        queryClient.invalidateQueries({ queryKey: ocrKeys.lists() }),
        queryClient.invalidateQueries({ queryKey: timelineKeys.all }),
      ]);
    },
  });
}

/**
 * Invalidate the timeline and the extracted text when a run finishes.
 *
 * A completed run appends `ocr_started` and `ocr_completed` to the case's
 * history and produces the pages the text endpoint serves — none of which the
 * client caused, so nothing would otherwise invalidate either. Watching the
 * status the poll is already fetching is what turns "the badge went green" into
 * "the text and the activity list caught up too".
 *
 * Keyed on the run's identifier *and* its status, so it fires once per
 * transition rather than on every poll tick.
 */
export function useOcrCompletionSync(result: OcrResult | undefined): void {
  const queryClient = useQueryClient();
  const previous = React.useRef<string | null>(null);

  const signature = result ? `${result.id}:${result.status}` : null;

  React.useEffect(() => {
    if (!result || !signature) return;
    if (previous.current === signature) return;

    const settled = previous.current !== null && result.isTerminal;
    previous.current = signature;
    if (!settled) return;

    void Promise.all([
      queryClient.invalidateQueries({
        queryKey: ocrKeys.text(result.documentId, result.documentVersion),
      }),
      queryClient.invalidateQueries({ queryKey: ocrKeys.text(result.documentId) }),
      queryClient.invalidateQueries({ queryKey: ocrKeys.history(result.documentId) }),
      queryClient.invalidateQueries({ queryKey: timelineKeys.all }),
      // The document list shows nothing about extraction, but a case workspace
      // rendering both should reconcile on the same tick.
      queryClient.invalidateQueries({ queryKey: documentKeys.details() }),
    ]);
  }, [queryClient, result, signature]);
}
