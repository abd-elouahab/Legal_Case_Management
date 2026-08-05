/**
 * Zod schemas for OCR Processing.
 *
 * Response validation only — there is no OCR *form*. Every field a run carries is
 * produced by the platform, and the one action a user takes (Retry) has an empty
 * body, so there is nothing for a user to fill in and nothing to validate on the
 * way out.
 *
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/ocr.py`; where they must agree, the API is the authority.
 */

import { z } from "zod";

import { OCR_STATUSES } from "@/types/ocr";

const ocrDocumentSummarySchema = z.object({
  id: z.string(),
  case_id: z.string(),
  original_filename: z.string(),
  file_extension: z.string(),
});

/**
 * One extraction run.
 *
 * `status` is a strict enum, unlike a timeline event type: the lifecycle is
 * closed and enforced by a database enum on the server, so an unrecognised value
 * would be a genuine contract break rather than a newer backend being ahead of
 * this build.
 *
 * `error_code` is a tolerant string for the opposite reason — a future engine may
 * report a cause this build has never heard of, and the API always sends a
 * human-readable `error_message` beside it.
 */
export const ocrResultSchema = z.object({
  id: z.string(),
  document_id: z.string(),
  document_version: z.number().int().positive(),
  document: ocrDocumentSummarySchema.nullable().default(null),

  status: z.enum(OCR_STATUSES),

  engine: z.string().nullable().default(null),
  engine_version: z.string().nullable().default(null),
  detected_language: z.string().nullable().default(null),
  page_count: z.number().int().nonnegative().nullable().default(null),
  confidence: z.number().nullable().default(null),

  started_at: z.string().nullable().default(null),
  finished_at: z.string().nullable().default(null),
  duration_ms: z.number().int().nonnegative().nullable().default(null),
  duration_seconds: z.number().nonnegative().nullable().default(null),
  attempt_count: z.number().int().nonnegative(),

  error_code: z.string().nullable().default(null),
  error_message: z.string().nullable().default(null),

  requested_by: z.string().nullable().default(null),
  created_at: z.string(),
  updated_at: z.string(),

  is_terminal: z.boolean(),
  is_active: z.boolean(),
  can_retry: z.boolean(),
});

export const ocrPageSchema = z.object({
  page_number: z.number().int().positive(),
  text: z.string(),
  confidence: z.number().nullable().default(null),
  character_count: z.number().int().nonnegative(),
  is_empty: z.boolean(),
});

export const ocrTextSchema = z.object({
  ocr_result_id: z.string(),
  document_id: z.string(),
  document_version: z.number().int().positive(),
  status: z.enum(OCR_STATUSES),
  detected_language: z.string().nullable().default(null),
  pages: z.array(ocrPageSchema).default([]),
  page_count: z.number().int().nonnegative(),
  character_count: z.number().int().nonnegative(),
  full_text: z.string(),
  page_separator: z.string(),
});

export const ocrResultListSchema = z.array(ocrResultSchema);

export const ocrResultPageSchema = z.object({
  items: z.array(ocrResultSchema),
  total_records: z.number().int().nonnegative(),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total_pages: z.number().int().nonnegative(),
});

export const ocrMetricsSchema = z.object({
  window_days: z.number().int().positive().nullable().default(null),

  total_runs: z.number().int().nonnegative(),
  pending: z.number().int().nonnegative(),
  processing: z.number().int().nonnegative(),
  completed: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),
  finished_runs: z.number().int().nonnegative(),

  success_rate: z.number(),
  failure_rate: z.number(),
  average_duration_ms: z.number().nullable().default(null),
  average_duration_seconds: z.number().nullable().default(null),
  // Open by construction: the keys are failure codes, which a future engine may
  // extend.
  failures_by_code: z.record(z.string(), z.number().int().nonnegative()).default({}),

  engine: z.string(),
  engine_available: z.boolean(),
  enabled: z.boolean(),
  supported_extensions: z.array(z.string()).default([]),
});

/** The list query, validated before it becomes a query string. */
export const ocrListQuerySchema = z.object({
  page: z.coerce.number().int().min(1).catch(1),
  pageSize: z.coerce.number().int().min(1).max(100).catch(20),
  status: z.enum(OCR_STATUSES).nullable().catch(null),
  documentId: z.string().nullable().catch(null),
  caseId: z.string().nullable().catch(null),
  errorCode: z.string().max(50).nullable().catch(null),
});
