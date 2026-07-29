"use client";

import * as React from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  archiveCase,
  createCase,
  fetchCase,
  fetchCases,
  restoreCase,
  updateCase,
  updateCaseAssignments,
} from "@/lib/api/cases";
import { ApiError, NetworkError } from "@/lib/api/errors";
import type { LegalCase } from "@/types/case";
import type {
  CaseListQuery,
  CasePage,
  CreateCasePayload,
  UpdateCaseAssignmentsPayload,
  UpdateCasePayload,
} from "@/types/case-management";

/**
 * Server state for Case Management.
 *
 * TanStack Query per `architecture.md`: the caseload is server state, so it is
 * cached and invalidated rather than mirrored into a client store. Every
 * mutation invalidates the list, which is what keeps a table consistent with a
 * change made from a details page, a dialog, or a row action without any of them
 * knowing about the others.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the cases API.
 */

/**
 * Query keys.
 *
 * The list key includes the full query object, so changing a filter is a
 * different cache entry rather than a refetch that discards the previous page —
 * which is what makes paging back and forth instant.
 */
export const caseKeys = {
  all: ["cases"] as const,
  lists: () => [...caseKeys.all, "list"] as const,
  list: (query: CaseListQuery) => [...caseKeys.lists(), query] as const,
  details: () => [...caseKeys.all, "detail"] as const,
  detail: (id: string) => [...caseKeys.details(), id] as const,
};

/**
 * Translate a failure into a message safe to show the user.
 *
 * Branches on the API's machine-readable `code` rather than on message text,
 * which is localizable and may change. Two of these codes carry the server's own
 * message through verbatim, because only the server knows the specifics: which
 * transition was refused, and which date was out of order.
 */
export function caseErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "case_number_already_exists":
        return "A case with this case number already exists.";
      case "case_not_found":
        return "This case no longer exists. Refresh the list and try again.";
      case "invalid_case_transition":
      case "invalid_case_dates":
        return error.message;
      case "invalid_assignment":
        return (
          error.details[0]?.message ??
          "The selected user cannot be assigned to that position."
        );
      case "validation_error":
        return error.details[0]?.message ?? "Check the details you entered.";
      case "forbidden":
        return "You do not have permission to perform this action.";
      case "invalid_token":
      case "token_expired":
      case "missing_token":
        return "Your session has expired. Sign in again to continue.";
      default:
        return error.message || "Something went wrong. Please try again.";
    }
  }

  return "Something went wrong. Please try again.";
}

/**
 * Field-level errors from a 422, keyed by the app's form field names.
 *
 * Lets a form show the server's complaint next to the offending input instead of
 * as an opaque banner. The API reports snake_case wire names, so they are mapped
 * back to the camelCase names the forms use.
 */
const FIELD_NAMES: Record<string, string> = {
  case_number: "caseNumber",
  title: "title",
  description: "description",
  category: "category",
  status: "status",
  priority: "priority",
  court_name: "courtName",
  filing_date: "filingDate",
  next_hearing_date: "nextHearingDate",
  assigned_lawyer_id: "assignedLawyerId",
  assigned_court_representative_id: "assignedCourtRepresentativeId",
};

export function caseFieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError)) return {};

  const errors: Record<string, string> = {};
  for (const detail of error.details) {
    const field = detail.field ? FIELD_NAMES[detail.field] : undefined;
    if (field && !errors[field]) errors[field] = detail.message;
  }
  return errors;
}

// --------------------------------------------------------------------------- //
// Queries
// --------------------------------------------------------------------------- //

/**
 * One page of the case list.
 *
 * `placeholderData` keeps the previous page on screen while the next one loads,
 * so paging or filtering does not blank the table out and shift the layout.
 */
export function useCases(
  query: CaseListQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<CasePage, unknown> {
  return useQuery({
    queryKey: caseKeys.list(query),
    queryFn: () => fetchCases(query),
    enabled: options.enabled ?? true,
    placeholderData: (previous) => previous,
  });
}

/** One case's complete record. */
export function useCase(
  id: string,
  options: { enabled?: boolean } = {},
): UseQueryResult<LegalCase, unknown> {
  return useQuery({
    queryKey: caseKeys.detail(id),
    queryFn: () => fetchCase(id),
    enabled: (options.enabled ?? true) && Boolean(id),
  });
}

// --------------------------------------------------------------------------- //
// Mutations
// --------------------------------------------------------------------------- //

/**
 * Invalidate everything the case list shows.
 *
 * Deliberately broad: a status change alters which filters a case falls under, a
 * priority change alters its sort position, and an assignment change alters who
 * can see it at all. Reconciling each of those by hand would be an ongoing
 * source of stale rows.
 */
function useInvalidateCases(): () => Promise<void> {
  const queryClient = useQueryClient();

  return React.useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: caseKeys.all });
  }, [queryClient]);
}

/** Seed the detail cache with the record the server just returned, then invalidate. */
function useApplyCase(): (legalCase: LegalCase) => Promise<void> {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateCases();

  return React.useCallback(
    async (legalCase: LegalCase) => {
      // So a details page reflects the change before its refetch lands.
      queryClient.setQueryData(caseKeys.detail(legalCase.id), legalCase);
      await invalidate();
    },
    [queryClient, invalidate],
  );
}

export function useCreateCase(): UseMutationResult<LegalCase, unknown, CreateCasePayload> {
  const invalidate = useInvalidateCases();

  return useMutation({
    mutationFn: (payload: CreateCasePayload) => createCase(payload),
    onSuccess: invalidate,
  });
}

export function useUpdateCase(): UseMutationResult<
  LegalCase,
  unknown,
  { id: string; payload: UpdateCasePayload }
> {
  const apply = useApplyCase();

  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: UpdateCasePayload }) =>
      updateCase(id, payload),
    onSuccess: apply,
  });
}

export function useUpdateCaseAssignments(): UseMutationResult<
  LegalCase,
  unknown,
  { id: string; payload: UpdateCaseAssignmentsPayload }
> {
  const apply = useApplyCase();

  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: UpdateCaseAssignmentsPayload }) =>
      updateCaseAssignments(id, payload),
    onSuccess: apply,
  });
}

export function useArchiveCase(): UseMutationResult<LegalCase, unknown, string> {
  const apply = useApplyCase();

  return useMutation({
    mutationFn: (id: string) => archiveCase(id),
    onSuccess: apply,
  });
}

export function useRestoreCase(): UseMutationResult<LegalCase, unknown, string> {
  const apply = useApplyCase();

  return useMutation({
    mutationFn: (id: string) => restoreCase(id),
    onSuccess: apply,
  });
}
