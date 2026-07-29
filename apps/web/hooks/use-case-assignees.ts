"use client";

import * as React from "react";

import { usePermissions } from "@/hooks/use-permissions";
import { useUsers } from "@/hooks/use-users";
import { PERMISSION } from "@/types/authorization";
import type { ManagedUser, UserRole } from "@/types/user";
import { DEFAULT_USER_LIST_QUERY } from "@/types/user-management";

/**
 * The people who can be assigned to a case.
 *
 * Reads the **User Management** directory rather than introducing a second
 * endpoint for "assignable users": the API already answers
 * `GET /users?role=lawyer&status=active`, and duplicating that would mean two
 * definitions of who is eligible — one of which would eventually be wrong.
 *
 * Gated on `users:view`, which is what that endpoint requires. Assignment itself
 * needs `cases:assign`, and every role holding it also holds `users:view`, so a
 * caller who can assign can always populate the picker. A caller who cannot is
 * never shown one, and the query is not even issued.
 */

/** How many candidates to load. Assignment is a picker, not a directory page. */
const ASSIGNEE_PAGE_SIZE = 100;

export interface CaseAssigneeOptions {
  /** Active users holding the requested role, sorted by name. */
  users: ManagedUser[];
  isLoading: boolean;
  /** Whether the caller may read the directory at all. */
  isAvailable: boolean;
}

export function useCaseAssignees(role: UserRole): CaseAssigneeOptions {
  const { can, isLoading: isSessionLoading } = usePermissions();
  const isAvailable = can(PERMISSION.usersView);

  const query = React.useMemo(
    () => ({
      ...DEFAULT_USER_LIST_QUERY,
      pageSize: ASSIGNEE_PAGE_SIZE,
      role,
      // Only active accounts: the API refuses to assign a disabled user, so
      // offering one would be offering an action that can only fail.
      status: "active" as const,
      sortBy: "name" as const,
      sortOrder: "asc" as const,
    }),
    [role],
  );

  const { data, isLoading } = useUsers(query, { enabled: isAvailable });

  return {
    users: data?.items ?? [],
    isLoading: isSessionLoading || (isAvailable && isLoading),
    isAvailable,
  };
}
