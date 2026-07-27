/**
 * User & session types.
 *
 * These mirror the three platform roles from `architecture.md` and the API's
 * user payload (`apps/api/schemas/user.py`). The role is *identity metadata*
 * only — it is displayed in the UI but grants nothing; authorization (RBAC)
 * arrives in a later spec.
 */

/** Platform roles. Union type instead of magic strings (per code standards). */
export const USER_ROLES = ["administrator", "lawyer", "court"] as const;
export type UserRole = (typeof USER_ROLES)[number];

export interface SessionUser {
  id: string;
  name: string;
  email: string;
  role: UserRole;
  /** Optional avatar URL; the UI falls back to initials when absent. */
  avatarUrl?: string;
}

/** Human-readable role labels (future: i18n keys). */
export const ROLE_LABELS: Record<UserRole, string> = {
  administrator: "Administrator",
  lawyer: "Lawyer",
  court: "Court Representative",
};
