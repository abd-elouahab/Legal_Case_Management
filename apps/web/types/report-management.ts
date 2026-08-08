/**
 * AI report DTOs.
 *
 * The request and result shapes of the report endpoints, in the app's camelCase
 * domain vocabulary. Mapping to and from the API's snake_case wire format happens
 * once, in `lib/api/reports.ts`, so nothing above that layer ever sees a wire
 * shape.
 */

import type { Report, ReportLanguage, ReportStatus, ReportType } from "@/types/report";

/** Default page size, matching `REPORT_PAGE_SIZE` in `apps/api/core/config.py`. */
export const DEFAULT_PAGE_SIZE = 20;

/** Columns the history may be ordered by, matching the API's allow-list. */
export const REPORT_SORT_FIELDS = [
  "created_at",
  "updated_at",
  "finished_at",
  "duration_ms",
  "title",
  "status",
  "report_type",
] as const;
export type ReportSortField = (typeof REPORT_SORT_FIELDS)[number];

export type SortOrder = "asc" | "desc";

/**
 * The full query state of the report history: filters, sort, and page.
 *
 * Held as one object so a change to any part can reset the page in a single
 * place — filtering while on page 4 would otherwise show an empty result. `null`
 * means "any" for every filter, which is how the Selects express "All".
 *
 * **There is no requester filter**, and there will not be one: the history is the
 * caller's own by construction, so a filter naming a user would either be
 * redundant or be a request the API must refuse — and offering it would suggest
 * the second is possible.
 */
export interface ReportListQuery {
  page: number;
  pageSize: number;
  status: ReportStatus | null;
  reportType: ReportType | null;
  caseId: string | null;
  search: string | null;
  sortBy: ReportSortField;
  sortOrder: SortOrder;
}

export const DEFAULT_REPORT_LIST_QUERY: ReportListQuery = {
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  status: null,
  reportType: null,
  caseId: null,
  search: null,
  // Newest first: a history is read that way.
  sortBy: "created_at",
  sortOrder: "desc",
};

/** One page of reports, with the totals needed to render pagination. */
export interface ReportPage {
  items: Report[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/**
 * What a client sends to generate one report.
 *
 * Deliberately small: everything that decides what the report *contains* comes
 * from the template, so a request chooses the case, the type, and the language.
 * The retrieval controls the API also accepts (`topK`, `minScore`, `filters`) are
 * not exposed here — they are per-request tuning for a caller who knows the
 * pipeline, and a form that offered them would ask a lawyer to choose a
 * similarity floor.
 */
export interface GenerateReportRequest {
  caseId: string;
  reportType: ReportType;
  language?: ReportLanguage | null;
  title?: string | null;
  /** Provenance, when the report was started from an assistant conversation. */
  conversationId?: string | null;
}
