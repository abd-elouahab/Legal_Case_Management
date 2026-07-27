/**
 * Authentication DTOs.
 *
 * These mirror the API contract in `apps/api/schemas/auth.py`. They describe the
 * wire format only — runtime validation of responses lives in
 * `lib/validation/auth.ts`.
 */

import type { SessionUser } from "@/types/user";

/** Credentials submitted to `POST /auth/login`. */
export interface LoginCredentials {
  email: string;
  password: string;
}

/** Body of `PATCH /auth/change-password`. */
export interface ChangePasswordPayload {
  currentPassword: string;
  newPassword: string;
}

/**
 * Result of a successful password change.
 *
 * The change revokes every session for the user, so the API returns a
 * replacement token pair to keep *this* device signed in. Other devices must
 * authenticate again.
 */
export interface ChangePasswordResult {
  message: string;
  /** Replacement access token — the previous one is no longer valid. */
  accessToken: string;
  /** Always true: all other sessions were invalidated. */
  sessionsRevoked: boolean;
}

/** Successful response from `POST /auth/login` and `POST /auth/refresh`. */
export interface AuthSession {
  accessToken: string;
  /** Access token lifetime in seconds. */
  expiresIn: number;
  user: SessionUser;
}

/**
 * Machine-readable error codes returned by the API's error envelope.
 *
 * `token_expired` is distinct from `invalid_token` so the client knows a refresh
 * is worth attempting rather than forcing a new sign-in.
 */
export const AUTH_ERROR_CODES = [
  "invalid_credentials",
  "account_disabled",
  "missing_token",
  "invalid_token",
  "token_expired",
  "invalid_password",
  "validation_error",
  "unauthorized",
  /** Too many consecutive failed sign-ins; the account or IP is locked out. */
  "too_many_login_attempts",
] as const;

export type AuthErrorCode = (typeof AUTH_ERROR_CODES)[number];

/** Lifecycle of the client-side session, used to drive route protection. */
export type SessionStatus =
  /** The session has not been resolved yet (initial page load). */
  | "loading"
  /** A valid session exists. */
  | "authenticated"
  /** No valid session — the user must sign in. */
  | "unauthenticated";
