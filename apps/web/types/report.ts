/**
 * AI report types.
 *
 * Mirror the API's report payloads (`apps/api/schemas/report.py`) and the
 * vocabularies defined in `apps/api/models/report.py` and
 * `apps/api/core/reports.py`. Union types rather than magic strings, per the code
 * standards: referencing a status or a report type the platform does not define
 * is a compile error.
 *
 * **The report *type* set is closed**, like the indexing status set and unlike a
 * timeline event type: it is a PostgreSQL enum on the server, every member needs
 * a template the server holds, and a type this build has never heard of is one it
 * could not render anyway. The **catalogue is still fetched** rather than
 * hardcoded (see {@link ReportTemplate}), because the *sections* and their labels
 * are the server's and a client that listed them itself would drift the first
 * time a template changed.
 *
 * **Nothing here describes a passage, a prompt, or a vector.** A report carries
 * prose and citations; how either was produced is the pipeline's business and
 * never reaches a client.
 */

import type { AssistantCitation } from "@/types/assistant";

/** Lifecycle states of one generation run, in the order they occur. */
export const REPORT_STATUSES = ["pending", "processing", "completed", "failed"] as const;
export type ReportStatus = (typeof REPORT_STATUSES)[number];

/** Human-readable status labels (future: i18n keys). */
export const REPORT_STATUS_LABELS: Record<ReportStatus, string> = {
  pending: "Queued",
  processing: "Generating",
  completed: "Ready",
  failed: "Failed",
};

/** The reports the platform can produce. */
export const REPORT_TYPES = [
  "case_summary",
  "hearing_preparation",
  "evidence_summary",
  "chronological_timeline",
  "executive_summary",
] as const;
export type ReportType = (typeof REPORT_TYPES)[number];

/**
 * Fallback labels for a report type.
 *
 * Used only where the fetched catalogue is not to hand — a history row rendered
 * before `/reports/templates` has resolved. Anywhere the catalogue *is*
 * available, its titles win, because they are the server's and are written in
 * the report's own language.
 */
export const REPORT_TYPE_LABELS: Record<ReportType, string> = {
  case_summary: "Case Summary",
  hearing_preparation: "Hearing Preparation",
  evidence_summary: "Evidence Summary",
  chronological_timeline: "Chronological Timeline",
  executive_summary: "Executive Summary",
};

/** A readable name for a report type, falling back to a readable identifier. */
export function reportTypeLabel(value: string): string {
  const known = REPORT_TYPE_LABELS[value as ReportType];
  if (known) return known;

  const words = value.replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Report";
}

/** Formats a finished report can be downloaded in. */
export const REPORT_FORMATS = ["pdf", "markdown"] as const;
export type ReportFormat = (typeof REPORT_FORMATS)[number];

export const REPORT_FORMAT_LABELS: Record<ReportFormat, string> = {
  pdf: "PDF",
  markdown: "Markdown",
};

/**
 * Why a run ended without a report.
 *
 * Open, deliberately: a later provider or export backend may report a cause this
 * build has never heard of, and a failed run must still render.
 * {@link reportFailureLabel} falls back — and the API always sends a
 * human-readable `errorMessage` alongside, so the label is a refinement rather
 * than the only thing a user sees.
 */
export const REPORT_FAILURE_CODES = [
  "retrieval_unavailable",
  "llm_unavailable",
  "timeout",
  "llm_failure",
  "malformed_response",
  "insufficient_context",
  "export_failure",
  "unknown",
] as const;
export type KnownReportFailureCode = (typeof REPORT_FAILURE_CODES)[number];

const REPORT_FAILURE_LABELS: Record<KnownReportFailureCode, string> = {
  retrieval_unavailable: "Documents could not be retrieved",
  llm_unavailable: "AI service unavailable",
  timeout: "Took too long",
  llm_failure: "AI service failed",
  malformed_response: "Unusable response",
  insufficient_context: "Nothing to report on",
  export_failure: "Export failed",
  unknown: "Did not complete",
};

/** A short label for a failure cause, falling back to a readable identifier. */
export function reportFailureLabel(code: string | null): string {
  if (!code) return "Failed";

  const known = REPORT_FAILURE_LABELS[code as KnownReportFailureCode];
  if (known) return known;

  const words = code.replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Failed";
}

/** Languages a report can be written in, matching the API's supported set. */
export const REPORT_LANGUAGES = ["fr", "ar", "en"] as const;
export type ReportLanguage = (typeof REPORT_LANGUAGES)[number];

export const REPORT_LANGUAGE_LABELS: Record<ReportLanguage, string> = {
  fr: "French",
  ar: "Arabic",
  en: "English",
};

/** Whether a language reads right-to-left, which decides `dir` on its prose. */
export function isRtlLanguage(language: string | null): boolean {
  return language === "ar";
}

/** One section of a finished report. */
export interface ReportSection {
  /** Stable identifier within the template. Never shown to a user. */
  key: string;
  title: string;
  content: string;
  /**
   * Whether the case file supported this section. `false` means the documents do
   * not cover it — a recorded outcome of a successful report, not an error, and
   * the content is the platform's own sentence saying so.
   */
  grounded: boolean;
  /**
   * Whether this section hit the model's output ceiling and stops mid-thought.
   * Reported rather than hidden: a legal section that ends early must not be
   * read as a complete one — the same rule an assistant answer follows.
   */
  truncated: boolean;
  /** Report-level markers this section's prose cites, in first-appearance order. */
  citationMarkers: number[];
  retrievedCount: number;
  contextCount: number;
  durationMs: number | null;
}

/**
 * One report as a history row.
 *
 * Deliberately without its sections: a page of twenty reports carrying twenty
 * full reports would be megabytes of generated legal prose sent to render a list
 * of titles — and a client polling a run's progress would re-download the
 * finished report on every tick.
 */
export interface Report {
  id: string;
  caseId: string;
  conversationId: string | null;
  reportType: ReportType;
  title: string;
  language: string;
  status: ReportStatus;

  /** Null until the run has been planned — not the same as zero. */
  sectionsTotal: number | null;
  sectionsCompleted: number;

  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
  durationSeconds: number | null;
  attemptCount: number;

  retrievedCount: number | null;
  contextCount: number | null;
  groundedSections: number | null;
  characterCount: number | null;

  provider: string | null;
  model: string | null;
  promptName: string | null;
  promptVersion: number | null;
  templateVersion: number;

  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;

  errorCode: string | null;
  errorMessage: string | null;

  exportCount: number;
  lastExportedAt: string | null;

  createdAt: string;
  updatedAt: string;

  /**
   * Whether the run has finished, whether to keep polling, and how far it got.
   *
   * All three taken from the server rather than re-derived here, for the same
   * reason an indexing run's `isActive` is: the rule lives in
   * `apps/api/models/report.py`, and a second copy in the browser would be the
   * one the user sees when the two disagree.
   */
  isTerminal: boolean;
  isActive: boolean;
  progressPercent: number;
}

/** A report together with its sections, its citations, and its front matter. */
export interface ReportDetail extends Report {
  sections: ReportSection[];
  /**
   * Every source the report rests on, de-duplicated across sections and numbered
   * globally. The **pipeline's own citation shape, reused verbatim** — the same
   * type the assistant renders, for the same reason it reuses it: a parallel
   * model here would be a second vocabulary to keep in step, and the first change
   * touching only one of them would produce a citation one surface shows and the
   * other cannot. The `[n]` markers in the sections resolve into this list.
   */
  citations: AssistantCitation[];
  citationCount: number;
  documentCount: number;
  /** Heading of the reference list, in the report's language. */
  referencesTitle: string;
  /**
   * The standing note that the report is not legal advice. Sent with the report
   * rather than added by each client, so it survives every surface and every
   * export.
   */
  disclaimer: string;
}

/** One section a template will produce, as advertised before generation. */
export interface ReportTemplateSection {
  key: string;
  title: string;
}

/**
 * One report type the platform can produce.
 *
 * Fetched rather than hardcoded so adding a sixth template is a server-side
 * entry and the picker, the type filter, and the section preview all follow
 * without a frontend change.
 */
export interface ReportTemplate {
  reportType: ReportType;
  title: string;
  description: string;
  sections: ReportTemplateSection[];
  sectionCount: number;
}

/** Platform-wide report health, for the monitoring view. */
export interface ReportMetrics {
  totalReports: number;
  pending: number;
  processing: number;
  completed: number;
  failed: number;

  successRate: number;
  failureRate: number;

  averageDurationMs: number | null;
  averageDurationSeconds: number | null;
  averageCharacters: number | null;

  totalSections: number;
  groundedSections: number;
  groundingRate: number;

  totalExports: number;
  exportedReports: number;

  /** Null when no provider has ever reported usage — not the same as zero. */
  totalPromptTokens: number | null;
  totalCompletionTokens: number | null;
  meteredReports: number;
  averageTotalTokens: number | null;

  reportsByType: Record<string, number>;
  failuresByCode: Record<string, number>;

  windowDays: number | null;

  /** Formats this deployment can actually produce — never offer one that is absent. */
  availableFormats: ReportFormat[];
  templateVersion: number;
  /** False means every generation will fail: no provider client can be built. */
  llmAvailable: boolean;
  /** False means every section will fail: the pipeline's prompt cannot be loaded. */
  promptAvailable: boolean;
  enabled: boolean;
}
