/**
 * Case Management API calls.
 *
 * Thin, typed wrappers over the `/cases/*` endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape —
 * and a backend change that alters the payload fails here, loudly, instead of
 * surfacing as `undefined` in a table cell.
 */

import { apiRequest } from "@/lib/api/client";
import { CASE_ENDPOINTS } from "@/lib/api/config";
import { casePageSchema, legalCaseSchema } from "@/lib/validation/case";
import type { CaseUserSummary, LegalCase } from "@/types/case";
import type {
  CaseListQuery,
  CasePage,
  CreateCasePayload,
  UpdateCaseAssignmentsPayload,
  UpdateCasePayload,
} from "@/types/case-management";

type LegalCaseWire = ReturnType<typeof legalCaseSchema.parse>;
type CaseUserWire = NonNullable<LegalCaseWire["assigned_lawyer"]>;

function toCaseUser(payload: CaseUserWire | null): CaseUserSummary | null {
  if (!payload) return null;
  return {
    id: payload.id,
    fullName: payload.full_name,
    email: payload.email,
    role: payload.role,
  };
}

/** Map one API case record onto the app's {@link LegalCase}. */
function toLegalCase(payload: LegalCaseWire): LegalCase {
  return {
    id: payload.id,
    caseNumber: payload.case_number,
    title: payload.title,
    description: payload.description,
    category: payload.category,
    status: payload.status,
    priority: payload.priority,
    courtName: payload.court_name,
    filingDate: payload.filing_date,
    nextHearingDate: payload.next_hearing_date,
    assignedLawyerId: payload.assigned_lawyer_id,
    assignedCourtRepresentativeId: payload.assigned_court_representative_id,
    assignedLawyer: toCaseUser(payload.assigned_lawyer),
    assignedCourtRepresentative: toCaseUser(payload.assigned_court_representative),
    createdBy: payload.created_by,
    updatedBy: payload.updated_by,
    creator: toCaseUser(payload.creator),
    updater: toCaseUser(payload.updater),
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
    isArchived: payload.is_archived,
    allowedTransitions: payload.allowed_transitions,
  };
}

/**
 * Build the query string for a list request.
 *
 * Empty search terms, blank dates, and "any" filters are omitted rather than
 * sent as blanks, so the request URL reflects what is actually being asked —
 * which also makes the query a stable cache key for TanStack Query.
 */
export function buildCaseListParams(query: CaseListQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
    sort_by: query.sortBy,
    sort_order: query.sortOrder,
  });

  const optional: Array<[string, string | null]> = [
    ["search", query.search.trim() || null],
    ["status", query.status],
    ["priority", query.priority],
    ["assigned_lawyer_id", query.assignedLawyerId],
    ["assigned_court_representative_id", query.assignedCourtRepresentativeId],
    ["court_name", query.courtName.trim() || null],
    ["filing_date_from", query.filingDateFrom || null],
    ["filing_date_to", query.filingDateTo || null],
    ["hearing_date_from", query.hearingDateFrom || null],
    ["hearing_date_to", query.hearingDateTo || null],
  ];

  for (const [key, value] of optional) {
    if (value) params.set(key, value);
  }

  return params.toString();
}

/** Fetch one page of the case list. */
export async function fetchCases(query: CaseListQuery): Promise<CasePage> {
  const raw = await apiRequest<unknown>(`${CASE_ENDPOINTS.list}?${buildCaseListParams(query)}`);
  const data = casePageSchema.parse(raw);

  return {
    items: data.items.map(toLegalCase),
    totalRecords: data.total_records,
    page: data.page,
    pageSize: data.page_size,
    totalPages: data.total_pages,
  };
}

/** Fetch one case's complete record. */
export async function fetchCase(id: string): Promise<LegalCase> {
  const raw = await apiRequest<unknown>(CASE_ENDPOINTS.detail(id));
  return toLegalCase(legalCaseSchema.parse(raw));
}

/**
 * Create a case.
 *
 * `case_number` is sent only when the caller supplied one; omitting it is what
 * asks the API to generate the next number in the series.
 */
export async function createCase(payload: CreateCasePayload): Promise<LegalCase> {
  const body: Record<string, unknown> = {
    title: payload.title,
    description: payload.description ?? null,
    category: payload.category ?? null,
    status: payload.status,
    priority: payload.priority,
    court_name: payload.courtName ?? null,
    filing_date: payload.filingDate ?? null,
    next_hearing_date: payload.nextHearingDate ?? null,
    assigned_lawyer_id: payload.assignedLawyerId ?? null,
    assigned_court_representative_id: payload.assignedCourtRepresentativeId ?? null,
  };

  if (payload.caseNumber) body.case_number = payload.caseNumber;

  const raw = await apiRequest<unknown>(CASE_ENDPOINTS.create, { method: "POST", body });
  return toLegalCase(legalCaseSchema.parse(raw));
}

/** Wire names for the fields a PATCH may carry, in the app's vocabulary. */
const UPDATE_FIELDS = {
  title: "title",
  description: "description",
  category: "category",
  status: "status",
  priority: "priority",
  courtName: "court_name",
  filingDate: "filing_date",
  nextHearingDate: "next_hearing_date",
  assignedLawyerId: "assigned_lawyer_id",
  assignedCourtRepresentativeId: "assigned_court_representative_id",
} as const satisfies Record<keyof UpdateCasePayload, string>;

/**
 * Apply a partial update.
 *
 * Only the keys present in `payload` are sent, so an omitted field is left
 * untouched by the server while an explicit `null` clears it — the distinction
 * the PATCH contract rests on, and the one that removes an assignment.
 */
export async function updateCase(id: string, payload: UpdateCasePayload): Promise<LegalCase> {
  const body: Record<string, unknown> = {};

  for (const [key, wireName] of Object.entries(UPDATE_FIELDS)) {
    const value = payload[key as keyof UpdateCasePayload];
    if (value !== undefined) body[wireName] = value;
  }

  const raw = await apiRequest<unknown>(CASE_ENDPOINTS.detail(id), { method: "PATCH", body });
  return toLegalCase(legalCaseSchema.parse(raw));
}

/**
 * Assign, change, or remove the people on a case.
 *
 * Its own endpoint rather than a plain update because assignment is gated on
 * `cases:assign` — a distinct capability from editing the case itself.
 */
export async function updateCaseAssignments(
  id: string,
  payload: UpdateCaseAssignmentsPayload,
): Promise<LegalCase> {
  const body: Record<string, unknown> = {};

  if (payload.assignedLawyerId !== undefined) body.assigned_lawyer_id = payload.assignedLawyerId;
  if (payload.assignedCourtRepresentativeId !== undefined) {
    body.assigned_court_representative_id = payload.assignedCourtRepresentativeId;
  }

  const raw = await apiRequest<unknown>(CASE_ENDPOINTS.assignments(id), {
    method: "PATCH",
    body,
  });
  return toLegalCase(legalCaseSchema.parse(raw));
}

/**
 * Archive a case (soft delete).
 *
 * Returns the updated record rather than nothing, so a caller can reflect the
 * new status without a follow-up fetch.
 */
export async function archiveCase(id: string): Promise<LegalCase> {
  const raw = await apiRequest<unknown>(CASE_ENDPOINTS.detail(id), { method: "DELETE" });
  return toLegalCase(legalCaseSchema.parse(raw));
}

/** Restore an archived case to the active workload. */
export async function restoreCase(id: string): Promise<LegalCase> {
  return updateCase(id, { status: "open" });
}
