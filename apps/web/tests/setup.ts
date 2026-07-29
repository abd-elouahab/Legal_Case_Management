/**
 * Global test setup.
 *
 * Registers jest-dom matchers, polyfills the browser APIs jsdom lacks, pins the
 * API base URL so request assertions are stable, and resets the module-level auth
 * state between tests (the token store and the shared in-flight refresh are
 * singletons by design).
 */

import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// --------------------------------------------------------------------------- //
// jsdom polyfills
//
// jsdom implements no layout engine and only part of the Pointer Events API, so
// Radix primitives that measure or capture pointers (Select, Checkbox, Dropdown
// Menu) throw on mount or on interaction. These stubs are enough for those code
// paths — nothing under test depends on real geometry, and asserting on measured
// pixels in jsdom would be meaningless anyway.
// --------------------------------------------------------------------------- //

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}

if (typeof Element !== "undefined") {
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= () => {};
  Element.prototype.releasePointerCapture ??= () => {};
  Element.prototype.scrollIntoView ??= () => {};
}

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
