"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { AccessDenied } from "@/components/shared/access-denied";
import { LoadingState } from "@/components/shared/loading-state";
import { usePermissions } from "@/hooks/use-permissions";
import type { AccessRule } from "@/types/authorization";

/**
 * Page-level authorization guard.
 *
 * Renders the Unauthorized page in place of the route's content when the
 * current user does not satisfy the rule. It never redirects: sending the user
 * elsewhere would hide *why* the page is unavailable, and bouncing them to a
 * page they can see makes a mistyped URL look like a broken app.
 *
 * Composes with — rather than replaces — `RequireAuth`, which answers the prior
 * question of whether there is a session at all. Ordering matters: an
 * unauthenticated visitor is sent to sign in (they may well be allowed once
 * they do); only an *authenticated* user is told they lack permission.
 *
 * Client-side only, and not a security boundary. Protected content is never
 * rendered to an unauthorized user, but the data behind it is protected by the
 * API, which authorizes every request independently.
 */
export interface ProtectedRouteProps extends AccessRule {
  children: React.ReactNode;
  /** Replaces the default Unauthorized page when the rule is not satisfied. */
  fallback?: React.ReactNode;
}

export function ProtectedRoute({ children, fallback, ...rule }: ProtectedRouteProps) {
  const { allows, isLoading } = usePermissions();
  const t = useTranslations("auth.states");

  // Deciding while the session is still resolving would flash "access denied"
  // at a user who is perfectly entitled to the page.
  if (isLoading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <LoadingState label={t("checkingAccess")} />
      </div>
    );
  }

  if (!allows(rule)) return <>{fallback ?? <AccessDenied />}</>;

  return <>{children}</>;
}
