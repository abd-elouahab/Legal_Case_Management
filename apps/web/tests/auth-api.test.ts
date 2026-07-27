/**
 * Tests for the authentication API layer: credential handling, token storage,
 * automatic refresh, and error mapping.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  changePassword,
  fetchCurrentUser,
  login,
  logout,
  restoreSession,
} from "@/lib/api/auth";
import { apiRequest, refreshAccessToken } from "@/lib/api/client";
import { ApiError, NetworkError } from "@/lib/api/errors";
import { getAccessToken, setAccessToken } from "@/lib/api/token-store";
import { errorEnvelope, mockFetch, TEST_SESSION_USER, TEST_USER, tokenResponse } from "./helpers";

const CREDENTIALS = { email: "amina.benali@example.com", password: "correct-horse-battery" };

describe("login", () => {
  it("returns the session and stores the access token in memory", async () => {
    mockFetch({ "/auth/login": { body: tokenResponse() } });

    const session = await login(CREDENTIALS);

    expect(session.user).toEqual(TEST_SESSION_USER);
    expect(session.expiresIn).toBe(900);
    expect(getAccessToken()).toBe("access-token-1");
  });

  it("posts the credentials as JSON", async () => {
    const { requests } = mockFetch({ "/auth/login": { body: tokenResponse() } });

    await login(CREDENTIALS);

    expect(requests[0]?.method).toBe("POST");
    expect(requests[0]?.body).toEqual(CREDENTIALS);
    expect(requests[0]?.url).toContain("/api/v1/auth/login");
  });

  it("includes credentials so the httpOnly refresh cookie is accepted", async () => {
    const { requests } = mockFetch({ "/auth/login": { body: tokenResponse() } });

    await login(CREDENTIALS);

    expect(requests[0]?.credentials).toBe("include");
  });

  it("sends no Authorization header", async () => {
    const { requests } = mockFetch({ "/auth/login": { body: tokenResponse() } });

    await login(CREDENTIALS);

    expect(requests[0]?.headers.Authorization).toBeUndefined();
  });

  it("never persists the token to browser storage", async () => {
    mockFetch({ "/auth/login": { body: tokenResponse() } });

    await login(CREDENTIALS);

    expect(JSON.stringify(window.localStorage)).not.toContain("access-token-1");
    expect(JSON.stringify(window.sessionStorage)).not.toContain("access-token-1");
    expect(document.cookie).not.toContain("access-token-1");
  });

  it("raises an ApiError with the server's code for bad credentials", async () => {
    mockFetch({
      "/auth/login": { status: 401, body: errorEnvelope("invalid_credentials", "Incorrect email or password.") },
    });

    await expect(login(CREDENTIALS)).rejects.toMatchObject({
      code: "invalid_credentials",
      status: 401,
    });
    expect(getAccessToken()).toBeNull();
  });

  it("surfaces a disabled account distinctly", async () => {
    mockFetch({ "/auth/login": { status: 403, body: errorEnvelope("account_disabled") } });

    await expect(login(CREDENTIALS)).rejects.toMatchObject({ code: "account_disabled" });
  });

  it("raises a NetworkError when the API is unreachable", async () => {
    mockFetch({ "/auth/login": { networkError: true } });

    await expect(login(CREDENTIALS)).rejects.toBeInstanceOf(NetworkError);
  });

  it("does not retry a failed login through the refresh path", async () => {
    const { fetchMock } = mockFetch({
      "/auth/login": { status: 401, body: errorEnvelope("invalid_credentials") },
    });

    await expect(login(CREDENTIALS)).rejects.toBeInstanceOf(ApiError);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("fetchCurrentUser", () => {
  it("sends the access token as a Bearer credential", async () => {
    setAccessToken("access-token-1");
    const { requests } = mockFetch({ "/auth/me": { body: TEST_USER } });

    const user = await fetchCurrentUser();

    expect(user).toEqual(TEST_SESSION_USER);
    expect(requests[0]?.headers.Authorization).toBe("Bearer access-token-1");
  });

  it("maps the API's snake_case payload onto the app's user shape", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/me": { body: TEST_USER } });

    const user = await fetchCurrentUser();

    expect(user.name).toBe(TEST_USER.full_name);
    expect(user).not.toHaveProperty("full_name");
  });

  it("rejects a response that does not match the expected shape", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/me": { body: { id: 1, unexpected: true } } });

    await expect(fetchCurrentUser()).rejects.toThrow();
  });
});

describe("automatic token refresh", () => {
  it("refreshes and replays the request when the access token expired", async () => {
    setAccessToken("stale-token");
    const { requests } = mockFetch({
      "/auth/me": [
        { status: 401, body: errorEnvelope("token_expired") },
        { body: TEST_USER },
      ],
      "/auth/refresh": { body: tokenResponse({ access_token: "fresh-token" }) },
    });

    const user = await fetchCurrentUser();

    expect(user).toEqual(TEST_SESSION_USER);
    expect(getAccessToken()).toBe("fresh-token");
    // The replay carries the new token, not the stale one.
    expect(requests.at(-1)?.headers.Authorization).toBe("Bearer fresh-token");
  });

  it("does not refresh for a revoked token, which is not retryable", async () => {
    setAccessToken("revoked-token");
    const { fetchMock } = mockFetch({
      "/auth/me": { status: 401, body: errorEnvelope("invalid_token") },
    });

    await expect(fetchCurrentUser()).rejects.toMatchObject({ code: "invalid_token" });

    // One attempt only: no refresh, no replay.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("clears the token when the refresh itself is rejected", async () => {
    setAccessToken("stale-token");
    mockFetch({
      "/auth/me": { status: 401, body: errorEnvelope("token_expired") },
      "/auth/refresh": { status: 401, body: errorEnvelope("invalid_token") },
    });

    await expect(fetchCurrentUser()).rejects.toBeInstanceOf(ApiError);
    expect(getAccessToken()).toBeNull();
  });

  it("shares one refresh across concurrent expired requests", async () => {
    // Refresh tokens are single-use, so parallel refreshes would sign the user
    // out. All callers must await the same in-flight request.
    setAccessToken("stale-token");
    const { fetchMock } = mockFetch({
      "/auth/me": [
        { status: 401, body: errorEnvelope("token_expired") },
        { status: 401, body: errorEnvelope("token_expired") },
        { status: 401, body: errorEnvelope("token_expired") },
        { body: TEST_USER },
      ],
      "/auth/refresh": { body: tokenResponse({ access_token: "fresh-token" }) },
    });

    await Promise.all([
      apiRequest("/auth/me").catch(() => null),
      apiRequest("/auth/me").catch(() => null),
      apiRequest("/auth/me").catch(() => null),
    ]);

    const refreshCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes("/auth/refresh"),
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it("returns null when there is no session to refresh", async () => {
    mockFetch({ "/auth/refresh": { status: 401, body: errorEnvelope("missing_token") } });

    await expect(refreshAccessToken()).resolves.toBeNull();
  });
});

describe("restoreSession", () => {
  it("rebuilds the session from the refresh cookie", async () => {
    mockFetch({
      "/auth/refresh": { body: tokenResponse({ access_token: "restored-token" }) },
      "/auth/me": { body: TEST_USER },
    });

    const session = await restoreSession();

    expect(session?.user).toEqual(TEST_SESSION_USER);
    expect(getAccessToken()).toBe("restored-token");
  });

  it("returns null for a visitor with no session", async () => {
    mockFetch({ "/auth/refresh": { status: 401, body: errorEnvelope("missing_token") } });

    await expect(restoreSession()).resolves.toBeNull();
  });

  it("sends no Authorization header when refreshing", async () => {
    const { requests } = mockFetch({
      "/auth/refresh": { body: tokenResponse() },
      "/auth/me": { body: TEST_USER },
    });

    await restoreSession();

    const refreshRequest = requests.find((request) => request.url.includes("/auth/refresh"));
    expect(refreshRequest?.headers.Authorization).toBeUndefined();
    expect(refreshRequest?.credentials).toBe("include");
  });
});

describe("logout", () => {
  it("calls the API and clears the token", async () => {
    setAccessToken("access-token-1");
    const { requests } = mockFetch({ "/auth/logout": { body: { message: "Signed out successfully." } } });

    await logout();

    expect(requests[0]?.method).toBe("POST");
    expect(requests[0]?.headers.Authorization).toBe("Bearer access-token-1");
    expect(getAccessToken()).toBeNull();
  });

  it("clears the token even when the server call fails", async () => {
    // The user asked to sign out; local state must not survive a failed request.
    setAccessToken("access-token-1");
    mockFetch({ "/auth/logout": { status: 500, body: errorEnvelope("internal_error") } });

    await expect(logout()).resolves.toBeUndefined();
    expect(getAccessToken()).toBeNull();
  });

  it("clears the token even when the network is down", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/logout": { networkError: true } });

    await expect(logout()).resolves.toBeUndefined();
    expect(getAccessToken()).toBeNull();
  });
});

describe("changePassword", () => {
  beforeEach(() => setAccessToken("access-token-1"));

  /** The API returns a replacement token pair alongside the message. */
  function changePasswordResponse(overrides: Record<string, unknown> = {}) {
    return {
      ...tokenResponse({ access_token: "post-change-token" }),
      message: "Password changed successfully. Other devices have been signed out.",
      sessions_revoked: true,
      ...overrides,
    };
  }

  it("sends the passwords in the API's expected snake_case shape", async () => {
    const { requests } = mockFetch({
      "/auth/change-password": { body: changePasswordResponse() },
    });

    const result = await changePassword({
      currentPassword: "old-password",
      newPassword: "brand-new-password",
    });

    expect(requests[0]?.method).toBe("PATCH");
    expect(requests[0]?.body).toEqual({
      current_password: "old-password",
      new_password: "brand-new-password",
    });
    expect(result.message).toContain("Password changed successfully");
  });

  it("swaps in the replacement access token", async () => {
    // Without this the next request would fail: the token used to make the change
    // is revoked by the change itself.
    mockFetch({ "/auth/change-password": { body: changePasswordResponse() } });

    await changePassword({ currentPassword: "old-password", newPassword: "brand-new-password" });

    expect(getAccessToken()).toBe("post-change-token");
  });

  it("reports that other sessions were revoked", async () => {
    mockFetch({ "/auth/change-password": { body: changePasswordResponse() } });

    const result = await changePassword({
      currentPassword: "old-password",
      newPassword: "brand-new-password",
    });

    expect(result.sessionsRevoked).toBe(true);
    expect(result.accessToken).toBe("post-change-token");
  });

  it("leaves the session usable afterwards", async () => {
    mockFetch({
      "/auth/change-password": { body: changePasswordResponse() },
      "/auth/me": { body: TEST_USER },
    });

    await changePassword({ currentPassword: "old-password", newPassword: "brand-new-password" });
    const { requests } = mockFetch({ "/auth/me": { body: TEST_USER } });
    await fetchCurrentUser();

    expect(requests[0]?.headers.Authorization).toBe("Bearer post-change-token");
  });

  it("surfaces a wrong current password", async () => {
    mockFetch({
      "/auth/change-password": { status: 400, body: errorEnvelope("invalid_password") },
    });

    await expect(
      changePassword({ currentPassword: "wrong", newPassword: "brand-new-password" }),
    ).rejects.toMatchObject({ code: "invalid_password", status: 400 });
  });

  it("keeps the existing token when the change fails", async () => {
    mockFetch({
      "/auth/change-password": { status: 400, body: errorEnvelope("invalid_password") },
    });

    await expect(
      changePassword({ currentPassword: "wrong", newPassword: "brand-new-password" }),
    ).rejects.toBeInstanceOf(ApiError);

    expect(getAccessToken()).toBe("access-token-1");
  });

  it("does not retry through the refresh path", async () => {
    // Replaying a password change is never correct, so a 401 must not be retried.
    const { fetchMock } = mockFetch({
      "/auth/change-password": { status: 401, body: errorEnvelope("token_expired") },
      "/auth/refresh": { body: tokenResponse() },
    });

    await expect(
      changePassword({ currentPassword: "old-password", newPassword: "brand-new-password" }),
    ).rejects.toBeInstanceOf(ApiError);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("rejects a response missing the replacement token", async () => {
    mockFetch({
      "/auth/change-password": { body: { message: "Password changed." } },
    });

    await expect(
      changePassword({ currentPassword: "old-password", newPassword: "brand-new-password" }),
    ).rejects.toThrow();
  });
});

describe("login throttling", () => {
  it("surfaces a 429 with the server's message", async () => {
    mockFetch({
      "/auth/login": {
        status: 429,
        body: errorEnvelope(
          "too_many_login_attempts",
          "Too many failed sign-in attempts. Try again in about 15 minutes.",
        ),
      },
    });

    await expect(login(CREDENTIALS)).rejects.toMatchObject({
      status: 429,
      code: "too_many_login_attempts",
      message: "Too many failed sign-in attempts. Try again in about 15 minutes.",
    });
  });

  it("exposes the throttled state", async () => {
    mockFetch({
      "/auth/login": { status: 429, body: errorEnvelope("too_many_login_attempts") },
    });

    await expect(login(CREDENTIALS)).rejects.toSatisfy(
      (error: unknown) => error instanceof ApiError && error.isRateLimited,
    );
  });

  it("issues no session when throttled", async () => {
    mockFetch({
      "/auth/login": { status: 429, body: errorEnvelope("too_many_login_attempts") },
    });

    await expect(login(CREDENTIALS)).rejects.toBeInstanceOf(ApiError);
    expect(getAccessToken()).toBeNull();
  });

  it("reads Retry-After into the error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(errorEnvelope("too_many_login_attempts")), {
            status: 429,
            headers: { "Content-Type": "application/json", "Retry-After": "900" },
          }),
      ),
    );

    await expect(login(CREDENTIALS)).rejects.toMatchObject({ retryAfterSeconds: 900 });
  });

  it("ignores a malformed Retry-After", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(errorEnvelope("too_many_login_attempts")), {
            status: 429,
            headers: { "Content-Type": "application/json", "Retry-After": "not-a-number" },
          }),
      ),
    );

    await expect(login(CREDENTIALS)).rejects.toMatchObject({ retryAfterSeconds: undefined });
  });
});

describe("error envelope handling", () => {
  it("does not leak an unparseable body to the user", async () => {
    setAccessToken("access-token-1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<html>Bad Gateway</html>", { status: 502 })),
    );

    await expect(fetchCurrentUser()).rejects.toMatchObject({
      code: "unexpected_error",
      message: "Something went wrong. Please try again.",
    });
  });

  it("exposes the correlation id for support", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/me": { status: 403, body: errorEnvelope("account_disabled") } });

    await expect(fetchCurrentUser()).rejects.toMatchObject({ requestId: "req-1" });
  });
});
