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
  /** Optional avatar URL; the UI falls back to initials when absent. */
  avatarUrl?: string;
}

/** Human-readable role labels (future: i18n keys). */
export const ROLE_LABELS: Record<UserRole, string> = {
  administrator: "Administrator",
  lawyer: "Lawyer",
  court: "Court Representative",
};
