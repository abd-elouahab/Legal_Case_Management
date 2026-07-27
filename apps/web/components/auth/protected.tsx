"use client";

import * as React from "react";

import { usePermissions } from "@/hooks/use-permissions";
import type { AccessRule } from "@/types/authorization";

/**
 * Conditionally renders its children based on the current user's grants.
 *
 * For *fragments* of a page — an "Assign lawyer" button, an admin-only column,
 * a settings section. For whole pages use `ProtectedRoute`, which shows the
 * Unauthorized page instead of quietly rendering nothing.
 *
 * @example
 * <Protected permission={PERMISSION.casesAssign}>
 *   <AssignLawyerButton />
 * </Protected>
 *
 * Requirements combine with AND, so `<Protected permission={a} anyOf={[b, c]}>`
 * needs `a` *and* one of `b`/`c`.
 */
export interface ProtectedProps extends AccessRule {
  children: React.ReactNode;
  /** Rendered instead of `children` when the rule is not satisfied. */
  fallback?: React.ReactNode;
  /**
   * Rendered while the session is still resolving. Defaults to the fallback —
   * showing nothing — so a protected control never flashes into view before the
   * check has run.
   */
  loading?: React.ReactNode;
}

export function Protected({
  children,
  fallback = null,
  loading,
  ...rule
}: ProtectedProps) {
  const { allows, isLoading } = usePermissions();

  if (isLoading) return <>{loading ?? fallback}</>;

  return <>{allows(rule) ? children : fallback}</>;
}
