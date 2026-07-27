/**
 * Access-rule evaluation.
 *
 * The one place in the frontend that decides whether a subject satisfies an
 * {@link AccessRule}. The permission hook, the role hook, `Protected`,
 * `ProtectedRoute`, and the sidebar all delegate here, so a requirement is
 * interpreted identically everywhere and there is no second copy of the logic to
 * fall out of step.
 *
 * These are pure functions over data the API supplied — never a security
 * boundary. Hiding a button does not protect the endpoint behind it; the API
 * authorizes every request independently (see `apps/api/api/authorization.py`).
 * This layer exists so users are not shown actions that would only fail.
 */

import type { AccessRule, AccessSubject, Permission } from "@/types/authorization";
import type { UserRole } from "@/types/user";

/** Whether `granted` contains `permission`. */
export function hasPermission(
  granted: readonly Permission[],
  permission: Permission,
): boolean {
  return granted.includes(permission);
}

/** Whether `granted` contains at least one of `permissions`. */
export function hasAnyPermission(
  granted: readonly Permission[],
  permissions: readonly Permission[],
): boolean {
  return permissions.some((permission) => granted.includes(permission));
}

/** Whether `granted` contains every one of `permissions`. */
export function hasAllPermissions(
  granted: readonly Permission[],
  permissions: readonly Permission[],
): boolean {
  return permissions.every((permission) => granted.includes(permission));
}

/** Whether `role` is one of `roles`. */
export function hasRole(role: UserRole, roles: readonly UserRole[]): boolean {
  return roles.includes(role);
}

/**
 * Evaluate an access rule against a subject.
 *
 * Every clause present must pass. An empty rule is satisfied by any subject,
 * which is how "authenticated is enough" is expressed — the caller has already
 * been resolved to a real session by the time this runs.
 *
 * An empty `anyOf` or `allOf` array is treated as a rule that grants nothing:
 * it almost always means a requirement was built dynamically and came out empty,
 * and denying is the safe reading.
 */
export function isAllowed(rule: AccessRule, subject: AccessSubject): boolean {
  const { permission, anyOf, allOf, roles } = rule;

  if (roles && !hasRole(subject.role, roles)) return false;
  if (permission && !hasPermission(subject.permissions, permission)) return false;
  if (anyOf && !(anyOf.length > 0 && hasAnyPermission(subject.permissions, anyOf))) return false;
  if (allOf && !(allOf.length > 0 && hasAllPermissions(subject.permissions, allOf))) return false;

  return true;
}
