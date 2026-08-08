/**
 * Zod schemas for AI report generation.
 *
 * Both directions, unlike indexing: there **is** a report form (choose a case, a
 * type, and a language), so this file validates what goes out as well as what
 * comes back.
 *
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/report.py`; where they must agree, the API is the authority.
 */

import { z } from "zod";

import { assistantCitationSchema } from "@/lib/validation/assistant";
import { REPORT_SORT_FIELDS } from "@/types/report-management";
import {
  REPORT_FORMATS,
  REPORT_LANGUAGES,
  REPORT_STATUSES,
  REPORT_TYPES,
} from "@/types/report";

/** Longest title the API accepts, matching `MAX_REPORT_TITLE_LENGTH`. */
export const MAX_TITLE_LENGTH = 255;

// --------------------------------------------------------------------------- //
// Requests
// --------------------------------------------------------------------------- //

/**
 * The generate-report form.
 *
 * A title is optional and empty means "let the platform name it" — the API
 * derives it from the template and the case *number*, which is the identifier
 * everyone entitled to the case already knows and the one that can safely travel
 * in an export filename.
 */
export const generateReportSchema = z.object({
  caseId: z.string().min(1, "Choose the case this report is about."),
  reportType: z.enum(REPORT_TYPES, { message: "Choose a report type." }),
  language: z.enum(REPORT_LANGUAGES).nullable().default(null),
  title: z
    .string()
    .max(MAX_TITLE_LENGTH, `Keep the title under ${MAX_TITLE_LENGTH} characters.`)
    .nullable()
    .default(null),
});

export type GenerateReportValues = z.input<typeof generateReportSchema>;

/** The list query, validated before it becomes a query string. */
export const reportListQuerySchema = z.object({
  page: z.coerce.number().int().min(1).catch(1),
  pageSize: z.coerce.number().int().min(1).max(100).catch(20),
  status: z.enum(REPORT_STATUSES).nullable().catch(null),
  reportType: z.enum(REPORT_TYPES).nullable().catch(null),
  caseId: z.string().nullable().catch(null),
  search: z.string().max(MAX_TITLE_LENGTH).nullable().catch(null),
  sortBy: z.enum(REPORT_SORT_FIELDS).catch("created_at"),
  sortOrder: z.enum(["asc", "desc"]).catch("desc"),
});

// --------------------------------------------------------------------------- //
// Responses
// --------------------------------------------------------------------------- //

/**
 * One report as a history row.
 *
 * `status` and `report_type` are strict enums: both are PostgreSQL enums on the
 * server, so an unrecognised value would be a genuine contract break rather than
 * a newer backend being ahead of this build.
 *
 * `error_code` and `language` are tolerant strings for the opposite reason — a
 * future provider or export backend may report a cause this build has never heard
 * of, and the API always sends a human-readable `error_message` beside the code.
 */
export const reportSchema = z.object({
  id: z.string(),
  case_id: z.string(),
  conversation_id: z.string().nullable().default(null),
  report_type: z.enum(REPORT_TYPES),
  title: z.string(),
  language: z.string(),
  status: z.enum(REPORT_STATUSES),

  sections_total: z.number().int().nonnegative().nullable().default(null),
  sections_completed: z.number().int().nonnegative().default(0),

  started_at: z.string().nullable().default(null),
  finished_at: z.string().nullable().default(null),
  duration_ms: z.number().int().nonnegative().nullable().default(null),
  duration_seconds: z.number().nonnegative().nullable().default(null),
  attempt_count: z.number().int().nonnegative().default(0),

  retrieved_count: z.number().int().nonnegative().nullable().default(null),
  context_count: z.number().int().nonnegative().nullable().default(null),
  grounded_sections: z.number().int().nonnegative().nullable().default(null),
  character_count: z.number().int().nonnegative().nullable().default(null),

  provider: z.string().nullable().default(null),
  model: z.string().nullable().default(null),
  prompt_name: z.string().nullable().default(null),
  prompt_version: z.number().int().nullable().default(null),
  template_version: z.number().int().default(1),

  prompt_tokens: z.number().int().nonnegative().nullable().default(null),
  completion_tokens: z.number().int().nonnegative().nullable().default(null),
  total_tokens: z.number().int().nonnegative().nullable().default(null),

  error_code: z.string().nullable().default(null),
  error_message: z.string().nullable().default(null),

  export_count: z.number().int().nonnegative().default(0),
  last_exported_at: z.string().nullable().default(null),

  created_at: z.string(),
  updated_at: z.string(),

  is_terminal: z.boolean(),
  is_active: z.boolean(),
  progress_percent: z.number().int().min(0).max(100),
});

export const reportSectionSchema = z.object({
  key: z.string(),
  title: z.string(),
  content: z.string(),
  grounded: z.boolean(),
  truncated: z.boolean().default(false),
  citation_markers: z.array(z.number().int()).default([]),
  retrieved_count: z.number().int().nonnegative().default(0),
  context_count: z.number().int().nonnegative().default(0),
  duration_ms: z.number().int().nonnegative().nullable().default(null),
});

/**
 * One report with its sections and citations.
 *
 * The citation schema is {@link assistantCitationSchema}, reused verbatim — the
 * API returns the pipeline's own citation objects, and a parallel schema here
 * would be a second definition of a citation that this feature would then own.
 */
export const reportDetailSchema = reportSchema.extend({
  sections: z.array(reportSectionSchema).default([]),
  citations: z.array(assistantCitationSchema).default([]),
  citation_count: z.number().int().nonnegative().default(0),
  document_count: z.number().int().nonnegative().default(0),
  references_title: z.string(),
  disclaimer: z.string(),
});

export const reportPageSchema = z.object({
  items: z.array(reportSchema),
  total_records: z.number().int().nonnegative(),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total_pages: z.number().int().nonnegative(),
});

export const reportTemplateSchema = z.object({
  report_type: z.enum(REPORT_TYPES),
  title: z.string(),
  description: z.string(),
  sections: z.array(z.object({ key: z.string(), title: z.string() })).default([]),
  section_count: z.number().int().nonnegative().default(0),
});

export const reportTemplateListSchema = z.array(reportTemplateSchema);

export const reportMetricsSchema = z.object({
  total_reports: z.number().int().nonnegative(),
  pending: z.number().int().nonnegative(),
  processing: z.number().int().nonnegative(),
  completed: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),

  success_rate: z.number(),
  failure_rate: z.number(),

  average_duration_ms: z.number().nullable().default(null),
  average_duration_seconds: z.number().nullable().default(null),
  average_characters: z.number().nullable().default(null),

  total_sections: z.number().int().nonnegative(),
  grounded_sections: z.number().int().nonnegative(),
  grounding_rate: z.number(),

  total_exports: z.number().int().nonnegative(),
  exported_reports: z.number().int().nonnegative(),

  total_prompt_tokens: z.number().int().nonnegative().nullable().default(null),
  total_completion_tokens: z.number().int().nonnegative().nullable().default(null),
  metered_reports: z.number().int().nonnegative(),
  average_total_tokens: z.number().nullable().default(null),

  // Open by construction: the keys are report types and failure codes, either of
  // which a future backend may extend.
  reports_by_type: z.record(z.string(), z.number().int().nonnegative()).default({}),
  failures_by_code: z.record(z.string(), z.number().int().nonnegative()).default({}),

  window_days: z.number().int().positive().nullable().default(null),

  available_formats: z.array(z.enum(REPORT_FORMATS)).default([]),
  template_version: z.number().int(),
  llm_available: z.boolean(),
  prompt_available: z.boolean(),
  enabled: z.boolean(),
});
