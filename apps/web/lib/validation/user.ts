/**
 * Zod schemas for User Management.
 *
 * Two jobs, both required by the code standards ("validate all external input
 * using Zod"):
 *
 * 1. **Form validation** — drives the create and edit forms through React Hook
 *    Form, mirroring the server's rules so administrators get immediate
 *    feedback. The server always re-validates; this is UX, not security.
 * 2. **Response validation** — API responses are external input too, so they are
 *    parsed before entering application state.
 *
 * The rules deliberately mirror `apps/api/schemas/user.py` and
 * `apps/api/core/users.py`. Where they must agree, the API is the authority: a
 * value this schema accepts but the server rejects surfaces as a field error on
 * submit rather than as a silent inconsistency.
 */

import { z } from "zod";

import { MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH } from "@/lib/validation/auth";
import { PERMISSIONS } from "@/types/authorization";
import { SORT_ORDERS, USER_SORT_FIELDS } from "@/types/user-management";
import { USER_ROLES, USER_STATUSES } from "@/types/user";

/** Matches `MAX_NAME_LENGTH` in `apps/api/core/users.py`. */
export const MAX_NAME_LENGTH = 100;

/** Matches `MIN_PHONE_DIGITS` / `MAX_PHONE_DIGITS` in `apps/api/core/users.py`. */
const MIN_PHONE_DIGITS = 7;
const MAX_PHONE_DIGITS = 15;

/** Characters a phone number may contain; mirrors the API's pattern. */
const PHONE_ALLOWED = /^\+?[0-9 ()./-]+$/;

const encoder = new TextEncoder();

/** Byte length, since bcrypt's limit is bytes rather than characters. */
function byteLength(value: string): number {
  return encoder.encode(value).length;
}

function countDigits(value: string): number {
  return (value.match(/\d/g) ?? []).length;
}

// --------------------------------------------------------------------------- //
// Field schemas
// --------------------------------------------------------------------------- //

const namePart = (label: string) =>
  z
    .string()
    .transform((value) => value.replace(/\s+/g, " ").trim())
    .refine((value) => value.length > 0, `${label} is required.`)
    .refine(
      (value) => value.length <= MAX_NAME_LENGTH,
      `${label} must be at most ${MAX_NAME_LENGTH} characters.`,
    );

const emailField = z
  .string()
  .transform((value) => value.trim().toLowerCase())
  .refine((value) => value.length > 0, "Email is required.")
  .refine((value) => z.string().email().safeParse(value).success, "Enter a valid email address.");

/**
 * An optional phone number. A blank field means "no phone", matching the API,
 * which treats an empty string as absent rather than as a zero-length number.
 */
const optionalPhoneField = z
  .string()
  .transform((value) => value.replace(/\s+/g, " ").trim())
  .refine(
    (value) => value === "" || PHONE_ALLOWED.test(value),
    "Phone number may contain only digits, spaces, and the characters + ( ) - . /",
  )
  .refine(
    (value) =>
      value === "" ||
      (countDigits(value) >= MIN_PHONE_DIGITS && countDigits(value) <= MAX_PHONE_DIGITS),
    `Phone number must contain between ${MIN_PHONE_DIGITS} and ${MAX_PHONE_DIGITS} digits.`,
  );

const passwordField = z
  .string()
  .min(MIN_PASSWORD_LENGTH, `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`)
  .refine(
    (value) => byteLength(value) <= MAX_PASSWORD_BYTES,
    `Password must not exceed ${MAX_PASSWORD_BYTES} bytes.`,
  );

// --------------------------------------------------------------------------- //
// Form schemas
// --------------------------------------------------------------------------- //

/**
 * Fields shared by the create and edit forms.
 *
 * No `.default()` anywhere: a defaulted field makes a schema's input type differ
 * from its output type, which then has to be threaded through React Hook Form as
 * two separate generics. Every field is supplied by `defaultValues`, so the two
 * types stay identical and the forms need one type each.
 */
const sharedUserFields = {
  firstName: namePart("First name"),
  lastName: namePart("Last name"),
  email: emailField,
  phone: optionalPhoneField,
  role: z.enum(USER_ROLES, { required_error: "Select a role." }),
  status: z.enum(USER_STATUSES, { required_error: "Select a status." }),
};

/**
 * Create-user form.
 *
 * Carries the password; the edit form deliberately does not, because changing a
 * password has to revoke sessions and therefore goes through its own endpoint.
 */
export const createUserFormSchema = z.object({
  ...sharedUserFields,
  password: passwordField,
  mustChangePassword: z.boolean(),
});

export type CreateUserFormValues = z.infer<typeof createUserFormSchema>;

/** Edit-user form — the same fields minus the password. */
export const editUserFormSchema = z.object(sharedUserFields);

export type EditUserFormValues = z.infer<typeof editUserFormSchema>;

// --------------------------------------------------------------------------- //
// Response schemas
// --------------------------------------------------------------------------- //

/**
 * A user record from `GET /users` and `GET /users/{id}`.
 *
 * Unknown permission identifiers are dropped rather than failing the whole
 * response, for the same reason the auth schema does it: a backend that has
 * added a permission this build does not know about must not be able to break
 * the directory, and a name the client cannot express is one it cannot gate on.
 */
export const managedUserSchema = z.object({
  id: z.string(),
  email: z.string(),
  first_name: z.string(),
  last_name: z.string(),
  full_name: z.string(),
  phone: z.string().nullable().default(null),
  profile_image: z.string().nullable().default(null),
  role: z.enum(USER_ROLES),
  status: z.enum(USER_STATUSES),
  is_active: z.boolean(),
  must_change_password: z.boolean().default(false),
  last_login_at: z.string().nullable().default(null),
  created_at: z.string(),
  updated_at: z.string(),
  created_by: z.string().nullable().default(null),
  updated_by: z.string().nullable().default(null),
  permissions: z
    .array(z.string())
    .default([])
    .transform((values) =>
      values.filter((value): value is (typeof PERMISSIONS)[number] =>
        (PERMISSIONS as readonly string[]).includes(value),
      ),
    ),
});

export const userPageSchema = z.object({
  items: z.array(managedUserSchema),
  total_records: z.number().int().nonnegative(),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total_pages: z.number().int().nonnegative(),
});

export const passwordResetSchema = z.object({
  user: managedUserSchema,
  temporary_password: z.string().min(1),
  must_change_password: z.boolean().default(true),
  message: z.string(),
});

// --------------------------------------------------------------------------- //
// Query validation
// --------------------------------------------------------------------------- //

/**
 * The list query, validated before it becomes a query string.
 *
 * The values come from UI state rather than from a user typing a URL, but they
 * are still the input to a request — and validating here means an out-of-range
 * page from a stale link is corrected rather than sent to the API to be rejected.
 */
export const userListQuerySchema = z.object({
  page: z.coerce.number().int().min(1).catch(1),
  pageSize: z.coerce.number().int().min(1).max(100).catch(20),
  search: z.string().trim().max(200).catch(""),
  role: z.enum(USER_ROLES).nullable().catch(null),
  status: z.enum(USER_STATUSES).nullable().catch(null),
  sortBy: z.enum(USER_SORT_FIELDS).catch("created_at"),
  sortOrder: z.enum(SORT_ORDERS).catch("desc"),
});
