/**
 * Tests for session management: initialization, persistence across a page
 * refresh, automatic refresh, automatic logout, and explicit sign-out.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";

import { SessionProvider } from "@/components/auth/session-provider";
import { useLogout } from "@/hooks/use-logout";
import { clearAccessToken, getAccessToken, setAccessToken } from "@/lib/api/token-store";
import { useSessionStore } from "@/stores/session-store";
import { errorEnvelope, mockFetch, TEST_SESSION_USER, TEST_USER, tokenResponse } from "./helpers";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

function renderWithProviders(ui: React.ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
    ),
  };
}

describe("session initialization and persistence", () => {
  it("restores a session from the httpOnly refresh cookie on mount", async () => {
    // This is what makes the session survive a page refresh: the access token is
    // gone from memory, but the cookie yields a new one.
    mockFetch({
      "/auth/refresh": { body: tokenResponse({ access_token: "restored-token" }) },
      "/auth/me": { body: TEST_USER },
    });

    renderWithProviders(
      <SessionProvider>
        <p>App</p>
      </SessionProvider>,
    );

    await waitFor(() =>
      expect(useSessionStore.getState().status).toBe("authenticated"),
    );
    expect(useSessionStore.getState().user).toEqual(TEST_SESSION_USER);
    expect(getAccessToken()).toBe("restored-token");
  });

  it("resolves to signed-out for a visitor with no cookie", async () => {
    mockFetch({ "/auth/refresh": { status: 401, body: errorEnvelope("missing_token") } });

    renderWithProviders(
      <SessionProvider>
        <p>App</p>
      </SessionProvider>,
    );

    await waitFor(() =>
      expect(useSessionStore.getState().status).toBe("unauthenticated"),
    );
    expect(useSessionStore.getState().user).toBeNull();
  });

  it("starts in the loading state so guards do not redirect prematurely", async () => {
    mockFetch({ "/auth/refresh": { status: 401, body: errorEnvelope("missing_token") } });

    // Before the provider mounts, the store must not claim "signed out" — that
    // would let a guard redirect a returning user mid-restore.
    expect(useSessionStore.getState().status).toBe("loading");

    renderWithProviders(
      <SessionProvider>
        <p>App</p>
      </SessionProvider>,
    );

    // Let the restore settle so the async state update happens inside act().
    await waitFor(() =>
      expect(useSessionStore.getState().status).toBe("unauthenticated"),
    );
  });

  it("resolves to signed-out rather than hanging when the network fails", async () => {
    mockFetch({ "/auth/refresh": { networkError: true } });

    renderWithProviders(
      <SessionProvider>
        <p>App</p>
      </SessionProvider>,
    );

    await waitFor(() =>
      expect(useSessionStore.getState().status).toBe("unauthenticated"),
    );
  });

  it("renders its children regardless of session outcome", async () => {
    mockFetch({ "/auth/refresh": { status: 401, body: errorEnvelope("missing_token") } });

    renderWithProviders(
      <SessionProvider>
        <p>App content</p>
      </SessionProvider>,
    );

    expect(screen.getByText("App content")).toBeInTheDocument();

    await waitFor(() =>
      expect(useSessionStore.getState().status).toBe("unauthenticated"),
    );
  });

  it("signs out when a revoked session cannot load the user", async () => {
    mockFetch({
      "/auth/refresh": { body: tokenResponse() },
      "/auth/me": { status: 401, body: errorEnvelope("invalid_token") },
    });

    renderWithProviders(
      <SessionProvider>
        <p>App</p>
      </SessionProvider>,
    );

    await waitFor(() =>
      expect(useSessionStore.getState().status).toBe("unauthenticated"),
    );
    expect(getAccessToken()).toBeNull();
  });
});

describe("automatic token refresh", () => {
  it("renews the access token shortly before it expires", async () => {
    vi.useFakeTimers();
    try {
      const { fetchMock } = mockFetch({
        "/auth/refresh": { body: tokenResponse({ access_token: "renewed-token" }) },
        "/auth/me": { body: TEST_USER },
      });

      renderWithProviders(
        <SessionProvider>
          <p>App</p>
        </SessionProvider>,
      );

      // Let the initial restore settle.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(useSessionStore.getState().status).toBe("authenticated");

      const callsAfterRestore = fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("/auth/refresh"),
      ).length;

      // Advance past the renewal point (15 min lifetime, 1 min margin).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15 * 60 * 1000);
      });

      const callsAfterRenewal = fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("/auth/refresh"),
      ).length;
      expect(callsAfterRenewal).toBeGreaterThan(callsAfterRestore);
      expect(useSessionStore.getState().status).toBe("authenticated");
    } finally {
      vi.useRealTimers();
    }
  });

  it("signs the user out when the scheduled refresh fails", async () => {
    vi.useFakeTimers();
    try {
      mockFetch({
        "/auth/refresh": [
          { body: tokenResponse() },
          { status: 401, body: errorEnvelope("invalid_token") },
        ],
        "/auth/me": { body: TEST_USER },
      });

      renderWithProviders(
        <SessionProvider>
          <p>App</p>
        </SessionProvider>,
      );

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(useSessionStore.getState().status).toBe("authenticated");

      await act(async () => {
        await vi.advanceTimersByTimeAsync(15 * 60 * 1000);
      });

      expect(useSessionStore.getState().status).toBe("unauthenticated");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("automatic logout", () => {
  it("downgrades an established session when the token is cleared", async () => {
    mockFetch({
      "/auth/refresh": { body: tokenResponse() },
      "/auth/me": { body: TEST_USER },
    });

    renderWithProviders(
      <SessionProvider>
        <p>App</p>
      </SessionProvider>,
    );
    await waitFor(() =>
      expect(useSessionStore.getState().status).toBe("authenticated"),
    );

    act(() => {
      clearAccessToken();
    });

    expect(useSessionStore.getState().status).toBe("unauthenticated");
    expect(useSessionStore.getState().user).toBeNull();
  });
});

describe("useLogout", () => {
  it("clears the session and returns to the login page", async () => {
    replace.mockClear();
    setAccessToken("access-token-1");
    useSessionStore.setState({ user: TEST_SESSION_USER, status: "authenticated" });
    mockFetch({ "/auth/logout": { body: { message: "Signed out successfully." } } });

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useLogout(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      ),
    });

    await act(async () => {
      await result.current.logout();
    });

    expect(useSessionStore.getState().status).toBe("unauthenticated");
    expect(useSessionStore.getState().user).toBeNull();
    expect(getAccessToken()).toBeNull();
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("drops cached query data so it cannot leak to the next user", async () => {
    replace.mockClear();
    setAccessToken("access-token-1");
    useSessionStore.setState({ user: TEST_SESSION_USER, status: "authenticated" });
    mockFetch({ "/auth/logout": { body: { message: "Signed out successfully." } } });

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(["cases"], [{ id: "case-1", title: "Confidential matter" }]);

    const { result } = renderHook(() => useLogout(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      ),
    });

    await act(async () => {
      await result.current.logout();
    });

    expect(queryClient.getQueryData(["cases"])).toBeUndefined();
  });

  it("still signs out locally when the server call fails", async () => {
    replace.mockClear();
    setAccessToken("access-token-1");
    useSessionStore.setState({ user: TEST_SESSION_USER, status: "authenticated" });
    mockFetch({ "/auth/logout": { status: 500, body: errorEnvelope("internal_error") } });

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useLogout(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      ),
    });

    await act(async () => {
      await result.current.logout();
    });

    expect(useSessionStore.getState().status).toBe("unauthenticated");
    expect(getAccessToken()).toBeNull();
    expect(replace).toHaveBeenCalledWith("/login");
  });
});
