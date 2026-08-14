/**
 * User & session types.
 *
 * These mirror the three platform roles from `architecture.md` and the API's
 * user payload (`apps/api/schemas/user.py`).
 */

import type { Permission } from "@/types/authorization";

/** Platform roles. Union type instead of magic strings (per code standards). */
export const USER_ROLES = ["administrator", "lawyer", "court"] as const;
export type UserRole = (typeof USER_ROLES)[number];

/**
 * Account lifecycle states. Only `active` may sign in; `inactive` is also the
 * soft-delete state, and `suspended` marks an account blocked pending review.
 */
export const USER_STATUSES = ["active", "inactive", "suspended"] as const;
export type UserStatus = (typeof USER_STATUSES)[number];

export interface SessionUser {
  id: string;
  name: string;
  email: string;
  role: UserRole;
  /**
   * Effective permissions, as computed by the API from the user's role.
   *
   * Delivered with every user payload so the UI can hide inaccessible
   * navigation and actions without a second round trip. The API remains the
   * authority — this list decides what is *shown*, never what is *allowed*.
   */
  permissions: readonly Permission[];
  /**
   * Whether an administrator reset this user's password, so they must choose a
   * new one before continuing.
   */
  mustChangePassword: boolean;
  /** Optional avatar URL; the UI falls back to initials when absent. */
  avatarUrl?: string;
}

/**
 * A user record as shown in the administrative directory.
 *
 * Richer than {@link SessionUser}, which describes only *the signed-in user*.
 * This is what `GET /users` and `GET /users/{id}` return: the full profile, the
 * account's lifecycle state, and the audit trail.
 */
export interface ManagedUser {
  id: string;
  email: string;
  firstName: string;
  lastName: string;
  /** Display name, composed by the API from the name parts. */
  fullName: string;
  phone: string | null;
  profileImage: string | null;
  role: UserRole;
  status: UserStatus;
  /** Whether the account may authenticate (`status === "active"`). */
  isActive: boolean;
  mustChangePassword: boolean;
  /** ISO-8601 timestamps, formatted for display at the point of use. */
  lastLoginAt: string | null;
  createdAt: string;
  updatedAt: string;
  createdBy: string | null;
  updatedBy: string | null;
  permissions: readonly Permission[];
}

/**
 * Where the words for a role and a status live.
 *
 * `users.roles` and `users.statuses` in the message catalogues — not here, since
 * `21-localization.md`. A module constant cannot read the reader's language, so a
 * badge built from one stayed English on an Arabic screen, which is exactly what
 * `code-standards.md` forbids. The *values* stay here because a role is a fact
 * about the platform's authorization model and a translation is not.
 *
 * Note the catalogue key for the court role is `court_representative` rather than
 * `court`: the API's `UserRole` value is `court`, and the catalogue was written
 * against the wire value the *notification* and *timeline* payloads carry. Call
 * sites map through {@link roleMessageKey} rather than each remembering.
 */
export const ROLE_NAMESPACE = "users.roles";
export const USER_STATUS_NAMESPACE = "users.statuses";

/** The catalogue key for a role value. */
export function roleMessageKey(role: UserRole | string): string {
  return role === "court" ? "court_representative" : role;
}
