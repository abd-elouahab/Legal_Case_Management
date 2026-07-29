/**
 * User Management DTOs.
 *
 * The request and result shapes of the `/users` endpoints, in the app's
 * camelCase domain vocabulary. Mapping to and from the API's snake_case wire
 * format happens once, in `lib/api/users.ts`, so nothing above that layer ever
 * sees a wire shape.
 */

import type { ManagedUser, UserRole, UserStatus } from "@/types/user";

/** Columns the directory can be ordered by. Mirrors the API's `UserSortField`. */
export const USER_SORT_FIELDS = ["name", "email", "created_at", "last_login"] as const;
export type UserSortField = (typeof USER_SORT_FIELDS)[number];

export const SORT_ORDERS = ["asc", "desc"] as const;
export type SortOrder = (typeof SORT_ORDERS)[number];

/** Default page size, matching `DEFAULT_PAGE_SIZE` in `apps/api/schemas/user.py`. */
export const DEFAULT_PAGE_SIZE = 20;

/**
 * The full query state of the user list: search, filters, sort, and page.
 *
 * Held as one object so a change to any part can reset the page in a single
 * place — searching while on page 4 would otherwise show an empty result.
 */
export interface UserListQuery {
  page: number;
  pageSize: number;
  search: string;
  /** `null` means "any", which is how the filter Selects express "All". */
  role: UserRole | null;
  status: UserStatus | null;
  sortBy: UserSortField;
  sortOrder: SortOrder;
}

export const DEFAULT_USER_LIST_QUERY: UserListQuery = {
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  search: "",
  role: null,
  status: null,
  sortBy: "created_at",
  sortOrder: "desc",
};

/** One page of the directory, with the totals needed to render pagination. */
export interface UserPage {
  items: ManagedUser[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/** Body of `POST /users`. */
export interface CreateUserPayload {
  email: string;
  firstName: string;
  lastName: string;
  password: string;
  phone?: string | null;
  role: UserRole;
  status: UserStatus;
  mustChangePassword?: boolean;
}

/**
 * Body of `PATCH /users/{id}`.
 *
 * Every field is optional because a PATCH describes only what changes. An
 * explicit `null` on `phone` clears it; omitting the key leaves it alone.
 */
export interface UpdateUserPayload {
  email?: string;
  firstName?: string;
  lastName?: string;
  phone?: string | null;
  role?: UserRole;
  status?: UserStatus;
}

/**
 * Result of an administrator-initiated password reset.
 *
 * `temporaryPassword` is returned by the API exactly once and is not retrievable
 * afterwards, so the UI must show it until the administrator dismisses it.
 */
export interface PasswordResetResult {
  user: ManagedUser;
  temporaryPassword: string;
  mustChangePassword: boolean;
  message: string;
}
