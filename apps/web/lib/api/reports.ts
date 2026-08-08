/**
 * AI report API calls.
 *
 * Thin, typed wrappers over the `/reports` endpoints. Every response is validated
 * with Zod and mapped from the API's snake_case wire format to the app's
 * camelCase domain types, so nothing above this layer sees a wire shape — and a
 * backend change that alters a payload fails here, loudly, instead of surfacing
 * as `undefined` in a progress bar.
 *
 * **The export goes through `lib/api/upload.ts` rather than the JSON client**,
 * for the reason a document download does: it needs a `Blob` rather than a parsed
 * object, and the access token is a header rather than a cookie — so pointing an
 * anchor at the API would send an anonymous request and download a 401.
 */

import { apiRequest } from "@/lib/api/client";
import { REPORT_ENDPOINTS } from "@/lib/api/config";
import { apiFetchFile, type FetchedFile } from "@/lib/api/upload";
import {
  reportDetailSchema,
  reportMetricsSchema,
  reportPageSchema,
  reportSchema,
  reportTemplateListSchema,
} from "@/lib/validation/report";
import type {
  Report,
  ReportDetail,
  ReportFormat,
  ReportMetrics,
  ReportSection,
  ReportTemplate,
} from "@/types/report";
import type {
  GenerateReportRequest,
  ReportListQuery,
  ReportPage,
} from "@/types/report-management";

type ReportWire = ReturnType<typeof reportSchema.parse>;
type ReportDetailWire = ReturnType<typeof reportDetailSchema.parse>;
type ReportSectionWire = ReportDetailWire["sections"][number];
type ReportCitationWire = ReportDetailWire["citations"][number];
type ReportTemplateWire = ReturnType<typeof reportTemplateListSchema.parse>[number];
type ReportMetricsWire = ReturnType<typeof reportMetricsSchema.parse>;

/** Map one API report row onto the app's {@link Report}. */
function toReport(payload: ReportWire): Report {
  return {
    id: payload.id,
    caseId: payload.case_id,
    conversationId: payload.conversation_id,
    reportType: payload.report_type,
    title: payload.title,
    language: payload.language,
    status: payload.status,

    sectionsTotal: payload.sections_total,
    sectionsCompleted: payload.sections_completed,

    startedAt: payload.started_at,
    finishedAt: payload.finished_at,
    durationMs: payload.duration_ms,
    durationSeconds: payload.duration_seconds,
    attemptCount: payload.attempt_count,

    retrievedCount: payload.retrieved_count,
    contextCount: payload.context_count,
    groundedSections: payload.grounded_sections,
    characterCount: payload.character_count,

    provider: payload.provider,
    model: payload.model,
    promptName: payload.prompt_name,
    promptVersion: payload.prompt_version,
    templateVersion: payload.template_version,

    promptTokens: payload.prompt_tokens,
    completionTokens: payload.completion_tokens,
    totalTokens: payload.total_tokens,

    errorCode: payload.error_code,
    errorMessage: payload.error_message,

    exportCount: payload.export_count,
    lastExportedAt: payload.last_exported_at,

    createdAt: payload.created_at,
    updatedAt: payload.updated_at,

    isTerminal: payload.is_terminal,
    isActive: payload.is_active,
    progressPercent: payload.progress_percent,
  };
}

function toSection(payload: ReportSectionWire): ReportSection {
  return {
    key: payload.key,
    title: payload.title,
    content: payload.content,
    grounded: payload.grounded,
    truncated: payload.truncated,
    citationMarkers: payload.citation_markers,
    retrievedCount: payload.retrieved_count,
    contextCount: payload.context_count,
    durationMs: payload.duration_ms,
  };
}

function toCitation(payload: ReportCitationWire) {
  return {
    marker: payload.marker,
    documentId: payload.document_id,
    documentName: payload.document_name,
    documentVersion: payload.document_version,
    pageNumber: payload.page_number,
    caseId: payload.case_id,
    score: payload.score,
    excerpt: payload.excerpt,
    excerptTruncated: payload.excerpt_truncated,
    referenced: payload.referenced,
  };
}

function toReportDetail(payload: ReportDetailWire): ReportDetail {
  return {
    ...toReport(payload),
    sections: payload.sections.map(toSection),
    citations: payload.citations.map(toCitation),
    citationCount: payload.citation_count,
    documentCount: payload.document_count,
    referencesTitle: payload.references_title,
    disclaimer: payload.disclaimer,
  };
}

function toTemplate(payload: ReportTemplateWire): ReportTemplate {
  return {
    reportType: payload.report_type,
    title: payload.title,
    description: payload.description,
    sections: payload.sections.map((section) => ({
      key: section.key,
      title: section.title,
    })),
    sectionCount: payload.section_count,
  };
}

function toReportMetrics(payload: ReportMetricsWire): ReportMetrics {
  return {
    totalReports: payload.total_reports,
    pending: payload.pending,
    processing: payload.processing,
    completed: payload.completed,
    failed: payload.failed,

    successRate: payload.success_rate,
    failureRate: payload.failure_rate,

    averageDurationMs: payload.average_duration_ms,
    averageDurationSeconds: payload.average_duration_seconds,
    averageCharacters: payload.average_characters,

    totalSections: payload.total_sections,
    groundedSections: payload.grounded_sections,
    groundingRate: payload.grounding_rate,

    totalExports: payload.total_exports,
    exportedReports: payload.exported_reports,

    totalPromptTokens: payload.total_prompt_tokens,
    totalCompletionTokens: payload.total_completion_tokens,
    meteredReports: payload.metered_reports,
    averageTotalTokens: payload.average_total_tokens,

    reportsByType: payload.reports_by_type,
    failuresByCode: payload.failures_by_code,

    windowDays: payload.window_days,

    availableFormats: payload.available_formats,
    templateVersion: payload.template_version,
    llmAvailable: payload.llm_available,
    promptAvailable: payload.prompt_available,
    enabled: payload.enabled,
  };
}

/**
 * Build the query string for a list request.
 *
 * "Any" filters are omitted rather than sent as blanks, so the request URL
 * reflects what is actually being asked — which also makes the query a stable
 * cache key for TanStack Query.
 */
export function buildReportListParams(query: ReportListQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
    sort_by: query.sortBy,
    sort_order: query.sortOrder,
  });

  const optional: Array<[string, string | null]> = [
    ["status", query.status],
    ["report_type", query.reportType],
    ["case_id", query.caseId],
    ["search", query.search],
  ];

  for (const [key, value] of optional) {
    if (value) params.set(key, value);
  }

  return params.toString();
}

/**
 * Ask the platform to generate one report.
 *
 * Answers 201 with the run in its `pending` state, so the caller polls
 * {@link fetchReport} for the outcome — a seven-section report is seven
 * retrievals and seven model calls, and waiting for it on an HTTP connection is
 * not something either end should try.
 */
export async function generateReport(payload: GenerateReportRequest): Promise<Report> {
  const raw = await apiRequest<unknown>(REPORT_ENDPOINTS.create, {
    method: "POST",
    body: {
      case_id: payload.caseId,
      report_type: payload.reportType,
      ...(payload.language ? { language: payload.language } : {}),
      ...(payload.title ? { title: payload.title } : {}),
      ...(payload.conversationId ? { conversation_id: payload.conversationId } : {}),
    },
  });
  return toReport(reportSchema.parse(raw));
}

/** Fetch one page of the caller's own report history. */
export async function fetchReports(query: ReportListQuery): Promise<ReportPage> {
  const raw = await apiRequest<unknown>(
    `${REPORT_ENDPOINTS.list}?${buildReportListParams(query)}`,
  );
  const data = reportPageSchema.parse(raw);

  return {
    items: data.items.map(toReport),
    totalRecords: data.total_records,
    page: data.page,
    pageSize: data.page_size,
    totalPages: data.total_pages,
  };
}

/**
 * Fetch one report with its sections and citations.
 *
 * **This is also the progress endpoint.** While a run is in flight the sections
 * are empty and `progressPercent` moves; poll it while `isActive` is true.
 */
export async function fetchReport(id: string): Promise<ReportDetail> {
  const raw = await apiRequest<unknown>(REPORT_ENDPOINTS.detail(id));
  return toReportDetail(reportDetailSchema.parse(raw));
}

/**
 * Produce this report again from the case's current documents.
 *
 * The same report, not a new one: the identifier and the title are unchanged, so
 * a link somebody saved keeps working. Answers 202 with the run back in its
 * `pending` state.
 */
export async function regenerateReport(id: string): Promise<Report> {
  const raw = await apiRequest<unknown>(REPORT_ENDPOINTS.regenerate(id), {
    method: "POST",
  });
  return toReport(reportSchema.parse(raw));
}

/** Withdraw a report from the caller's history. */
export async function deleteReport(id: string): Promise<void> {
  await apiRequest<void>(REPORT_ENDPOINTS.detail(id), { method: "DELETE" });
}

/** Fetch every report type the platform can produce, with its sections. */
export async function fetchReportTemplates(language?: string): Promise<ReportTemplate[]> {
  const path = language
    ? `${REPORT_ENDPOINTS.templates}?language=${encodeURIComponent(language)}`
    : REPORT_ENDPOINTS.templates;
  const raw = await apiRequest<unknown>(path);
  return reportTemplateListSchema.parse(raw).map(toTemplate);
}

/** Platform-wide report health. Requires `reports:monitor`. */
export async function fetchReportMetrics(windowDays?: number): Promise<ReportMetrics> {
  const path = windowDays
    ? `${REPORT_ENDPOINTS.metrics}?window_days=${windowDays}`
    : REPORT_ENDPOINTS.metrics;
  const raw = await apiRequest<unknown>(path);
  return toReportMetrics(reportMetricsSchema.parse(raw));
}

/**
 * Fetch a rendered report for saving.
 *
 * As a blob through an authenticated request, for the same reason a document
 * download is: the access token is a header rather than a cookie, so pointing an
 * anchor or an `<iframe>` at the API would arrive anonymous.
 */
export async function exportReportFile(
  id: string,
  format: ReportFormat,
  fallbackName: string,
): Promise<FetchedFile> {
  return apiFetchFile(REPORT_ENDPOINTS.exportFile(id, format), fallbackName);
}
