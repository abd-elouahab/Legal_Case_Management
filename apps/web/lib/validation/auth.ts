/**
 * Zod schemas for authentication.
 *
 * Two jobs, both required by the code standards ("validate all external input
 * using Zod"):
 *
 * 1. **Form validation** — drives the login and change-password forms through
 *    React Hook Form, mirroring the server's rules so users get immediate
 *    feedback. The server always re-validates; this is UX, not security.
 * 2. **Response validation** — API responses are external input too, so they are
 *    parsed before entering application state.
 */

import { z } from "zod";

import { PERMISSIONS } from "@/types/authorization";
import { USER_ROLES } from "@/types/user";

/** Must match `MIN_PASSWORD_LENGTH` in `apps/api/schemas/auth.py`. */
export const MIN_PASSWORD_LENGTH = 8;

/** bcrypt hashes at most 72 bytes; the API rejects anything longer. */
export const MAX_PASSWORD_BYTES = 72;

const encoder = new TextEncoder();

/** Byte length, since bcrypt's limit is bytes rather than characters. */
function byteLength(value: string): number {
  return encoder.encode(value).length;
}

// --------------------------------------------------------------------------- //
// Form schemas
// --------------------------------------------------------------------------- //

/**
 * Login form. Only presence is checked on the password: enforcing the length
 * policy here would block existing shorter passwords and reveal the policy.
 */
export const loginFormSchema = z.object({
  email: z
    .string()
    .min(1, "Email is required.")
    .email("Enter a valid email address.")
    .transform((value) => value.trim().toLowerCase()),
  password: z.string().min(1, "Password is required."),
});

export type LoginFormValues = z.input<typeof loginFormSchema>;

export const changePasswordFormSchema = z
  .object({
    currentPassword: z.string().min(1, "Current password is required."),
    newPassword: z
      .string()
      .min(MIN_PASSWORD_LENGTH, `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`)
      .refine(
        (value) => byteLength(value) <= MAX_PASSWORD_BYTES,
        `Password must not exceed ${MAX_PASSWORD_BYTES} bytes.`,
      ),
    confirmPassword: z.string().min(1, "Confirm your new password."),
  })
  .refine((values) => values.newPassword === values.confirmPassword, {
    message: "Passwords do not match.",
    path: ["confirmPassword"],
  })
  .refine((values) => values.newPassword !== values.currentPassword, {
    message: "New password must be different from the current password.",
    path: ["newPassword"],
  });

export type ChangePasswordFormValues = z.input<typeof changePasswordFormSchema>;

// --------------------------------------------------------------------------- //
// Response schemas
// --------------------------------------------------------------------------- //

/** A user as returned by `GET /auth/me` and nested in the token response. */
export const userResponseSchema = z.object({
  id: z.string(),
  email: z.string(),
  full_name: z.string(),
  role: z.enum(USER_ROLES),
  is_active: z.boolean(),
  /**
   * Effective permissions for the user's role, computed by the API.
   *
   * Unknown identifiers are dropped rather than failing the whole response: a
   * backend that has added a permission this build does not know about must not
   * be able to break sign-in. Anything the client cannot name, it cannot gate
   * on, so ignoring it is also the safe outcome.
   */
  permissions: z
    .array(z.string())
    .default([])
    .transform((values) =>
      values.filter((value): value is (typeof PERMISSIONS)[number] =>
        (PERMISSIONS as readonly string[]).includes(value),
      ),
    ),
  /**
   * Set after an administrator resets the password. Defaults to `false` so a
   * payload from an older backend still parses.
   */
  must_change_password: z.boolean().default(false),
  last_login_at: z.string().nullable().optional(),
  created_at: z.string().optional(),
});

export const tokenResponseSchema = z.object({
  access_token: z.string().min(1),
  token_type: z.string(),
  expires_in: z.number().int().positive(),
  user: userResponseSchema,
  // The refresh token also arrives in the body, but the browser client
  // deliberately ignores it in favour of the httpOnly cookie.
  refresh_token: z.string().optional(),
});

/** The API's standard error envelope. */
export const errorResponseSchema = z.object({
  error: z.string(),
  message: z.string(),
  request_id: z.string().nullable().optional(),
  details: z
    .array(z.object({ field: z.string().nullable().optional(), message: z.string() }))
    .optional(),
});

export const messageResponseSchema = z.object({ message: z.string() });

/**
 * Response to a password change. Extends the token response because the change
 * revokes all sessions and issues a replacement pair for the current one.
 */
export const changePasswordResponseSchema = tokenResponseSchema.extend({
  message: z.string(),
  sessions_revoked: z.boolean().default(true),
});
