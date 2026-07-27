/**
 * Tests for the change-password flow, including the session revocation it
 * triggers and the token swap that keeps the current device signed in.
 */

import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useChangePassword } from "@/hooks/use-change-password";
import { fetchCurrentUser } from "@/lib/api/auth";
import { getAccessToken, setAccessToken } from "@/lib/api/token-store";
import {
  changePasswordFormSchema,
  MAX_PASSWORD_BYTES,
  MIN_PASSWORD_LENGTH,
} from "@/lib/validation/auth";
import { errorEnvelope, mockFetch, TEST_USER, tokenResponse } from "./helpers";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

const PAYLOAD = { currentPassword: "old-password", newPassword: "brand-new-password" };

function successResponse(overrides: Record<string, unknown> = {}) {
  return {
    ...tokenResponse({ access_token: "post-change-token" }),
    message: "Password changed successfully. Other devices have been signed out.",
    sessions_revoked: true,
    ...overrides,
  };
}

describe("useChangePassword", () => {
  it("reports success and the server's message", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/change-password": { body: successResponse() } });

    const { result } = renderHook(() => useChangePassword());

    let outcome = false;
    await act(async () => {
      outcome = await result.current.submit(PAYLOAD);
    });

    expect(outcome).toBe(true);
    expect(result.current.successMessage).toContain("Password changed successfully");
    expect(result.current.error).toBeNull();
  });

  it("tells the user other devices were signed out", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/change-password": { body: successResponse() } });

    const { result } = renderHook(() => useChangePassword());
    await act(async () => {
      await result.current.submit(PAYLOAD);
    });

    expect(result.current.successMessage).toMatch(/other devices/i);
  });

  it("keeps this device signed in with the replacement token", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/change-password": { body: successResponse() } });

    const { result } = renderHook(() => useChangePassword());
    await act(async () => {
      await result.current.submit(PAYLOAD);
    });

    expect(getAccessToken()).toBe("post-change-token");
  });

  it("leaves subsequent requests working", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/change-password": { body: successResponse() } });

    const { result } = renderHook(() => useChangePassword());
    await act(async () => {
      await result.current.submit(PAYLOAD);
    });

    const { requests } = mockFetch({ "/auth/me": { body: TEST_USER } });
    await fetchCurrentUser();

    expect(requests[0]?.headers.Authorization).toBe("Bearer post-change-token");
  });

  it("reports a wrong current password", async () => {
    setAccessToken("access-token-1");
    mockFetch({
      "/auth/change-password": { status: 400, body: errorEnvelope("invalid_password") },
    });

    const { result } = renderHook(() => useChangePassword());

    let outcome = true;
    await act(async () => {
      outcome = await result.current.submit({ ...PAYLOAD, currentPassword: "wrong" });
    });

    expect(outcome).toBe(false);
    expect(result.current.error).toBe("Your current password is incorrect.");
    expect(result.current.successMessage).toBeNull();
  });

  it("keeps the current token when the change fails", async () => {
    setAccessToken("access-token-1");
    mockFetch({
      "/auth/change-password": { status: 400, body: errorEnvelope("invalid_password") },
    });

    const { result } = renderHook(() => useChangePassword());
    await act(async () => {
      await result.current.submit(PAYLOAD);
    });

    expect(getAccessToken()).toBe("access-token-1");
  });

  it("explains an expired session", async () => {
    setAccessToken("access-token-1");
    mockFetch({
      "/auth/change-password": { status: 401, body: errorEnvelope("token_expired") },
    });

    const { result } = renderHook(() => useChangePassword());
    await act(async () => {
      await result.current.submit(PAYLOAD);
    });

    expect(result.current.error).toMatch(/session has expired/i);
  });

  it("surfaces a server-side validation error", async () => {
    setAccessToken("access-token-1");
    mockFetch({
      "/auth/change-password": {
        status: 422,
        body: {
          ...errorEnvelope("validation_error", "Request validation failed."),
          details: [{ field: "new_password", message: "Password is too short." }],
        },
      },
    });

    const { result } = renderHook(() => useChangePassword());
    await act(async () => {
      await result.current.submit(PAYLOAD);
    });

    expect(result.current.error).toBe("Password is too short.");
  });

  it("reports an unreachable server", async () => {
    setAccessToken("access-token-1");
    mockFetch({ "/auth/change-password": { networkError: true } });

    const { result } = renderHook(() => useChangePassword());
    await act(async () => {
      await result.current.submit(PAYLOAD);
    });

    expect(result.current.error).toMatch(/unable to reach the server/i);
  });

  it("clears state on reset", async () => {
    setAccessToken("access-token-1");
    mockFetch({
      "/auth/change-password": { status: 400, body: errorEnvelope("invalid_password") },
    });

    const { result } = renderHook(() => useChangePassword());
    await act(async () => {
      await result.current.submit(PAYLOAD);
    });
    expect(result.current.error).not.toBeNull();

    act(() => result.current.reset());

    expect(result.current.error).toBeNull();
    expect(result.current.successMessage).toBeNull();
  });
});

describe("changePasswordFormSchema", () => {
  const valid = {
    currentPassword: "old-password",
    newPassword: "brand-new-password",
    confirmPassword: "brand-new-password",
  };

  it("accepts a valid change", () => {
    expect(changePasswordFormSchema.safeParse(valid).success).toBe(true);
  });

  it("requires the current password", () => {
    const result = changePasswordFormSchema.safeParse({ ...valid, currentPassword: "" });

    expect(result.success).toBe(false);
  });

  it("enforces the minimum length", () => {
    const short = "a".repeat(MIN_PASSWORD_LENGTH - 1);
    const result = changePasswordFormSchema.safeParse({
      ...valid,
      newPassword: short,
      confirmPassword: short,
    });

    expect(result.success).toBe(false);
  });

  it("rejects a mismatched confirmation", () => {
    const result = changePasswordFormSchema.safeParse({
      ...valid,
      confirmPassword: "something-else",
    });

    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((issue) => issue.path.includes("confirmPassword"))).toBe(true);
    }
  });

  it("rejects reusing the current password", () => {
    const result = changePasswordFormSchema.safeParse({
      currentPassword: "same-password-here",
      newPassword: "same-password-here",
      confirmPassword: "same-password-here",
    });

    expect(result.success).toBe(false);
  });

  it("enforces bcrypt's byte limit, not a character count", () => {
    // 25 three-byte characters is 75 bytes but only 25 characters.
    const multibyte = "€".repeat(25);
    expect(multibyte.length).toBeLessThan(MAX_PASSWORD_BYTES);

    const result = changePasswordFormSchema.safeParse({
      ...valid,
      newPassword: multibyte,
      confirmPassword: multibyte,
    });

    expect(result.success).toBe(false);
  });
});
