/**
 * Case Management DTOs.
 *
 * The request and result shapes of the `/cases` endpoints, in the app's
 * camelCase domain vocabulary. Mapping to and from the API's snake_case wire
 * format happens once, in `lib/api/cases.ts`, so nothing above that layer ever
 * sees a wire shape.
 */

import type { CasePriority, CaseStatus, LegalCase } from "@/types/case";

/** Columns the list can be ordered by. Mirrors the API's `CaseSortField`. */
export const CASE_SORT_FIELDS = [
  "case_number",
  "created_at",
  "updated_at",
  "filing_date",
  "next_hearing_date",
  "priority",
] as const;
export type CaseSortField = (typeof CASE_SORT_FIELDS)[number];

export const SORT_ORDERS = ["asc", "desc"] as const;
export type SortOrder = (typeof SORT_ORDERS)[number];

/** Default page size, matching `DEFAULT_PAGE_SIZE` in `apps/api/schemas/case.py`. */
export const DEFAULT_PAGE_SIZE = 20;

/**
 * The full query state of the case list: search, filters, sort, and page.
 *
 * Held as one object so a change to any part can reset the page in a single
 * place — searching while on page 4 would otherwise show an empty result.
 * `null` means "any" for every filter, which is how the Selects express "All".
 */
export interface CaseListQuery {
  page: number;
  pageSize: number;
  search: string;
  status: CaseStatus | null;
  priority: CasePriority | null;
  assignedLawyerId: string | null;
  assignedCourtRepresentativeId: string | null;
  courtName: string;
  filingDateFrom: string;
  filingDateTo: string;
  hearingDateFrom: string;
  hearingDateTo: string;
  sortBy: CaseSortField;
  sortOrder: SortOrder;
}

export const DEFAULT_CASE_LIST_QUERY: CaseListQuery = {
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  search: "",
  status: null,
  priority: null,
  assignedLawyerId: null,
  assignedCourtRepresentativeId: null,
  courtName: "",
  filingDateFrom: "",
  filingDateTo: "",
  hearingDateFrom: "",
  hearingDateTo: "",
  sortBy: "created_at",
  sortOrder: "desc",
};

/** One page of the case list, with the totals needed to render pagination. */
export interface CasePage {
  items: LegalCase[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/**
 * Body of `POST /cases`.
 *
 * `caseNumber` is omitted to let the API generate one — which is what an empty
 * field in the create form means.
 */
export interface CreateCasePayload {
  caseNumber?: string | null;
  title: string;
  description?: string | null;
  category?: string | null;
  status: CaseStatus;
  priority: CasePriority;
  courtName?: string | null;
  filingDate?: string | null;
  nextHearingDate?: string | null;
  assignedLawyerId?: string | null;
  assignedCourtRepresentativeId?: string | null;
}

/**
 * Body of `PATCH /cases/{id}`.
 *
 * Every field is optional because a PATCH describes only what changes. An
 * explicit `null` clears an optional field — which is how an assignment is
 * removed; omitting the key leaves it alone.
 */
export interface UpdateCasePayload {
  title?: string;
  description?: string | null;
  category?: string | null;
  status?: CaseStatus;
  priority?: CasePriority;
  courtName?: string | null;
  filingDate?: string | null;
  nextHearingDate?: string | null;
  assignedLawyerId?: string | null;
  assignedCourtRepresentativeId?: string | null;
}

/** Body of `PATCH /cases/{id}/assignments`. */
export interface UpdateCaseAssignmentsPayload {
  assignedLawyerId?: string | null;
  assignedCourtRepresentativeId?: string | null;
}
