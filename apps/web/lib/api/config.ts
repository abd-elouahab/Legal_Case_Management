/**
 * API connection configuration.
 *
 * The base URL is environment-driven (never hardcoded) so the same build works
 * against a local API, a staging deployment, or a same-origin production setup
 * behind Nginx.
 */

/** Base URL of the FastAPI backend, without a trailing slash. */
export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/+$/, "");

/** Version prefix, matching `API_V1_PREFIX` on the backend. */
export const API_V1_PREFIX = "/api/v1";

/** Absolute URL for a versioned API path (e.g. `/auth/login`). */
export function apiUrl(path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE_URL}${API_V1_PREFIX}${normalized}`;
}

/**
 * Name of the httpOnly refresh cookie set by the API.
 *
 * The value is never readable from JavaScript; the name is only needed so the
 * Next.js proxy (which runs on the server) can check for the cookie's presence
 * during route protection.
 */
export const REFRESH_COOKIE_NAME =
  process.env.NEXT_PUBLIC_REFRESH_COOKIE_NAME ?? "legal_platform_refresh";

/** Auth endpoint paths, relative to the version prefix. */
export const AUTH_ENDPOINTS = {
  login: "/auth/login",
  logout: "/auth/logout",
  refresh: "/auth/refresh",
  me: "/auth/me",
  changePassword: "/auth/change-password",
} as const;
