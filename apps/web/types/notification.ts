/**
 * Notification types.
 *
 * Mirror the API's notification payloads (`apps/api/schemas/notification.py`) and
 * the vocabularies defined in `apps/api/models/notification.py` and
 * `apps/api/core/notifications.py`. Union types rather than magic strings, per
 * the code standards: referencing a priority the platform does not define is a
 * compile error.
 *
 * **One vocabulary here is deliberately open and two are closed**, and the split
 * mirrors the server's storage exactly. `NotificationType` and
 * `NotificationPriority` are PostgreSQL enums on the server, so a value this
 * build has never heard of would be a genuine contract break. `category` is a
 * `VARCHAR` because `16-notifications.md` requires future categories *"without
 * redesign"* — so it is typed as a **string** here, with the known members
 * offered as a helper for icons and labels, and an unrecognised one renders with
 * a neutral fallback rather than failing.
 *
 * **Nothing here describes a route.** A notification names a *resource*
 * (`{targetType, targetId}`), never a URL, because the spec requires navigation
 * to stay independent of frontend routing. Turning one into a path is
 * `notificationHref`'s job, and it is the only place that knows this app's
 * routes.
 */

import { ROUTES, caseRoute } from "@/lib/routes";

/** How a notification should be read. Closed: a PostgreSQL enum on the server. */
export const NOTIFICATION_TYPES = [
  "information",
  "success",
  "warning",
  "error",
] as const;
export type NotificationType = (typeof NOTIFICATION_TYPES)[number];

/** How loudly a notification presents itself. Closed, for the same reason. */
export const NOTIFICATION_PRIORITIES = ["low", "normal", "high", "critical"] as const;
export type NotificationPriority = (typeof NOTIFICATION_PRIORITIES)[number];

/**
 * Display order for priorities.
 *
 * Mirrors `PRIORITY_RANK` on the server, and exists for the same reason: sorting
 * alphabetically puts `critical` below `low`, which makes "most urgent first"
 * mean nothing.
 */
export const NOTIFICATION_PRIORITY_RANK: Record<NotificationPriority, number> = {
  low: 0,
  normal: 1,
  high: 2,
  critical: 3,
};

/** Where the words live: `notifications.priorities` in the message catalogues. */
export const NOTIFICATION_PRIORITY_NAMESPACE = "notifications.priorities";

/**
 * The categories the platform ships today.
 *
 * A helper rather than the type of `Notification["category"]`: the registry is
 * **open** on the server, so a category added there must render here rather than
 * breaking a parse. Use {@link isKnownCategory} to narrow before reaching for a
 * label or an icon.
 */
export const NOTIFICATION_CATEGORIES = [
  "case",
  "document",
  "hearing",
  "ocr",
  "ai",
  "report",
  "user",
  "system",
] as const;
export type KnownNotificationCategory = (typeof NOTIFICATION_CATEGORIES)[number];

/** Where the words live: `notifications.categories` in the message catalogues. */
export const NOTIFICATION_CATEGORY_NAMESPACE = "notifications.categories";


/** Whether this build knows how to label and illustrate a category. */
export function isKnownCategory(value: string): value is KnownNotificationCategory {
  return (NOTIFICATION_CATEGORIES as readonly string[]).includes(value);
}

/**
 * The catalogue key for a category.
 *
 * The registry is **open** on the server, so a category added there has no key
 * here — and renders through the provider's `getMessageFallback`, which humanizes
 * it. That is what the old `categoryLabel` fallback did, moved to the one place
 * the whole application already falls back.
 */


/** What a notification opens. A resource, never a path. */
export const NOTIFICATION_TARGET_TYPES = [
  "case",
  "document",
  "report",
  "account",
] as const;
export type NotificationTargetType = (typeof NOTIFICATION_TARGET_TYPES)[number];

export interface NotificationTarget {
  targetType: string;
  /** Null for `account`, which names the reader. */
  targetId: string | null;
}

/** Who caused a notification. Name and role only — never an email address. */
export interface NotificationActor {
  id: string;
  fullName: string;
  role: string;
}

/** One notification, rendered by the API for this reader in one language. */
export interface Notification {
  id: string;
  /** Open registry — see the module note. Narrow with {@link isKnownCategory}. */
  category: string;
  notificationType: NotificationType;
  priority: NotificationPriority;

  /** Rendered by the server. There is no client-side template for a feed. */
  title: string;
  message: string;
  /** ISO 639-1 code the server rendered in. */
  language: string;

  eventType: string | null;
  ruleKey: string;

  caseId: string | null;
  actor: NotificationActor | null;
  target: NotificationTarget | null;

  readAt: string | null;
  isRead: boolean;
  createdAt: string;
}

/** One page of the feed, with the badge state the panel draws beside it. */
export interface NotificationPage {
  items: Notification[];
  totalRecords: number;
  unreadCount: number;
  /** Whether `unreadCount` hit its ceiling and means "this many or more". */
  unreadCountCapped: boolean;
  page: number;
  pageSize: number;
  totalPages: number;
}

/** The bell's state, from `/notifications/summary`. */
export interface NotificationSummary {
  unreadCount: number;
  unreadCountCapped: boolean;
  totalCount: number;
  unreadByCategory: Record<string, number>;
  highestUnreadPriority: NotificationPriority | null;
}

/** What a user can switch off. Open on the server, like the category. */
export const NOTIFICATION_PREFERENCE_KEYS = [
  "case_updates",
  "document_updates",
  "ocr_completion",
  "ai_report_completion",
  "hearing_updates",
  "account_activity",
  "system_announcements",
] as const;
export type NotificationPreferenceKey = (typeof NOTIFICATION_PREFERENCE_KEYS)[number];

/**
 * Where a preference's title and explanation live.
 *
 * `notifications.preferences.<key>.title` / `.description` in the catalogues. The
 * *keys* stay in {@link NOTIFICATION_PREFERENCE_KEYS} because they are the
 * platform's vocabulary and travel to the API; only the sentences moved.
 */
export const NOTIFICATION_PREFERENCE_NAMESPACE = "notifications.preferences";


/** The channels a notification can be delivered on. */
export const NOTIFICATION_CHANNELS = ["inApp", "email", "whatsapp"] as const;
export type NotificationChannel = (typeof NOTIFICATION_CHANNELS)[number];

/** One preference, as it currently stands, on every channel. */
export interface NotificationPreference {
  preferenceKey: NotificationPreferenceKey;
  inApp: boolean;
  /**
   * Whether emails of this kind are delivered.
   *
   * A `true` does not mean an email is sent: only the notification kinds the
   * platform marks for email travel on that channel, and only when the
   * deployment has configured a provider.
   */
  email: boolean;
  /**
   * Whether WhatsApp messages of this kind are delivered.
   *
   * As with email, a `true` does not mean a message is sent — and this channel
   * narrows once more than email does: it also needs a phone number on the
   * account, which is optional on this platform.
   */
  whatsapp: boolean;
  /** Whether this is the platform default rather than a choice the user made. */
  isDefault: boolean;
}

/**
 * A change to one preference. Every channel is optional and omitting one leaves
 * it alone, which matches the API exactly — a panel that toggles the WhatsApp
 * switch must not silently rewrite the in-app one.
 */
export interface NotificationPreferenceChange {
  preferenceKey: NotificationPreferenceKey;
  inApp?: boolean;
  email?: boolean;
  whatsapp?: boolean;
}

/** Which announcement a `POST /notifications/announcements` publishes. */
export const ANNOUNCEMENT_KINDS = ["announcement", "maintenance"] as const;
export type AnnouncementKind = (typeof ANNOUNCEMENT_KINDS)[number];

/** What a published announcement reached. */
export interface AnnouncementResult {
  recipients: number;
  skipped: number;
  kind: AnnouncementKind;
}

/** Platform-wide notification health. Requires `notifications:monitor`. */
export interface NotificationMetrics {
  since: string;
  enabled: boolean;

  totalNotifications: number;
  unreadNotifications: number;
  readNotifications: number;
  readRate: number;
  recipients: number;

  created: number;
  delivered: number;
  failed: number;
  suppressedByPreference: number;
  deduplicated: number;
  dropped: number;
  pending: number;

  averageDeliveryLatencyMs: number | null;

  notificationsByCategory: Record<string, number>;
  createdByRule: Record<string, number>;
  failuresByReason: Record<string, number>;

  windowDays: number | null;
}

/** Sortable columns of the feed. */
export const NOTIFICATION_SORT_FIELDS = ["created_at", "priority"] as const;
export type NotificationSortField = (typeof NOTIFICATION_SORT_FIELDS)[number];

/** The list query, as the hooks hold it. */
export interface NotificationListQuery {
  page: number;
  pageSize: number;
  unreadOnly: boolean;
  category: string | null;
  notificationType: NotificationType | null;
  priority: NotificationPriority | null;
  caseId: string | null;
  sortBy: NotificationSortField;
  sortOrder: "asc" | "desc";
  language: string | null;
}

/** Defaults for the history page. */
export const DEFAULT_NOTIFICATION_QUERY: NotificationListQuery = {
  page: 1,
  pageSize: 20,
  unreadOnly: false,
  category: null,
  notificationType: null,
  priority: null,
  caseId: null,
  sortBy: "created_at",
  sortOrder: "desc",
  language: null,
};

/**
 * Where a notification leads, or `null` if it leads nowhere.
 *
 * **The only place in the app that turns a notification's resource into a
 * path**, which is what keeps the API's navigation metadata independent of this
 * app's routing: the server says "case, this identifier", and restructuring the
 * routes below changes nothing on the server.
 *
 * A notification with no target — a withdrawn document, a case somebody was just
 * removed from — deliberately leads nowhere, because offering to open either
 * would offer a refusal.
 *
 * A **document** target resolves to its case workspace rather than to a document
 * route, because this app has none: documents are read inside the case they
 * belong to. That is a fact about *this client*, which is exactly why the server
 * sends a resource and not a path — a mobile client with a document screen maps
 * the same target somewhere else without the API changing.
 */
export function notificationHref(notification: Notification): string | null {
  const target = notification.target;
  if (!target) return null;

  switch (target.targetType) {
    case "case":
      return target.targetId ? caseRoute(target.targetId) : null;
    case "document":
      return notification.caseId ? caseRoute(notification.caseId) : ROUTES.documents;
    case "report":
      return ROUTES.reports;
    case "account":
      return ROUTES.settings;
    default:
      // A target type added server-side. Leading nowhere is the honest answer;
      // guessing a route would send somebody to a 404.
      return null;
  }
}
