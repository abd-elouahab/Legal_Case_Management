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
  activateUser,
  createUser,
  deactivateUser,
  fetchUser,
  fetchUsers,
  resetUserPassword,
  updateUser,
} from "@/lib/api/users";
import { ApiError, NetworkError } from "@/lib/api/errors";
import type { ManagedUser } from "@/types/user";
import type {
  CreateUserPayload,
  PasswordResetResult,
  UpdateUserPayload,
  UserListQuery,
  UserPage,
} from "@/types/user-management";

/**
 * Server state for User Management.
 *
 * TanStack Query per `architecture.md`: the directory is server state, so it is
 * cached and invalidated rather than mirrored into a client store. Every mutation
 * invalidates the list, which is what keeps a table consistent with a change made
 * from a details page, a dialog, or a row action without any of them knowing
 * about the others.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the users API.
 */

/**
 * Query keys.
 *
 * The list key includes the full query object, so changing a filter is a
 * different cache entry rather than a refetch that discards the previous page —
 * which is what makes paging back and forth instant.
 */
export const userKeys = {
  all: ["users"] as const,
  lists: () => [...userKeys.all, "list"] as const,
  list: (query: UserListQuery) => [...userKeys.lists(), query] as const,
  details: () => [...userKeys.all, "detail"] as const,
  detail: (id: string) => [...userKeys.details(), id] as const,
};

/**
 * Translate a failure into a message safe to show an administrator.
 *
 * Branches on the API's machine-readable `code` rather than on message text,
 * which is localizable and may change.
 */
export function userErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "email_already_exists":
        return "A user with this email address already exists.";
      case "user_not_found":
        return "This user no longer exists. Refresh the list and try again.";
      case "cannot_modify_own_account":
        return "You cannot change your own role or account status.";
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
  first_name: "firstName",
  last_name: "lastName",
  email: "email",
  phone: "phone",
  password: "password",
  role: "role",
  status: "status",
};

export function fieldErrors(error: unknown): Record<string, string> {
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
 * One page of the user directory.
 *
 * `placeholderData` keeps the previous page on screen while the next one loads,
 * so paging or filtering does not blank the table out and shift the layout.
 */
export function useUsers(
  query: UserListQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<UserPage, unknown> {
  return useQuery({
    queryKey: userKeys.list(query),
    queryFn: () => fetchUsers(query),
    enabled: options.enabled ?? true,
    placeholderData: (previous) => previous,
  });
}

/** One user's complete record. */
export function useUser(
  id: string,
  options: { enabled?: boolean } = {},
): UseQueryResult<ManagedUser, unknown> {
  return useQuery({
    queryKey: userKeys.detail(id),
    queryFn: () => fetchUser(id),
    enabled: (options.enabled ?? true) && Boolean(id),
  });
}

// --------------------------------------------------------------------------- //
// Mutations
// --------------------------------------------------------------------------- //

/**
 * Invalidate everything the directory shows.
 *
 * Deliberately broad: a role change alters a user's permissions, a status change
 * alters which filters they fall under, and a rename alters their sort position.
 * Reconciling each of those by hand would be an ongoing source of stale rows.
 */
function useInvalidateUsers(): () => Promise<void> {
  const queryClient = useQueryClient();

  return React.useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: userKeys.all });
  }, [queryClient]);
}

export function useCreateUser(): UseMutationResult<ManagedUser, unknown, CreateUserPayload> {
  const invalidate = useInvalidateUsers();

  return useMutation({
    mutationFn: (payload: CreateUserPayload) => createUser(payload),
    onSuccess: invalidate,
  });
}

export function useUpdateUser(): UseMutationResult<
  ManagedUser,
  unknown,
  { id: string; payload: UpdateUserPayload }
> {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateUsers();

  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: UpdateUserPayload }) =>
      updateUser(id, payload),
    onSuccess: async (user) => {
      // Seed the detail cache with the record the server just returned, so a
      // details page reflects the edit before its refetch lands.
      queryClient.setQueryData(userKeys.detail(user.id), user);
      await invalidate();
    },
  });
}

export function useDeactivateUser(): UseMutationResult<ManagedUser, unknown, string> {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateUsers();

  return useMutation({
    mutationFn: (id: string) => deactivateUser(id),
    onSuccess: async (user) => {
      queryClient.setQueryData(userKeys.detail(user.id), user);
      await invalidate();
    },
  });
}

export function useActivateUser(): UseMutationResult<ManagedUser, unknown, string> {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateUsers();

  return useMutation({
    mutationFn: (id: string) => activateUser(id),
    onSuccess: async (user) => {
      queryClient.setQueryData(userKeys.detail(user.id), user);
      await invalidate();
    },
  });
}

export function useResetUserPassword(): UseMutationResult<PasswordResetResult, unknown, string> {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateUsers();

  return useMutation({
    mutationFn: (id: string) => resetUserPassword(id),
    onSuccess: async (result) => {
      queryClient.setQueryData(userKeys.detail(result.user.id), result.user);
      await invalidate();
    },
  });
}
