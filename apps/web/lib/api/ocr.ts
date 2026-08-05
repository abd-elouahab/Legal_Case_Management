/**
 * OCR Processing API calls.
 *
 * Thin, typed wrappers over the `/documents/{id}/ocr*` and `/ocr` endpoints.
 * Every response is validated with Zod and mapped from the API's snake_case wire
 * format to the app's camelCase domain types, so nothing above this layer sees a
 * wire shape — and a backend change that alters a payload fails here, loudly,
 * instead of surfacing as `undefined` in a status badge.
 *
 * Status and text are two calls rather than one, mirroring the API: a client
 * polling a document being processed must not pull a hundred pages of recognised
 * prose on every tick.
 */

import { apiRequest } from "@/lib/api/client";
import { OCR_ENDPOINTS } from "@/lib/api/config";
import {
  ocrMetricsSchema,
  ocrResultListSchema,
  ocrResultPageSchema,
  ocrResultSchema,
  ocrTextSchema,
} from "@/lib/validation/ocr";
import type {
  OcrDocumentSummary,
  OcrMetrics,
  OcrPage,
  OcrResult,
  OcrText,
} from "@/types/ocr";
import type { OcrListQuery, OcrResultPage } from "@/types/ocr-management";

type OcrResultWire = ReturnType<typeof ocrResultSchema.parse>;
type OcrTextWire = ReturnType<typeof ocrTextSchema.parse>;
type OcrPageWire = OcrTextWire["pages"][number];
type OcrDocumentWire = NonNullable<OcrResultWire["document"]>;
type OcrMetricsWire = ReturnType<typeof ocrMetricsSchema.parse>;

function toOcrDocument(payload: OcrDocumentWire | null): OcrDocumentSummary | null {
  if (!payload) return null;
  return {
    id: payload.id,
    caseId: payload.case_id,
    originalFilename: payload.original_filename,
    fileExtension: payload.file_extension,
  };
}

/** Map one API run record onto the app's {@link OcrResult}. */
function toOcrResult(payload: OcrResultWire): OcrResult {
  return {
    id: payload.id,
    documentId: payload.document_id,
    documentVersion: payload.document_version,
    document: toOcrDocument(payload.document),
    status: payload.status,
    engine: payload.engine,
    engineVersion: payload.engine_version,
    detectedLanguage: payload.detected_language,
    pageCount: payload.page_count,
    confidence: payload.confidence,
    startedAt: payload.started_at,
    finishedAt: payload.finished_at,
    durationMs: payload.duration_ms,
    durationSeconds: payload.duration_seconds,
    attemptCount: payload.attempt_count,
    errorCode: payload.error_code,
    errorMessage: payload.error_message,
    requestedBy: payload.requested_by,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
    isTerminal: payload.is_terminal,
    isActive: payload.is_active,
    canRetry: payload.can_retry,
  };
}

function toOcrPage(payload: OcrPageWire): OcrPage {
  return {
    pageNumber: payload.page_number,
    text: payload.text,
    confidence: payload.confidence,
    characterCount: payload.character_count,
    isEmpty: payload.is_empty,
  };
}

function toOcrText(payload: OcrTextWire): OcrText {
  return {
    ocrResultId: payload.ocr_result_id,
    documentId: payload.document_id,
    documentVersion: payload.document_version,
    status: payload.status,
    detectedLanguage: payload.detected_language,
    pages: payload.pages.map(toOcrPage),
    pageCount: payload.page_count,
    characterCount: payload.character_count,
    fullText: payload.full_text,
    pageSeparator: payload.page_separator,
  };
}

function toOcrMetrics(payload: OcrMetricsWire): OcrMetrics {
  return {
    windowDays: payload.window_days,
    totalRuns: payload.total_runs,
    pending: payload.pending,
    processing: payload.processing,
    completed: payload.completed,
    failed: payload.failed,
    finishedRuns: payload.finished_runs,
    successRate: payload.success_rate,
    failureRate: payload.failure_rate,
    averageDurationMs: payload.average_duration_ms,
    averageDurationSeconds: payload.average_duration_seconds,
    failuresByCode: payload.failures_by_code,
    engine: payload.engine,
    engineAvailable: payload.engine_available,
    enabled: payload.enabled,
    supportedExtensions: payload.supported_extensions,
  };
}

/**
 * Build the query string for a list request.
 *
 * "Any" filters are omitted rather than sent as blanks, so the request URL
 * reflects what is actually being asked — which also makes the query a stable
 * cache key for TanStack Query.
 */
export function buildOcrListParams(query: OcrListQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
  });

  const optional: Array<[string, string | null]> = [
    ["status", query.status],
    ["document_id", query.documentId],
    ["case_id", query.caseId],
    ["error_code", query.errorCode],
  ];

  for (const [key, value] of optional) {
    if (value) params.set(key, value);
  }

  return params.toString();
}

/** Fetch a document's extraction status. Defaults to the current version. */
export async function fetchOcrResult(
  documentId: string,
  version?: number,
): Promise<OcrResult> {
  const raw = await apiRequest<unknown>(OCR_ENDPOINTS.status(documentId, version));
  return toOcrResult(ocrResultSchema.parse(raw));
}

/** Fetch a document's extracted text, page by page. */
export async function fetchOcrText(documentId: string, version?: number): Promise<OcrText> {
  const raw = await apiRequest<unknown>(OCR_ENDPOINTS.text(documentId, version));
  return toOcrText(ocrTextSchema.parse(raw));
}

/** Fetch every extraction recorded for a document, oldest version first. */
export async function fetchOcrHistory(documentId: string): Promise<OcrResult[]> {
  const raw = await apiRequest<unknown>(OCR_ENDPOINTS.history(documentId));
  return ocrResultListSchema.parse(raw).map(toOcrResult);
}

/**
 * Queue a fresh extraction for a document already uploaded.
 *
 * No file travels: the API takes the document's identifier and re-reads what is
 * already stored. Answers 202 with the run in its `pending` state, so the caller
 * polls {@link fetchOcrResult} for the outcome.
 */
export async function retryOcr(documentId: string, version?: number): Promise<OcrResult> {
  const raw = await apiRequest<unknown>(OCR_ENDPOINTS.retry(documentId, version), {
    method: "POST",
  });
  return toOcrResult(ocrResultSchema.parse(raw));
}

/** Fetch one page of the extraction-run list. */
export async function fetchOcrResults(query: OcrListQuery): Promise<OcrResultPage> {
  const raw = await apiRequest<unknown>(`${OCR_ENDPOINTS.list}?${buildOcrListParams(query)}`);
  const data = ocrResultPageSchema.parse(raw);

  return {
    items: data.items.map(toOcrResult),
    totalRecords: data.total_records,
    page: data.page,
    pageSize: data.page_size,
    totalPages: data.total_pages,
  };
}

/** Fetch platform-wide extraction health. Requires `ocr:monitor`. */
export async function fetchOcrMetrics(windowDays?: number): Promise<OcrMetrics> {
  const path = windowDays
    ? `${OCR_ENDPOINTS.metrics}?window_days=${windowDays}`
    : OCR_ENDPOINTS.metrics;
  const raw = await apiRequest<unknown>(path);
  return toOcrMetrics(ocrMetricsSchema.parse(raw));
}
