"use client";

import * as React from "react";

import { hasRole } from "@/lib/authorization/access";
import { useSession } from "@/hooks/use-session";
import type { UserRole } from "@/types/user";

/**
 * Role hook — reads the current user's platform role.
 *
 * Deliberately offers no `isAdministrator`-style shortcuts: those bake a
 * specific role into every call site and have to be revisited whenever the role
 * model changes. Prefer {@link usePermissions} wherever a capability describes
 * the rule; reach for a role check only when the rule genuinely is about *who
 * someone is* rather than what they may do.
 */
export function useRole(): {
  /** The current user's role, or `null` when signed out or still resolving. */
  role: UserRole | null;
  /** Whether the user holds exactly `role`. */
  is: (role: UserRole) => boolean;
  /** Whether the user holds one of `roles`. */
  isAny: (roles: readonly UserRole[]) => boolean;
  /** True while the session is still being restored. */
  isLoading: boolean;
} {
  const { user, isLoading } = useSession();

  return React.useMemo(() => {
    const role = user?.role ?? null;

    return {
      role,
      isLoading,
      is: (candidate: UserRole) => role === candidate,
      isAny: (roles: readonly UserRole[]) => role !== null && hasRole(role, roles),
    };
  }, [user, isLoading]);
}
