"use client";

import * as React from "react";

import {
  hasAllPermissions,
  hasAnyPermission,
  hasPermission,
  isAllowed,
} from "@/lib/authorization/access";
import { useSession } from "@/hooks/use-session";
import type { AccessRule, Permission } from "@/types/authorization";

/**
 * Permission hook — the frontend's entry point for "may this user do X?".
 *
 * The permission list comes from the API with the session, so this hook never
 * derives grants from the role itself; the policy lives on the server only.
 *
 * `isLoading` matters: while the session is being restored the user is `null`
 * and every check answers `false`. A caller that renders "you don't have
 * access" without consulting `isLoading` would flash that message on every page
 * reload, so components should render a pending state until the session
 * resolves.
 */
export function usePermissions(): {
  /** Everything the current user is allowed to do; empty when signed out. */
  permissions: readonly Permission[];
  /** Whether the user holds `permission`. */
  can: (permission: Permission) => boolean;
  /** Whether the user holds at least one of `permissions`. */
  canAny: (permissions: readonly Permission[]) => boolean;
  /** Whether the user holds every one of `permissions`. */
  canAll: (permissions: readonly Permission[]) => boolean;
  /** Whether the user satisfies a full {@link AccessRule}. */
  allows: (rule: AccessRule) => boolean;
  /** True while the session is still being restored. */
  isLoading: boolean;
} {
  const { user, isLoading } = useSession();

  return React.useMemo(() => {
    const permissions = user?.permissions ?? [];

    return {
      permissions,
      isLoading,
      can: (permission: Permission) => hasPermission(permissions, permission),
      canAny: (required: readonly Permission[]) => hasAnyPermission(permissions, required),
      canAll: (required: readonly Permission[]) => hasAllPermissions(permissions, required),
      allows: (rule: AccessRule) =>
        user !== null && isAllowed(rule, { role: user.role, permissions }),
    };
  }, [user, isLoading]);
}
