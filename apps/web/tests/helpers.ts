/**
 * Test helpers for the frontend auth suite.
 *
 * Provides a small scripted `fetch` double so tests describe API behaviour
 * declaratively instead of hand-rolling mock responses.
 */

import { vi } from "vitest";

import { PERMISSIONS, type Permission } from "@/types/authorization";
import type { SessionUser, UserRole } from "@/types/user";

/**
 * Permissions per role, mirroring the API's policy (`apps/api/core/roles.py`).
 *
 * Only tests carry this table: the application always takes permissions from the
 * session the API delivered. Duplicating it here is what lets the fixtures
 * describe a realistic lawyer or court representative without a running backend.
 */
export const ROLE_PERMISSIONS: Record<UserRole, readonly Permission[]> = {
  administrator: PERMISSIONS,
  lawyer: [
    "cases:view",
    "documents:view",
    "documents:upload",
    "timeline:view",
    "timeline:create",
    "reports:view",
    "reports:generate",
    "ai:chat",
    "ai:generate-report",
    "notifications:view",
    "settings:view",
  ],
  court: [
    "cases:view",
    "cases:update",
    "documents:view",
    "documents:upload",
    "timeline:view",
    "timeline:create",
    "notifications:view",
    "settings:view",
  ],
};

export const TEST_USER = {
  id: "8f14e45f-ceea-467a-9f3a-1b2c3d4e5f60",
  email: "amina.benali@example.com",
  full_name: "Amina Benali",
  role: "administrator" as const,
  is_active: true,
  permissions: ROLE_PERMISSIONS.administrator,
  must_change_password: false,
  last_login_at: null,
  created_at: "2026-07-01T09:00:00Z",
};

export const TEST_SESSION_USER: SessionUser = {
  id: TEST_USER.id,
  email: TEST_USER.email,
  name: TEST_USER.full_name,
  role: TEST_USER.role,
  permissions: TEST_USER.permissions,
  mustChangePassword: false,
};

/** A signed-in user with the grants their role actually carries. */
export function sessionUserWithRole(role: UserRole): SessionUser {
  return {
    ...TEST_SESSION_USER,
    id: `user-${role}`,
    email: `${role}@example.com`,
    role,
    permissions: ROLE_PERMISSIONS[role],
  };
}

export function tokenResponse(overrides: Record<string, unknown> = {}) {
  return {
    access_token: "access-token-1",
    refresh_token: "refresh-token-1",
    token_type: "bearer",
    expires_in: 900,
    user: TEST_USER,
    ...overrides,
  };
}

export function errorEnvelope(
  code: string,
  message = "Something went wrong.",
  details: Array<{ field?: string | null; message: string }> = [],
) {
  return { error: code, message, request_id: "req-1", details };
}

// --------------------------------------------------------------------------- //
// User Management fixtures
// --------------------------------------------------------------------------- //

/** A user record in the API's wire format, as `GET /users` returns it. */
export function managedUserPayload(overrides: Record<string, unknown> = {}) {
  const role = (overrides.role as UserRole | undefined) ?? "lawyer";

  return {
    id: "11111111-1111-4111-8111-111111111111",
    email: "karim.zahra@example.com",
    first_name: "Karim",
    last_name: "Zahra",
    full_name: "Karim Zahra",
    phone: "+212 612345678",
    profile_image: null,
    role,
    status: "active",
    is_active: true,
    must_change_password: false,
    last_login_at: "2026-07-20T08:30:00Z",
    created_at: "2026-07-01T09:00:00Z",
    updated_at: "2026-07-20T08:30:00Z",
    created_by: TEST_USER.id,
    updated_by: TEST_USER.id,
    permissions: ROLE_PERMISSIONS[role],
    ...overrides,
  };
}

/** A page of users in the API's wire format. */
export function userPagePayload(
  items: Array<Record<string, unknown>> = [managedUserPayload()],
  overrides: Record<string, unknown> = {},
) {
  return {
    items,
    total_records: items.length,
    page: 1,
    page_size: 20,
    total_pages: 1,
    ...overrides,
  };
}

/** A single scripted response for one endpoint. */
export interface RouteResponse {
  status?: number;
  body?: unknown;
  /** Throw a network-level failure instead of responding. */
  networkError?: boolean;
}

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
  credentials?: RequestCredentials;
}

/**
 * Install a `fetch` double that answers based on URL substrings.
 *
 * A route value may be a single response or an array, in which case successive
 * calls to that endpoint consume it in order — that is what makes
 * refresh-then-retry sequences testable.
 */
export function mockFetch(routes: Record<string, RouteResponse | RouteResponse[]>) {
  const requests: RecordedRequest[] = [];
  const queues = new Map<string, RouteResponse[]>(
    Object.entries(routes).map(([key, value]) => [key, Array.isArray(value) ? [...value] : [value]]),
  );

  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const headers = (init?.headers ?? {}) as Record<string, string>;

    requests.push({
      url,
      method: init?.method ?? "GET",
      headers,
      body: init?.body ? JSON.parse(init.body as string) : undefined,
      credentials: init?.credentials,
    });

    const key = [...queues.keys()].find((candidate) => url.includes(candidate));
    if (!key) throw new Error(`Unexpected request in test: ${url}`);

    const queue = queues.get(key)!;
    // The last entry repeats, so tests only script the calls they care about.
    const route = queue.length > 1 ? queue.shift()! : queue[0]!;

    if (route.networkError) throw new TypeError("Failed to fetch");

    const status = route.status ?? 200;
    return new Response(route.body === undefined ? null : JSON.stringify(route.body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });

  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, requests };
}
