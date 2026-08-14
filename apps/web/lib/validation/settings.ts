/**
 * Zod schemas for settings.
 *
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/settings.py`; where they must agree, the API is the
 * authority.
 *
 * **Setting keys are parsed as loose strings and the vocabularies around them are
 * not**, which is the same split `lib/validation/notification.ts` makes. A
 * `section`, a `storage`, and a `value_type` are closed sets this client
 * *branches on* — a panel per section, a control per value type — so an
 * unrecognised one is a genuine contract break. A **key** is open by design on the
 * server, so parsing it strictly here would mean a setting added server-side
 * turned somebody's settings page into a parse error rather than into a control
 * wearing its identifier.
 *
 * **A setting's `value` is `z.unknown()`, deliberately.** Its permitted shape is
 * declared by its own definition, which travels in the same response — so the
 * only schema that could validate it is one built at runtime from the payload
 * being validated. Narrowing happens where the value is *used*, against the
 * definition that describes it, and the API is the authority on what is
 * acceptable in any case: it validates every write against the same registry
 * before anything is persisted.
 */

import { z } from "zod";

import { vm } from "@/lib/validation/messages";

import {
  SETTINGS_SECTIONS,
  SETTINGS_STORAGE_KINDS,
  SETTING_VALUE_TYPES,
} from "@/types/settings";

/** Longest maintenance message the API accepts, matching `MAINTENANCE_MESSAGE_MAX_LENGTH`. */
export const MAX_MAINTENANCE_MESSAGE_LENGTH = 500;

/** Longest job title the API accepts, matching `users.job_title`. */
export const MAX_JOB_TITLE_LENGTH = 120;

/** Longest name part, matching `MAX_NAME_LENGTH` on the API. */
export const MAX_NAME_LENGTH = 100;

/** Longest avatar reference, matching `users.profile_image`. */
export const MAX_PROFILE_IMAGE_LENGTH = 512;

/** Shortest password the API will set, matching `MIN_PASSWORD_LENGTH`. */
export const MIN_PASSWORD_LENGTH = 8;

// --------------------------------------------------------------------------- //
// Requests
// --------------------------------------------------------------------------- //

/**
 * The profile form.
 *
 * Names are required and trimmed; everything else is optional and may be
 * emptied.
 *
 * **No `.transform()` anywhere, deliberately.** A transform would make the
 * schema's input and output types differ, and `zodResolver` hands React Hook
 * Form the *output* — so the form's values would no longer round-trip through
 * the same schema, and a field this file turned into `null` would fail its own
 * `z.string()` on the way back through. The blank-to-`null` normalization
 * belongs to the submit handler, which is the one place that knows it is talking
 * to the API. See {@link emptyToNull}.
 */
export const profileFormSchema = z.object({
  firstName: z
    .string()
    .trim()
    .min(1, vm("validation.settings.firstNameRequired"))
    .max(MAX_NAME_LENGTH, vm("validation.maxLength", { max: MAX_NAME_LENGTH })),
  lastName: z
    .string()
    .trim()
    .min(1, vm("validation.settings.lastNameRequired"))
    .max(MAX_NAME_LENGTH, vm("validation.maxLength", { max: MAX_NAME_LENGTH })),
  phone: z.string().trim().max(32, "Keep it under 32 characters.").optional(),
  jobTitle: z
    .string()
    .trim()
    .max(MAX_JOB_TITLE_LENGTH, vm("validation.maxLength", { max: MAX_JOB_TITLE_LENGTH }))
    .optional(),
  profileImage: z
    .string()
    .trim()
    .max(MAX_PROFILE_IMAGE_LENGTH, vm("validation.settings.urlTooLong"))
    .optional(),
});

export type ProfileFormValues = z.infer<typeof profileFormSchema>;

/**
 * A blank optional field means "no value", not "a value of length zero".
 *
 * Sent as `null` rather than `""` because that is how somebody *removes* a phone
 * number the platform holds about them — and because the API reads an empty
 * string the same way, so one representation on the wire is one fewer thing for
 * the two sides to disagree about.
 */
export function emptyToNull(value: string | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

/**
 * The change-password form.
 *
 * The confirmation field is checked **here and nowhere else**: the API never sees
 * it, because "did you type it twice the same" is a question about a form rather
 * than about a password. Sending it would be sending a credential the server has
 * no use for.
 */
export const changePasswordFormSchema = z
  .object({
    currentPassword: z.string().min(1, vm("validation.settings.currentPasswordRequired")),
    newPassword: z
      .string()
      .min(MIN_PASSWORD_LENGTH, vm("validation.auth.passwordTooShort", { min: MIN_PASSWORD_LENGTH })),
    confirmPassword: z.string().min(1, vm("validation.auth.confirmPassword")),
  })
  .refine((values) => values.newPassword === values.confirmPassword, {
    path: ["confirmPassword"],
    message: vm("validation.auth.passwordsDoNotMatch"),
  })
  .refine((values) => values.newPassword !== values.currentPassword, {
    path: ["newPassword"],
    message: vm("validation.auth.passwordUnchanged"),
  });

export type ChangePasswordFormValues = z.input<typeof changePasswordFormSchema>;

/** The maintenance-mode form, in the Administration panel. */
export const maintenanceFormSchema = z.object({
  maintenanceMode: z.boolean(),
  message: z
    .string()
    .trim()
    .max(
      MAX_MAINTENANCE_MESSAGE_LENGTH,
      vm("validation.settings.noticeTooLong", { max: MAX_MAINTENANCE_MESSAGE_LENGTH }),
    ),
});

export type MaintenanceFormValues = z.input<typeof maintenanceFormSchema>;

// --------------------------------------------------------------------------- //
// Responses
// --------------------------------------------------------------------------- //

export const settingDefinitionSchema = z.object({
  key: z.string(),
  section: z.enum(SETTINGS_SECTIONS),
  value_type: z.enum(SETTING_VALUE_TYPES),
  choices: z.array(z.string()),
  max_length: z.number().nullable(),
  max_items: z.number().nullable(),
});

export const settingSchema = z.object({
  key: z.string(),
  section: z.enum(SETTINGS_SECTIONS),
  // See the module note: only the definition travelling beside it knows what a
  // valid value for this key looks like.
  value: z.unknown(),
  is_default: z.boolean(),
});

export const settingsCollectionSchema = z.object({
  settings: z.array(settingSchema),
  definitions: z.array(settingDefinitionSchema),
});

export const settingsSectionSchema = z.object({
  section: z.enum(SETTINGS_SECTIONS),
  storage: z.enum(SETTINGS_STORAGE_KINDS),
  editable: z.boolean(),
  administrative: z.boolean(),
});

export const profileSchema = z.object({
  id: z.string(),
  email: z.string(),
  first_name: z.string(),
  last_name: z.string(),
  full_name: z.string(),
  phone: z.string().nullable(),
  profile_image: z.string().nullable(),
  job_title: z.string().nullable(),
  role: z.string(),
  status: z.string(),
  must_change_password: z.boolean(),
  last_login_at: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const sessionSchema = z.object({
  session_id: z.string(),
  is_current: z.boolean(),
  created_at: z.string(),
  last_seen_at: z.string(),
  expires_at: z.string(),
  ip_address: z.string().nullable(),
  user_agent: z.string().nullable(),
});

export const sessionListSchema = z.object({
  sessions: z.array(sessionSchema),
  available: z.boolean(),
});

export const maintenanceStatusSchema = z.object({
  maintenance_mode: z.boolean(),
  message: z.string().nullable(),
});

export const settingsOverviewSchema = z.object({
  sections: z.array(settingsSectionSchema),
  profile: profileSchema,
  settings: settingsCollectionSchema,
  maintenance: maintenanceStatusSchema,
});

export const sessionRevocationSchema = z.object({
  message: z.string(),
  access_token: z.string(),
  refresh_token: z.string(),
  expires_in: z.number(),
});

export const settingsMetricsSchema = z.object({
  since: z.string(),
  stored_user_settings: z.number(),
  customised_users: z.number(),
  stored_platform_settings: z.number(),
  updated: z.number(),
  failed: z.number(),
  success_rate: z.number(),
  profile_changes: z.number(),
  password_changes: z.number(),
  session_revocations: z.number(),
  updated_by_section: z.record(z.string(), z.number()),
  failures_by_reason: z.record(z.string(), z.number()),
});
