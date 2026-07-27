"use client";

import * as React from "react";
import { usePathname } from "next/navigation";

import { ProtectedRoute } from "@/components/auth/protected-route";
import { accessRuleForPath } from "@/lib/authorization/routes";

/**
 * Applies the centrally-declared permission for the current route.
 *
 * Sits in the protected layout, so every page under `(protected)` is authorized
 * by construction — there is no per-page check to forget, and no page has to
 * name a permission itself. The requirement comes from `config/navigation.ts`
 * via {@link accessRuleForPath}; routes with no rule render normally, which is
 * how the Dashboard and the Unauthorized page stay reachable for all roles.
 *
 * Pages needing a finer rule than their route's (a tab only administrators may
 * open, say) compose `ProtectedRoute` or `Protected` directly on top of this.
 */
export function RouteGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const rule = accessRuleForPath(pathname);

  if (rule === null) return <>{children}</>;

  return <ProtectedRoute {...rule}>{children}</ProtectedRoute>;
}
