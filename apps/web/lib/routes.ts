/**
 * Application route constants.
 *
 * Single source of truth for every URL path in the web app. Navigation config,
 * links, and redirects reference these constants instead of hardcoding strings
 * so routes can be renamed in one place.
 *
 * Localization note: these are URL paths (not user-facing copy) and are not
 * translated. Human-readable labels live in the navigation config.
 */

export const ROUTES = {
  home: "/",

  // Public / auth
  login: "/login",
  accessDenied: "/access-denied",

  // Protected app
  dashboard: "/dashboard",
  cases: "/cases",
  documents: "/documents",
  search: "/search",
  users: "/users",
  lawyers: "/lawyers",
  courtUpdates: "/court",
  reports: "/reports",
  notifications: "/notifications",
  aiAssistant: "/ai",
  settings: "/settings",
} as const;

export type RouteKey = keyof typeof ROUTES;
export type RoutePath = (typeof ROUTES)[RouteKey];

/**
 * Path to one user's details page.
 *
 * A function rather than a template literal at the call site, so the nesting
 * under {@link ROUTES.users} stays in this file — which is also what keeps the
 * route guard's longest-prefix match working for `/users/{id}`.
 */
export function userRoute(userId: string): string {
  return `${ROUTES.users}/${encodeURIComponent(userId)}`;
}

/**
 * Path to one case's details page.
 *
 * A function for the same reason as {@link userRoute}: the nesting under
 * {@link ROUTES.cases} stays in this file, which is what keeps the route guard's
 * longest-prefix match working for `/cases/{id}`.
 */
export function caseRoute(caseId: string): string {
  return `${ROUTES.cases}/${encodeURIComponent(caseId)}`;
}

/** Landing route once a session exists. */
export const DEFAULT_AUTHENTICATED_ROUTE = ROUTES.dashboard;

/**
 * Validate a post-login redirect target before navigating to it.
 *
 * The `?next=` parameter is attacker-controllable, so it is only accepted when it
 * is a same-origin *path*. Rejecting absolute URLs and protocol-relative paths
 * (`//evil.com`) prevents the login page from being turned into an open redirect.
 *
 * @returns the safe path, or `null` when the input cannot be trusted.
 */
export function safeRedirectTarget(target: string | null | undefined): string | null {
  if (!target) return null;
  // Must be a rooted path, and must not start "//" (protocol-relative URL).
  if (!target.startsWith("/") || target.startsWith("//")) return null;
  // Reject anything that smuggles a scheme or a backslash-based host.
  if (target.includes("\\") || /^\/[^/]*:/.test(target)) return null;
  // Never bounce back to the login page itself.
  if (target === ROUTES.login || target.startsWith(`${ROUTES.login}?`)) return null;
  return target;
}
