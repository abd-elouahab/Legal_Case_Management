import { NextResponse, type NextRequest } from "next/server";

import { REFRESH_COOKIE_NAME } from "@/lib/api/config";
import { DEFAULT_AUTHENTICATED_ROUTE, ROUTES } from "@/lib/routes";

/**
 * Request-level route protection (Next.js 16 `proxy` convention — the successor
 * to `middleware`).
 *
 * This is a **fast pre-filter**, not the security boundary. It can only observe
 * whether the httpOnly refresh cookie is *present*; it cannot verify the token,
 * because validation requires the API's signing secret. Its job is to avoid
 * shipping the app shell to an obviously-anonymous visitor and to keep a
 * signed-in user off the login page.
 *
 * Authoritative enforcement lives in two places that do verify the token:
 *
 * * `RequireAuth` on the client, which redirects when the session cannot be
 *   restored (covers revoked and expired sessions the cookie check cannot see);
 * * the API itself, which rejects every unauthenticated request — the only
 *   boundary that actually protects data.
 */

/** Routes that require a session. */
const PROTECTED_PREFIXES = [
  ROUTES.dashboard,
  ROUTES.cases,
  ROUTES.documents,
  ROUTES.lawyers,
  ROUTES.courtUpdates,
  ROUTES.reports,
  ROUTES.notifications,
  ROUTES.aiAssistant,
  ROUTES.settings,
  ROUTES.accessDenied,
];

/** Public routes an authenticated user should not land on. */
const AUTH_ONLY_PREFIXES = [ROUTES.login];

function matches(pathname: string, prefixes: readonly string[]): boolean {
  return prefixes.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const hasSessionCookie = request.cookies.has(REFRESH_COOKIE_NAME);

  // The platform has no public landing page: route `/` by session state.
  if (pathname === "/") {
    const destination = hasSessionCookie ? DEFAULT_AUTHENTICATED_ROUTE : ROUTES.login;
    return NextResponse.redirect(new URL(destination, request.url));
  }

  if (!hasSessionCookie && matches(pathname, PROTECTED_PREFIXES)) {
    const loginUrl = new URL(ROUTES.login, request.url);
    // Preserve where the user was heading so they can be returned there after
    // signing in.
    loginUrl.searchParams.set("next", `${pathname}${search}`);
    return NextResponse.redirect(loginUrl);
  }

  if (hasSessionCookie && matches(pathname, AUTH_ONLY_PREFIXES)) {
    return NextResponse.redirect(new URL(DEFAULT_AUTHENTICATED_ROUTE, request.url));
  }

  return NextResponse.next();
}

export const config = {
  /**
   * Skip Next.js internals and static assets — they never need a session check
   * and matching them would add latency to every asset request.
   */
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)"],
};
