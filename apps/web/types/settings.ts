/**
 * Settings domain types.
 *
 * The app-side shape of `/settings`, in camelCase. Nothing above
 * `lib/api/settings.ts` sees the API's snake_case wire format.
 *
 * **Two vocabularies are strict unions and one is deliberately a loose string.**
 * A section, a storage kind, and a value type are closed sets the client
 * *branches on* — a renderer per value type, a panel per section — so an
 * unrecognised one is a genuine contract break worth failing on. A **setting key**
 * is a loose `string`, because the server's registry is open by design: a tenth
 * setting is one entry in `core/settings.py` with no migration, and parsing keys
 * as a strict enum here would mean a setting added server-side turned somebody's
 * settings page into a parse error. That is the same distinction
 * `lib/validation/notification.ts` draws between a notification category and a
 * notification type.
 */

/** The nine sections `20-settings.md`'s structure diagram names, in its order. */
export const SETTINGS_SECTIONS = [
  "profile",
  "security",
  "notifications",
  "communication",
  "ai",
  "dashboard",
  "appearance",
  "language",
  "administration",
] as const;

export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

/**
 * Where a section's values live — and therefore which endpoint writes them.
 *
 * Served by the API rather than assumed here, which is what lets the Settings
 * page render a section it stores nothing for: `notification_preferences` sends
 * the client to `/notifications/preferences`, the feature that owns them.
 */
export const SETTINGS_STORAGE_KINDS = [
  "user_settings",
  "platform_settings",
  "profile",
  "account",
  "notification_preferences",
] as const;

export type SettingsStorageKind = (typeof SETTINGS_STORAGE_KINDS)[number];

/** How a setting's value is carried, and therefore which control renders it. */
export const SETTING_VALUE_TYPES = [
  "boolean",
  "enum",
  "text",
  "timezone",
  "string_list",
] as const;

export type SettingValueType = (typeof SETTING_VALUE_TYPES)[number];

/** A setting's value. Narrow, because the registry admits exactly these shapes. */
export type SettingValue = boolean | string | string[];

/** One section of the page, as the API describes it. */
export interface SettingsSectionDescriptor {
  section: SettingsSection;
  storage: SettingsStorageKind;
  editable: boolean;
  administrative: boolean;
}

/** How to render one setting's control. Served, never hard-coded here. */
export interface SettingDefinition {
  key: string;
  section: SettingsSection;
  valueType: SettingValueType;
  choices: string[];
  maxLength: number | null;
  maxItems: number | null;
}

/** One setting, with this caller's answer to it. */
export interface Setting {
  key: string;
  section: SettingsSection;
  value: SettingValue;
  /** Whether this is the platform's answer rather than a choice somebody made. */
  isDefault: boolean;
}

/** Every setting the platform offers, with how to render each. */
export interface SettingsCollection {
  settings: Setting[];
  definitions: SettingDefinition[];
}

/** One change to one setting. */
export interface SettingChange {
  key: string;
  value: SettingValue;
}

/** The caller's own profile. */
export interface Profile {
  id: string;
  email: string;
  firstName: string;
  lastName: string;
  fullName: string;
  phone: string | null;
  profileImage: string | null;
  jobTitle: string | null;
  role: string;
  status: string;
  mustChangePassword: boolean;
  lastLoginAt: string | null;
  createdAt: string;
  updatedAt: string;
}

/**
 * The four fields a person may change about themselves.
 *
 * Every field optional and `null` meaningful: omitting one leaves it alone, while
 * sending `null` clears it. There is no `email`, `role`, or `status` — see
 * `ProfileUpdate` on the API for why those fields do not exist rather than being
 * ignored.
 */
export interface ProfileUpdate {
  firstName?: string;
  lastName?: string;
  phone?: string | null;
  profileImage?: string | null;
  jobTitle?: string | null;
}

/** One live sign-in. */
export interface Session {
  sessionId: string;
  isCurrent: boolean;
  createdAt: string;
  lastSeenAt: string;
  expiresAt: string;
  ipAddress: string | null;
  userAgent: string | null;
}

/**
 * The caller's sessions, and whether the list could be built at all.
 *
 * `available: false` means the registry was unreachable — the list is
 * *unavailable*, not empty. The two need different sentences on screen, which is
 * why the API reports them separately rather than letting an empty array mean
 * both.
 */
export interface SessionListing {
  sessions: Session[];
  available: boolean;
}

/** What the platform is telling everybody about its own availability. */
export interface MaintenanceStatus {
  maintenanceMode: boolean;
  message: string | null;
}

/** Everything the Settings page needs on first load. */
export interface SettingsOverview {
  sections: SettingsSectionDescriptor[];
  profile: Profile;
  settings: SettingsCollection;
  maintenance: MaintenanceStatus;
}

/** Platform-wide settings health. Requires `settings:monitor`. */
export interface SettingsMetrics {
  since: string;
  storedUserSettings: number;
  customisedUsers: number;
  storedPlatformSettings: number;
  updated: number;
  failed: number;
  successRate: number;
  profileChanges: number;
  passwordChanges: number;
  sessionRevocations: number;
  updatedBySection: Record<string, number>;
  failuresByReason: Record<string, number>;
}

// --------------------------------------------------------------------------- //
// Known setting keys
// --------------------------------------------------------------------------- //

/**
 * The setting keys this build knows how to label.
 *
 * A **convenience, not a contract**: the page renders every setting the API
 * sends, and one absent from here falls back to its key. That is the whole point
 * of the server-described definitions — a setting added on the server appears in
 * a browser nobody redeployed, wearing its identifier until this file catches up.
 */
export const SETTING_KEY = {
  theme: "theme",
  language: "language",
  timezone: "timezone",
  dateFormat: "date_format",
  timeFormat: "time_format",
  aiResponseLength: "ai_response_length",
  aiStreaming: "ai_streaming",
  aiCitations: "ai_citations",
  dashboardRange: "dashboard_range",
  dashboardWidgets: "dashboard_widgets",
} as const;

/** Platform setting keys, for the Administration panel. Same caveat as above. */
export const PLATFORM_SETTING_KEY = {
  maintenanceMode: "maintenance_mode",
  maintenanceMessage: "maintenance_message",
  defaultTheme: "default_theme",
  defaultLanguage: "default_language",
  defaultTimezone: "default_timezone",
  defaultDateFormat: "default_date_format",
  defaultTimeFormat: "default_time_format",
  aiDefaultResponseLength: "ai_default_response_length",
  aiDefaultStreaming: "ai_default_streaming",
  aiDefaultCitations: "ai_default_citations",
} as const;

/**
 * The preferences the shell itself applies, resolved from the collection above.
 *
 * A flat, typed view over what is otherwise a list of `{key, value}` pairs, so a
 * component that needs the date format reads `preferences.dateFormat` rather than
 * searching an array and narrowing an unknown. Only the settings something
 * actually *reads* appear here; the rest are rendered generically and never
 * destructured.
 */
export interface ResolvedPreferences {
  theme: "light" | "dark" | "system";
  language: string;
  timezone: string;
  dateFormat: string;
  timeFormat: string;
  aiResponseLength: string;
  aiStreaming: boolean;
  aiCitations: string;
  dashboardRange: string;
  dashboardWidgets: string[];
  /**
   * Whether `language` is the platform's answer rather than one somebody chose.
   *
   * The one `*IsDefault` flag carried up here, and it earns its place:
   * `21-localization.md` puts *browser language* second in its selection chain
   * and qualifies it **"first login only"**, which needs a precise notion of
   * "has not chosen". An account that has never opened Settings has no stored row
   * at all — the platform's own representation of that — and this is how the
   * shell reads it. Without it, a browser set to Arabic would silently override
   * somebody who deliberately chose the platform default, on every load.
   */
  languageIsDefault: boolean;
}
