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
import { ApiError, NetworkError } from "@/lib/api/errors";
import { useErrorMessage, type ErrorCodeMap } from "@/hooks/use-error-message";
import {
  deleteReport,
  exportReportFile,
  fetchReport,
  fetchReportMetrics,
  fetchReports,
  fetchReportTemplates,
  generateReport,
  regenerateReport,
} from "@/lib/api/reports";
import { saveFile } from "@/lib/api/upload";
import type {
  Report,
  ReportDetail,
  ReportFormat,
  ReportMetrics,
  ReportTemplate,
} from "@/types/report";
import type {
  GenerateReportRequest,
  ReportListQuery,
  ReportPage,
} from "@/types/report-management";

/**
 * Server state for AI report generation.
 *
 * TanStack Query per `architecture.md`: a report is server state, so it is cached
 * and polled rather than mirrored into a client store.
 *
 * Like OCR and indexing, this module **polls**, and it has to: a report is
 * produced on a background worker, so a run that is `pending` when a page renders
 * becomes `completed` with nothing on the client causing it. The polling rule is
 * derived from the server's own `isActive` flag rather than from a second copy of
 * "which statuses are still running" — see {@link activePollInterval}.
 *
 * The interval sits between OCR's and indexing's, and the reason is what a report
 * *is*: a section is a retrieval plus a model call, so progress moves in visible
 * steps of a few seconds each. Polling every three seconds would mostly report
 * the same section; every ten would make a finished report feel late.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the report API.
 */

/** How often an in-flight run is re-checked, in milliseconds. */
export const REPORT_POLL_INTERVAL_MS = 4_000;

/** Query keys. */
export const reportKeys = {
  all: ["reports"] as const,
  lists: () => [...reportKeys.all, "list"] as const,
  list: (query: ReportListQuery) => [...reportKeys.lists(), query] as const,
  details: () => [...reportKeys.all, "detail"] as const,
  detail: (id: string) => [...reportKeys.details(), id] as const,
  templates: (language?: string) =>
    [...reportKeys.all, "templates", language ?? "default"] as const,
  metrics: (windowDays?: number) =>
    [...reportKeys.all, "metrics", windowDays ?? "all"] as const,
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
const REPORT_ERRORS: ErrorCodeMap = {
  report_not_found: "notFound",
  report_not_ready: "notReady",
  too_many_active_reports: "tooManyActive",
  report_export_unavailable: "exportUnavailable",
  report_already_running: "alreadyRunning",
  reports_disabled: "disabled",
  case_not_found: "caseNotFound",
  forbidden: "noGenerateAccess",
  missing_token: "sessionExpired",
};

export function useReportErrorMessage(): (error: unknown) => string {
  return useErrorMessage("reports.errors", REPORT_ERRORS);
}

/** Whether a failure is the API saying "no such report belongs to you". */
export function isReportMissing(error: unknown): boolean {
  return error instanceof ApiError && error.code === "report_not_found";
}

/**
 * How often to re-check a run, given what the last response said.
 *
 * `false` stops the polling. The decision reads the server's `isActive` flag
 * rather than testing the status against a client-side list of "still running"
 * states — the same reasoning OCR and indexing record: the rule lives on the API,
 * and a second copy here would be the one that kept polling forever after a
 * status was renamed.
 */
function activePollInterval(report: Report | undefined): number | false {
  if (!report) return false;
  return report.isActive ? REPORT_POLL_INTERVAL_MS : false;
}

// --------------------------------------------------------------------------- //
// Queries
// --------------------------------------------------------------------------- //

/**
 * One page of the caller's report history.
 *
 * Polled **only while something on the page is still running**, which is the
 * cheap half of keeping a list live: a history of finished reports changes when
 * the user changes it, and polling it would be a request every few seconds to be
 * told nothing happened.
 *
 * `placeholderData` keeps the previous page on screen while the next one loads,
 * so paging or filtering does not blank the table out and shift the layout.
 */
export function useReports(
  query: ReportListQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<ReportPage, unknown> {
  return useQuery({
    queryKey: reportKeys.list(query),
    queryFn: () => fetchReports(query),
    enabled: options.enabled ?? true,
    placeholderData: (previous) => previous,
    refetchInterval: (result) =>
      result.state.data?.items.some((report) => report.isActive)
        ? REPORT_POLL_INTERVAL_MS
        : false,
  });
}

/**
 * One report with its sections, polled while the run is in flight.
 *
 * A 404 means the report is not the caller's — deleted, or somebody else's, and
 * the API deliberately does not say which. Retrying it would poll an endpoint
 * that can only ever answer the same way, so it is not retried.
 */
export function useReport(
  id: string | null,
  options: { enabled?: boolean } = {},
): UseQueryResult<ReportDetail, unknown> {
  return useQuery({
    queryKey: reportKeys.detail(id ?? ""),
    queryFn: () => fetchReport(id as string),
    enabled: (options.enabled ?? true) && Boolean(id),
    refetchInterval: (query) => activePollInterval(query.state.data),
    retry: (failureCount, error) => !isReportMissing(error) && failureCount < 2,
  });
}

/**
 * Every report type the platform can produce.
 *
 * Long-lived in cache: the catalogue changes when the platform is deployed, not
 * while somebody is filling in a form, and re-fetching it on every dialog open
 * would be a request per click for an answer that never varies.
 */
export function useReportTemplates(
  options: { enabled?: boolean; language?: string } = {},
): UseQueryResult<ReportTemplate[], unknown> {
  return useQuery({
    queryKey: reportKeys.templates(options.language),
    queryFn: () => fetchReportTemplates(options.language),
    enabled: options.enabled ?? true,
    staleTime: 10 * 60 * 1000,
  });
}

/** Platform-wide report health. Requires `reports:monitor`. */
export function useReportMetrics(
  options: { enabled?: boolean; windowDays?: number } = {},
): UseQueryResult<ReportMetrics, unknown> {
  return useQuery({
    queryKey: reportKeys.metrics(options.windowDays),
    queryFn: () => fetchReportMetrics(options.windowDays),
    enabled: options.enabled ?? true,
  });
}

// --------------------------------------------------------------------------- //
// Mutations
// --------------------------------------------------------------------------- //

/**
 * Ask the platform to generate one report.
 *
 * On success the returned `pending` run is written straight into the detail
 * cache, which is what lets the client open it and watch the progress bar move
 * immediately rather than after the next list refetch. The timeline is
 * invalidated too, because the request appends a `report_requested` event to the
 * case's history — and the run that follows appends another, which the poll's own
 * invalidation picks up.
 *
 * The **case and document caches are deliberately left alone**: generating a
 * report changes nothing about the case, its documents, or their indexes.
 */
export function useGenerateReport(): UseMutationResult<
  Report,
  unknown,
  GenerateReportRequest
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (payload: GenerateReportRequest) => generateReport(payload),
    onSuccess: async (report) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: reportKeys.lists() }),
        queryClient.invalidateQueries({ queryKey: reportKeys.detail(report.id) }),
        queryClient.invalidateQueries({ queryKey: timelineKeys.all }),
      ]);
    },
  });
}

/** Produce a report again from the case's current documents. */
export function useRegenerateReport(): UseMutationResult<Report, unknown, string> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => regenerateReport(id),
    onSuccess: async (report) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: reportKeys.detail(report.id) }),
        queryClient.invalidateQueries({ queryKey: reportKeys.lists() }),
        queryClient.invalidateQueries({ queryKey: timelineKeys.all }),
      ]);
    },
  });
}

/**
 * Withdraw a report.
 *
 * The detail cache entry is **removed** rather than invalidated: invalidating it
 * would immediately refetch a report the server now answers 404 for, turning a
 * successful delete into an error toast.
 */
export function useDeleteReport(): UseMutationResult<void, unknown, string> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => deleteReport(id),
    onSuccess: async (_result, id) => {
      queryClient.removeQueries({ queryKey: reportKeys.detail(id) });
      await queryClient.invalidateQueries({ queryKey: reportKeys.lists() });
    },
  });
}

/** What an export request identifies. */
export interface ExportReportRequest {
  id: string;
  format: ReportFormat;
  /** Used only if the server sends no `Content-Disposition` filename. */
  fallbackName: string;
}

/**
 * Download a rendered report.
 *
 * The blob is handed to the browser's download manager here rather than in a
 * component, so the object-URL lifecycle lives in one place — see
 * {@link saveFile}, which revokes it immediately because an object URL keeps the
 * whole blob alive in memory until it is.
 *
 * The **list is invalidated** afterwards, because the server counts exports on
 * the report row and a stale "exported 2 times" is a small lie the next render
 * would keep telling. The timeline is invalidated too: an export appends a
 * `report_exported` event to the case's history.
 */
export function useExportReport(): UseMutationResult<void, unknown, ExportReportRequest> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({ id, format, fallbackName }: ExportReportRequest) => {
      saveFile(await exportReportFile(id, format, fallbackName));
    },
    onSuccess: async (_result, { id }) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: reportKeys.detail(id) }),
        queryClient.invalidateQueries({ queryKey: reportKeys.lists() }),
        queryClient.invalidateQueries({ queryKey: timelineKeys.all }),
      ]);
    },
  });
}

/**
 * Invalidate the history and the timeline when a run finishes.
 *
 * A completed run appends `report_generated` to the case's history — which
 * nothing on the client caused, so nothing would otherwise invalidate it.
 * Watching the report the poll is already fetching is what turns "the badge went
 * green" into "the activity list caught up too".
 *
 * Keyed on the run's identifier *and* its status, so it fires once per transition
 * rather than on every poll tick — the same shape
 * `useIndexCompletionSync` has.
 */
export function useReportCompletionSync(report: Report | undefined): void {
  const queryClient = useQueryClient();
  const previous = React.useRef<string | null>(null);

  const signature = report ? `${report.id}:${report.status}` : null;

  React.useEffect(() => {
    if (!report || !signature) return;
    if (previous.current === signature) return;

    const settled = previous.current !== null && report.isTerminal;
    previous.current = signature;
    if (!settled) return;

    void Promise.all([
      queryClient.invalidateQueries({ queryKey: reportKeys.lists() }),
      queryClient.invalidateQueries({ queryKey: timelineKeys.all }),
    ]);
  }, [queryClient, report, signature]);
}
