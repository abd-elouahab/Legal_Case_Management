/**
 * Global test setup.
 *
 * Registers jest-dom matchers, pins the API base URL so request assertions are
 * stable, and resets the module-level auth state between tests (the token store
 * and the shared in-flight refresh are singletons by design).
 */

import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

import { resetRefreshState } from "@/lib/api/client";
import { resetTokenStore } from "@/lib/api/token-store";
import { useSessionStore } from "@/stores/session-store";

process.env.NEXT_PUBLIC_API_BASE_URL = "http://api.test";
process.env.NEXT_PUBLIC_REFRESH_COOKIE_NAME = "legal_platform_refresh";

beforeEach(() => {
  resetTokenStore();
  resetRefreshState();
  useSessionStore.setState({ user: null, status: "loading" });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
