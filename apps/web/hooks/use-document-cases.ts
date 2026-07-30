"use client";

import * as React from "react";

import { usePermissions } from "@/hooks/use-permissions";
import { useCases } from "@/hooks/use-cases";
import { PERMISSION } from "@/types/authorization";
import type { LegalCase } from "@/types/case";
import { DEFAULT_CASE_LIST_QUERY } from "@/types/case-management";

/**
 * The cases a document can be filed under.
 *
 * Reads the **Case Management** list rather than introducing a second endpoint
 * for "cases I may upload to": `GET /cases` already scopes its result to the
 * caller's assignments, so a lawyer's picker contains exactly the matters they
 * are allowed to attach a document to — the same rule the upload endpoint
 * enforces, taken from the same place rather than restated.
 *
 * Gated on `cases:view`, which that endpoint requires. Every role holding
 * `documents:upload` also holds `cases:view`, so a caller who can upload can
 * always populate the picker.
 */

/** How many cases to load. Choosing a case is a picker, not a caseload page. */
const CASE_PAGE_SIZE = 100;

export interface DocumentCaseOptions {
  /** Cases the caller may file a document under, most recently updated first. */
  cases: LegalCase[];
  isLoading: boolean;
  /** Whether the caller may read the case list at all. */
  isAvailable: boolean;
}

export function useDocumentCases(options: { enabled?: boolean } = {}): DocumentCaseOptions {
  const { can, isLoading: isSessionLoading } = usePermissions();
  const isAvailable = can(PERMISSION.casesView);
  const enabled = (options.enabled ?? true) && isAvailable;

  const query = React.useMemo(
    () => ({
      ...DEFAULT_CASE_LIST_QUERY,
      pageSize: CASE_PAGE_SIZE,
      // Most recently touched first: the case someone is filing a document
      // against is almost always one they have just been working on.
      sortBy: "updated_at" as const,
      sortOrder: "desc" as const,
    }),
    [],
  );

  const { data, isLoading } = useCases(query, { enabled });

  return {
    cases: data?.items ?? [],
    isLoading: isSessionLoading || (enabled && isLoading),
    isAvailable,
  };
}
