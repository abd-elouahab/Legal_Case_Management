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

/** User management endpoint paths, relative to the version prefix. */
export const USER_ENDPOINTS = {
  list: "/users",
  create: "/users",
  detail: (id: string) => `/users/${encodeURIComponent(id)}`,
  resetPassword: (id: string) => `/users/${encodeURIComponent(id)}/reset-password`,
} as const;

/** Case management endpoint paths, relative to the version prefix. */
export const CASE_ENDPOINTS = {
  list: "/cases",
  create: "/cases",
  detail: (id: string) => `/cases/${encodeURIComponent(id)}`,
  assignments: (id: string) => `/cases/${encodeURIComponent(id)}/assignments`,
} as const;

/**
 * Document management endpoint paths, relative to the version prefix.
 *
 * `download` and `preview` take an optional version: omitted serves the current
 * one, which is what the API does when the parameter is absent.
 */
export const DOCUMENT_ENDPOINTS = {
  list: "/documents",
  upload: "/documents/upload",
  detail: (id: string) => `/documents/${encodeURIComponent(id)}`,
  versions: (id: string) => `/documents/${encodeURIComponent(id)}/versions`,
  replace: (id: string) => `/documents/${encodeURIComponent(id)}/replace`,
  download: (id: string, version?: number) =>
    `/documents/${encodeURIComponent(id)}/download${version ? `?version=${version}` : ""}`,
  preview: (id: string, version?: number) =>
    `/documents/${encodeURIComponent(id)}/preview${version ? `?version=${version}` : ""}`,
} as const;

/**
 * Timeline endpoint paths, relative to the version prefix.
 *
 * Read-only, and the shape says so: there is no create, update, or delete path
 * because the API exposes none — events are published by the services that cause
 * them. `caseTimeline` sits under `/cases` because a timeline belongs to a case.
 */
export const TIMELINE_ENDPOINTS = {
  caseTimeline: (caseId: string) => `/cases/${encodeURIComponent(caseId)}/timeline`,
  detail: (eventId: string) => `/timeline/${encodeURIComponent(eventId)}`,
} as const;
