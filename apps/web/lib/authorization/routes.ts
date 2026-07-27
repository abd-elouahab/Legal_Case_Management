/**
 * Route → access rule resolution.
 *
 * Turns the current pathname into the {@link AccessRule} that guards it, using
 * the rules declared once in `config/navigation.ts`. Nested routes inherit their
 * section's requirement (`/cases/123/documents` is gated by `/cases`), so a new
 * page under an existing section is protected without another declaration.
 */

import { routeAccessRules } from "@/config/navigation";
import type { AccessRule } from "@/types/authorization";

/**
 * The rule guarding `pathname`, or `null` when the route has no requirement
 * beyond being signed in.
 *
 * When several rules match, the most specific (longest) path wins — so a future
 * `/cases/archive` rule can tighten access without loosening `/cases`.
 */
export function accessRuleForPath(pathname: string): AccessRule | null {
  const matches = routeAccessRules.filter(
    ({ path }) => pathname === path || pathname.startsWith(`${path}/`),
  );

  if (matches.length === 0) return null;

  return matches.reduce((mostSpecific, candidate) =>
    candidate.path.length > mostSpecific.path.length ? candidate : mostSpecific,
  ).access;
}
