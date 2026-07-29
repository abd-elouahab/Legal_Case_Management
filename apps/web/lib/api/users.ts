/**
 * User Management API calls.
 *
 * Thin, typed wrappers over the `/users/*` endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape —
 * and a backend change that alters the payload fails here, loudly, instead of
 * surfacing as `undefined` in a table cell.
 */

import { apiRequest } from "@/lib/api/client";
import { USER_ENDPOINTS } from "@/lib/api/config";
import {
  managedUserSchema,
  passwordResetSchema,
  userPageSchema,
} from "@/lib/validation/user";
import type { ManagedUser } from "@/types/user";
import type {
  CreateUserPayload,
  PasswordResetResult,
  UpdateUserPayload,
  UserListQuery,
  UserPage,
} from "@/types/user-management";

type ManagedUserWire = ReturnType<typeof managedUserSchema.parse>;

/** Map one API user record onto the app's {@link ManagedUser}. */
function toManagedUser(payload: ManagedUserWire): ManagedUser {
  return {
    id: payload.id,
    email: payload.email,
    firstName: payload.first_name,
    lastName: payload.last_name,
    fullName: payload.full_name,
    phone: payload.phone,
    profileImage: payload.profile_image,
    role: payload.role,
    status: payload.status,
    isActive: payload.is_active,
    mustChangePassword: payload.must_change_password,
    lastLoginAt: payload.last_login_at,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
    createdBy: payload.created_by,
    updatedBy: payload.updated_by,
    permissions: payload.permissions,
  };
}

/**
 * Build the query string for a list request.
 *
 * Empty search and "any" filters are omitted rather than sent as blanks, so the
 * request URL reflects what is actually being asked — which also makes the query
 * a stable cache key for TanStack Query.
 */
export function buildUserListParams(query: UserListQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
    sort_by: query.sortBy,
    sort_order: query.sortOrder,
  });

  if (query.search.trim()) params.set("search", query.search.trim());
  if (query.role) params.set("role", query.role);
  if (query.status) params.set("status", query.status);

  return params.toString();
}

/** Fetch one page of the user directory. */
export async function fetchUsers(query: UserListQuery): Promise<UserPage> {
  const raw = await apiRequest<unknown>(`${USER_ENDPOINTS.list}?${buildUserListParams(query)}`);
  const data = userPageSchema.parse(raw);

  return {
    items: data.items.map(toManagedUser),
    totalRecords: data.total_records,
    page: data.page,
    pageSize: data.page_size,
    totalPages: data.total_pages,
  };
}

/** Fetch one user's complete record. */
export async function fetchUser(id: string): Promise<ManagedUser> {
  const raw = await apiRequest<unknown>(USER_ENDPOINTS.detail(id));
  return toManagedUser(managedUserSchema.parse(raw));
}

/** Create a user. */
export async function createUser(payload: CreateUserPayload): Promise<ManagedUser> {
  const raw = await apiRequest<unknown>(USER_ENDPOINTS.create, {
    method: "POST",
    body: {
      email: payload.email,
      first_name: payload.firstName,
      last_name: payload.lastName,
      password: payload.password,
      phone: payload.phone ?? null,
      role: payload.role,
      status: payload.status,
      must_change_password: payload.mustChangePassword ?? false,
    },
  });

  return toManagedUser(managedUserSchema.parse(raw));
}

/**
 * Apply a partial update.
 *
 * Only the keys present in `payload` are sent, so an omitted field is left
 * untouched by the server while an explicit `null` clears it — the distinction
 * the PATCH contract rests on.
 */
export async function updateUser(id: string, payload: UpdateUserPayload): Promise<ManagedUser> {
  const body: Record<string, unknown> = {};

  if (payload.email !== undefined) body.email = payload.email;
  if (payload.firstName !== undefined) body.first_name = payload.firstName;
  if (payload.lastName !== undefined) body.last_name = payload.lastName;
  if (payload.phone !== undefined) body.phone = payload.phone;
  if (payload.role !== undefined) body.role = payload.role;
  if (payload.status !== undefined) body.status = payload.status;

  const raw = await apiRequest<unknown>(USER_ENDPOINTS.detail(id), { method: "PATCH", body });
  return toManagedUser(managedUserSchema.parse(raw));
}

/**
 * Deactivate a user (soft delete).
 *
 * Returns the updated record rather than nothing, so a caller can reflect the
 * new status without a follow-up fetch.
 */
export async function deactivateUser(id: string): Promise<ManagedUser> {
  const raw = await apiRequest<unknown>(USER_ENDPOINTS.detail(id), { method: "DELETE" });
  return toManagedUser(managedUserSchema.parse(raw));
}

/** Reactivate a previously deactivated or suspended user. */
export async function activateUser(id: string): Promise<ManagedUser> {
  return updateUser(id, { status: "active" });
}

/**
 * Reset a user's password.
 *
 * The temporary password in the result is shown by the API exactly once, so the
 * caller must surface it rather than discarding it.
 */
export async function resetUserPassword(id: string): Promise<PasswordResetResult> {
  const raw = await apiRequest<unknown>(USER_ENDPOINTS.resetPassword(id), { method: "POST" });
  const data = passwordResetSchema.parse(raw);

  return {
    user: toManagedUser(data.user),
    temporaryPassword: data.temporary_password,
    mustChangePassword: data.must_change_password,
    message: data.message,
  };
}
