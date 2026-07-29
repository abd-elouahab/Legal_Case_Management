/**
 * Case types.
 *
 * Mirror the API's case payload (`apps/api/schemas/case.py`) and the lifecycle
 * defined in `apps/api/models/case.py`. Union types rather than magic strings,
 * per the code standards: referencing a status the platform does not define is a
 * compile error.
 */

import type { UserRole } from "@/types/user";

/**
 * Lifecycle states, in the order a case moves through them.
 *
 * The order is the natural progression, not a ranking — nothing sorts on it. It
 * is used to render status menus in a sensible sequence.
 */
export const CASE_STATUSES = [
  "draft",
  "open",
  "in_progress",
  "waiting_for_hearing",
  "closed",
  "archived",
] as const;
export type CaseStatus = (typeof CASE_STATUSES)[number];

/** Priorities, from least to most urgent. Mirrors `PRIORITY_RANK` on the API. */
export const CASE_PRIORITIES = ["low", "medium", "high", "urgent"] as const;
export type CasePriority = (typeof CASE_PRIORITIES)[number];

/**
 * A person referenced by a case: an assignee or an auditor.
 *
 * Deliberately narrower than {@link ManagedUser} — a lawyer may read a case
 * without holding `users:view`, so the API serves only what a case screen shows.
 */
export interface CaseUserSummary {
  id: string;
  fullName: string;
  email: string;
  role: UserRole;
}

/** A case as returned by `GET /cases` and `GET /cases/{id}`. */
export interface LegalCase {
  id: string;
  caseNumber: string;
  title: string;
  description: string | null;
  category: string | null;
  status: CaseStatus;
  priority: CasePriority;
  courtName: string | null;
  /** ISO-8601 calendar dates (`YYYY-MM-DD`), formatted for display at use. */
  filingDate: string | null;
  nextHearingDate: string | null;

  assignedLawyerId: string | null;
  assignedCourtRepresentativeId: string | null;
  assignedLawyer: CaseUserSummary | null;
  assignedCourtRepresentative: CaseUserSummary | null;

  createdBy: string | null;
  updatedBy: string | null;
  creator: CaseUserSummary | null;
  updater: CaseUserSummary | null;

  /** ISO-8601 timestamps. */
  createdAt: string;
  updatedAt: string;

  isArchived: boolean;
  /**
   * Statuses this case may legally move to, served by the API.
   *
   * Taken from the server rather than re-derived here: the transition rules live
   * in `apps/api/core/cases.py`, and a second copy in the browser would be one
   * more thing to keep in step — and the one the user sees when they disagree.
   */
  allowedTransitions: readonly CaseStatus[];
}

/** Human-readable status labels (future: i18n keys). */
export const CASE_STATUS_LABELS: Record<CaseStatus, string> = {
  draft: "Draft",
  open: "Open",
  in_progress: "In Progress",
  waiting_for_hearing: "Waiting for Hearing",
  closed: "Closed",
  archived: "Archived",
};

/** Human-readable priority labels (future: i18n keys). */
export const CASE_PRIORITY_LABELS: Record<CasePriority, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  urgent: "Urgent",
};
