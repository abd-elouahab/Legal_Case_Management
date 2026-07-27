/**
 * Authentication API calls.
 *
 * Thin, typed wrappers over the `/auth/*` endpoints. Every response is validated
 * with Zod and mapped from the API's snake_case wire format to the app's
 * camelCase domain types, so the rest of the frontend never sees wire shapes.
 */

import { apiRequest, refreshAccessToken } from "@/lib/api/client";
import { AUTH_ENDPOINTS } from "@/lib/api/config";
import { ApiError } from "@/lib/api/errors";
import { clearAccessToken, setAccessToken } from "@/lib/api/token-store";
import {
  changePasswordResponseSchema,
  tokenResponseSchema,
  userResponseSchema,
} from "@/lib/validation/auth";
import type {
  AuthSession,
  ChangePasswordPayload,
  ChangePasswordResult,
  LoginCredentials,
} from "@/types/auth";
import type { SessionUser } from "@/types/user";

/** Map the API's user payload onto the app's {@link SessionUser}. */
function toSessionUser(payload: {
  id: string;
  email: string;
  full_name: string;
  role: SessionUser["role"];
}): SessionUser {
  return {
    id: payload.id,
    email: payload.email,
    name: payload.full_name,
    role: payload.role,
  };
}

function parseSession(raw: unknown): AuthSession {
  const data = tokenResponseSchema.parse(raw);
  return {
    accessToken: data.access_token,
    expiresIn: data.expires_in,
    user: toSessionUser(data.user),
  };
}

/**
 * Sign in and start a session.
 *
 * On success the access token is stored in memory and the API sets the httpOnly
 * refresh cookie. The `refresh_token` in the response body is intentionally
 * ignored by this (browser) client.
 */
export async function login(credentials: LoginCredentials): Promise<AuthSession> {
  const raw = await apiRequest<unknown>(AUTH_ENDPOINTS.login, {
    method: "POST",
    body: { email: credentials.email, password: credentials.password },
    anonymous: true,
    skipAuthRefresh: true,
  });

  const session = parseSession(raw);
  setAccessToken(session.accessToken);
  return session;
}

/**
 * End the session.
 *
 * Local state is cleared even if the server call fails (already-expired token,
 * network outage) — the user asked to sign out, so the UI must comply.
 */
export async function logout(): Promise<void> {
  try {
    await apiRequest<unknown>(AUTH_ENDPOINTS.logout, {
      method: "POST",
      skipAuthRefresh: true,
    });
  } catch {
    // Intentionally ignored: sign-out must always succeed client-side.
  } finally {
    clearAccessToken();
  }
}

/** Fetch the authenticated user. */
export async function fetchCurrentUser(): Promise<SessionUser> {
  const raw = await apiRequest<unknown>(AUTH_ENDPOINTS.me);
  return toSessionUser(userResponseSchema.parse(raw));
}

/**
 * Restore a session on page load using the httpOnly refresh cookie.
 *
 * Returns `null` when there is no usable session, which is the normal case for a
 * first-time visitor and must not be treated as an error.
 */
export async function restoreSession(): Promise<AuthSession | null> {
  const token = await refreshAccessToken();
  if (!token) return null;

  try {
    const user = await fetchCurrentUser();
    return { accessToken: token, expiresIn: 0, user };
  } catch (error) {
    if (error instanceof ApiError && error.isAuthFailure) {
      clearAccessToken();
      return null;
    }
    throw error;
  }
}

/**
 * Change the authenticated user's password.
 *
 * The server revokes every session for this user, so it returns a replacement
 * token pair. The new access token is stored immediately — without that swap the
 * very next request would fail, because the token this call was made with has
 * just been invalidated. The refresh cookie is replaced by the server.
 */
export async function changePassword(
  payload: ChangePasswordPayload,
): Promise<ChangePasswordResult> {
  const raw = await apiRequest<unknown>(AUTH_ENDPOINTS.changePassword, {
    method: "PATCH",
    body: {
      current_password: payload.currentPassword,
      new_password: payload.newPassword,
    },
    // The old token dies as part of this call, so a 401 here must not trigger a
    // refresh-and-replay: replaying a password change is never correct.
    skipAuthRefresh: true,
  });

  const data = changePasswordResponseSchema.parse(raw);
  setAccessToken(data.access_token);

  return {
    message: data.message,
    accessToken: data.access_token,
    sessionsRevoked: data.sessions_revoked,
  };
}

/**
 * Refresh the access token, returning the fresh session.
 *
 * Used by the proactive refresh timer, which renews shortly before expiry so a
 * user mid-task never sees a failed request.
 */
export async function refreshSession(): Promise<string | null> {
  return refreshAccessToken();
}
