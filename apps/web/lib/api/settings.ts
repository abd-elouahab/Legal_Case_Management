/**
 * Settings API calls.
 *
 * Thin, typed wrappers over the `/settings` endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape —
 * and a backend change that alters a payload fails here, loudly, instead of
 * surfacing as `undefined` in a form.
 *
 * **There is no `fetchNotificationPreferences` here, and there will not be one.**
 * Notification and communication preferences are the Notification Service's and
 * are read through `lib/api/notifications.ts`; the Settings page composes both,
 * which is what `20-settings.md`'s *"each feature should own its configuration"*
 * means in a client as well as in an API. A wrapper here would be a second path
 * to one stored thing.
 *
 * **Two calls swap the caller's access token**, and they must: changing a
 * password and signing out of every other session both invalidate every token
 * for the account, including the one that made the request. Each hands back a
 * replacement, which is put into the in-memory token store before the promise
 * resolves — so a caller that ignores the return value is still signed in, and a
 * caller that forgets to swap it cannot exist.
 */

import { apiRequest } from "@/lib/api/client";
import { SETTINGS_ENDPOINTS } from "@/lib/api/config";
import { setAccessToken } from "@/lib/api/token-store";
import {
  maintenanceStatusSchema,
  profileSchema,
  sessionListSchema,
  sessionRevocationSchema,
  settingsCollectionSchema,
  settingsMetricsSchema,
  settingsOverviewSchema,
} from "@/lib/validation/settings";
import type {
  MaintenanceStatus,
  Profile,
  ProfileUpdate,
  SessionListing,
  SettingChange,
  SettingsCollection,
  SettingsMetrics,
  SettingsOverview,
  SettingsSectionDescriptor,
  SettingValue,
} from "@/types/settings";

type ProfileWire = ReturnType<typeof profileSchema.parse>;
type CollectionWire = ReturnType<typeof settingsCollectionSchema.parse>;
type SessionsWire = ReturnType<typeof sessionListSchema.parse>;
type MaintenanceWire = ReturnType<typeof maintenanceStatusSchema.parse>;
type MetricsWire = ReturnType<typeof settingsMetricsSchema.parse>;
type OverviewWire = ReturnType<typeof settingsOverviewSchema.parse>;

function toProfile(payload: ProfileWire): Profile {
  return {
    id: payload.id,
    email: payload.email,
    firstName: payload.first_name,
    lastName: payload.last_name,
    fullName: payload.full_name,
    phone: payload.phone,
    profileImage: payload.profile_image,
    jobTitle: payload.job_title,
    role: payload.role,
    status: payload.status,
    mustChangePassword: payload.must_change_password,
    lastLoginAt: payload.last_login_at,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function toCollection(payload: CollectionWire): SettingsCollection {
  return {
    settings: payload.settings.map((entry) => ({
      key: entry.key,
      section: entry.section,
      // The API validates every value against the same registry that produced the
      // definition beside it, so a value reaching here is one the server accepted.
      // Narrowing happens where it is read, against that definition.
      value: entry.value as SettingValue,
      isDefault: entry.is_default,
    })),
    definitions: payload.definitions.map((entry) => ({
      key: entry.key,
      section: entry.section,
      valueType: entry.value_type,
      choices: entry.choices,
      maxLength: entry.max_length,
      maxItems: entry.max_items,
    })),
  };
}

function toSections(
  payload: OverviewWire["sections"],
): SettingsSectionDescriptor[] {
  return payload.map((entry) => ({
    section: entry.section,
    storage: entry.storage,
    editable: entry.editable,
    administrative: entry.administrative,
  }));
}

function toSessions(payload: SessionsWire): SessionListing {
  return {
    sessions: payload.sessions.map((entry) => ({
      sessionId: entry.session_id,
      isCurrent: entry.is_current,
      createdAt: entry.created_at,
      lastSeenAt: entry.last_seen_at,
      expiresAt: entry.expires_at,
      ipAddress: entry.ip_address,
      userAgent: entry.user_agent,
    })),
    available: payload.available,
  };
}

function toMaintenance(payload: MaintenanceWire): MaintenanceStatus {
  return { maintenanceMode: payload.maintenance_mode, message: payload.message };
}

function toMetrics(payload: MetricsWire): SettingsMetrics {
  return {
    since: payload.since,
    storedUserSettings: payload.stored_user_settings,
    customisedUsers: payload.customised_users,
    storedPlatformSettings: payload.stored_platform_settings,
    updated: payload.updated,
    failed: payload.failed,
    successRate: payload.success_rate,
    profileChanges: payload.profile_changes,
    passwordChanges: payload.password_changes,
    sessionRevocations: payload.session_revocations,
    updatedBySection: payload.updated_by_section,
    failuresByReason: payload.failures_by_reason,
  };
}

/**
 * Fetch everything the Settings page needs on first load.
 *
 * One request rather than four. Notification and communication preferences are
 * **not** in it — the section list says where they live and the client fetches
 * them from the feature that owns them.
 */
export async function fetchSettingsOverview(): Promise<SettingsOverview> {
  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.overview);
  const data = settingsOverviewSchema.parse(raw);
  return {
    sections: toSections(data.sections),
    profile: toProfile(data.profile),
    settings: toCollection(data.settings),
    maintenance: toMaintenance(data.maintenance),
  };
}

/** Fetch the caller's own profile. */
export async function fetchProfile(): Promise<Profile> {
  return toProfile(profileSchema.parse(await apiRequest<unknown>(SETTINGS_ENDPOINTS.profile)));
}

/**
 * Change the caller's own profile.
 *
 * Only the fields present in `changes` are sent, because omission and `null` mean
 * different things to the API: the first leaves a field alone, the second clears
 * it. A form that sent every field on every save would be unable to express
 * "leave my avatar as it is".
 */
export async function updateProfile(changes: ProfileUpdate): Promise<Profile> {
  const body: Record<string, unknown> = {};
  if (changes.firstName !== undefined) body.first_name = changes.firstName;
  if (changes.lastName !== undefined) body.last_name = changes.lastName;
  if (changes.phone !== undefined) body.phone = changes.phone;
  if (changes.profileImage !== undefined) body.profile_image = changes.profileImage;
  if (changes.jobTitle !== undefined) body.job_title = changes.jobTitle;

  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.profile, {
    method: "PATCH",
    body,
  });
  return toProfile(profileSchema.parse(raw));
}

/** Fetch every setting the platform offers, with the caller's answer to each. */
export async function fetchSettings(): Promise<SettingsCollection> {
  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.preferences);
  return toCollection(settingsCollectionSchema.parse(raw));
}

/**
 * Set some of the caller's settings.
 *
 * A list of *changes* rather than the whole set, matching the API: two settings
 * panels open at once cannot then silently revert each other's saves, and a
 * setting this build has never heard of is never overwritten by omission.
 */
export async function updateSettings(
  changes: SettingChange[],
): Promise<SettingsCollection> {
  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.preferences, {
    method: "PUT",
    body: {
      settings: changes.map((entry) => ({
        setting_key: entry.key,
        value: entry.value,
      })),
    },
  });
  return toCollection(settingsCollectionSchema.parse(raw));
}

/** Fetch the caller's live sign-ins. */
export async function fetchSessions(): Promise<SessionListing> {
  return toSessions(sessionListSchema.parse(await apiRequest<unknown>(SETTINGS_ENDPOINTS.sessions)));
}

/**
 * Sign out of every session except this one.
 *
 * **Swaps the access token before resolving.** The revocation invalidates every
 * token for the account including the one that made this request, so the
 * replacement the API returns is put into the token store here — a caller that
 * simply awaits this stays signed in, and there is no way to forget the step.
 *
 * @returns the API's confirmation message.
 */
export async function revokeOtherSessions(): Promise<string> {
  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.sessions, {
    method: "DELETE",
    // The replay-on-401 path would re-send this after a refresh, which would
    // revoke a second time. It cannot 401 in practice — the token is valid at the
    // moment of the call — but saying so is cheaper than reasoning about it later.
    skipAuthRefresh: true,
  });
  const data = sessionRevocationSchema.parse(raw);
  setAccessToken(data.access_token);
  return data.message;
}

/**
 * Change the caller's password.
 *
 * Delegates to the Settings surface over the authentication system: the current
 * password is required, `must_change_password` is cleared, and every *other*
 * session is signed out. The replacement access token is swapped in here, for the
 * reason {@link revokeOtherSessions} swaps one.
 *
 * @returns the API's confirmation message.
 */
export async function changeSettingsPassword(payload: {
  currentPassword: string;
  newPassword: string;
}): Promise<string> {
  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.password, {
    method: "POST",
    body: {
      current_password: payload.currentPassword,
      new_password: payload.newPassword,
    },
    skipAuthRefresh: true,
  });
  const data = sessionRevocationSchema.parse(raw);
  setAccessToken(data.access_token);
  return data.message;
}

/** Fetch the deployment's configuration. Requires `settings:manage`. */
export async function fetchPlatformSettings(): Promise<SettingsCollection> {
  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.administration);
  return toCollection(settingsCollectionSchema.parse(raw));
}

/** Set some of the deployment's configuration. Requires `settings:manage`. */
export async function updatePlatformSettings(
  changes: SettingChange[],
): Promise<SettingsCollection> {
  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.administration, {
    method: "PUT",
    body: {
      settings: changes.map((entry) => ({
        setting_key: entry.key,
        value: entry.value,
      })),
    },
  });
  return toCollection(settingsCollectionSchema.parse(raw));
}

/** Fetch the platform's maintenance posture. Readable by every authenticated caller. */
export async function fetchMaintenanceStatus(): Promise<MaintenanceStatus> {
  const raw = await apiRequest<unknown>(SETTINGS_ENDPOINTS.maintenance);
  return toMaintenance(maintenanceStatusSchema.parse(raw));
}

/** Platform-wide settings health. Requires `settings:monitor`. */
export async function fetchSettingsMetrics(): Promise<SettingsMetrics> {
  return toMetrics(settingsMetricsSchema.parse(await apiRequest<unknown>(SETTINGS_ENDPOINTS.metrics)));
}
