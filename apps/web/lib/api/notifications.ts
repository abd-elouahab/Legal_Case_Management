/**
 * Notification API calls.
 *
 * Thin, typed wrappers over the `/notifications` endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape —
 * and a backend change that alters a payload fails here, loudly, instead of
 * surfacing as `undefined` in a badge.
 *
 * **There is no `createNotification` here, and there will not be one.**
 * Notifications are created by a subscriber on the API's event dispatcher, from
 * business modules that do not know the feature exists; the only thing a client
 * can create is a platform-wide announcement, which has no business action
 * behind it and no resource it is about.
 */

import { apiRequest } from "@/lib/api/client";
import { NOTIFICATION_ENDPOINTS } from "@/lib/api/config";
import {
  announcementResultSchema,
  notificationMetricsSchema,
  notificationPageSchema,
  notificationPreferencesSchema,
  notificationSchema,
  notificationSummarySchema,
} from "@/lib/validation/notification";
import type {
  AnnouncementKind,
  AnnouncementResult,
  Notification,
  NotificationListQuery,
  NotificationMetrics,
  NotificationPage,
  NotificationPreference,
  NotificationPreferenceChange,
  NotificationSummary,
} from "@/types/notification";

type NotificationWire = ReturnType<typeof notificationSchema.parse>;
type SummaryWire = ReturnType<typeof notificationSummarySchema.parse>;
type PreferencesWire = ReturnType<typeof notificationPreferencesSchema.parse>;
type MetricsWire = ReturnType<typeof notificationMetricsSchema.parse>;

/** Map one API notification onto the app's {@link Notification}. */
function toNotification(payload: NotificationWire): Notification {
  return {
    id: payload.id,
    category: payload.category,
    notificationType: payload.notification_type,
    priority: payload.priority,

    title: payload.title,
    message: payload.message,
    language: payload.language,

    eventType: payload.event_type,
    ruleKey: payload.rule_key,

    caseId: payload.case_id,
    actor: payload.actor
      ? {
          id: payload.actor.id,
          fullName: payload.actor.full_name,
          role: payload.actor.role,
        }
      : null,
    target: payload.target
      ? { targetType: payload.target.target_type, targetId: payload.target.target_id }
      : null,

    readAt: payload.read_at,
    isRead: payload.is_read,
    createdAt: payload.created_at,
  };
}

function toSummary(payload: SummaryWire): NotificationSummary {
  return {
    unreadCount: payload.unread_count,
    unreadCountCapped: payload.unread_count_capped,
    totalCount: payload.total_count,
    unreadByCategory: payload.unread_by_category,
    highestUnreadPriority: payload.highest_unread_priority,
  };
}

function toPreferences(payload: PreferencesWire): NotificationPreference[] {
  return payload.preferences.map((entry) => ({
    preferenceKey: entry.preference_key,
    inApp: entry.in_app,
    email: entry.email,
    isDefault: entry.is_default,
  }));
}

function toMetrics(payload: MetricsWire): NotificationMetrics {
  return {
    since: payload.since,
    enabled: payload.enabled,

    totalNotifications: payload.total_notifications,
    unreadNotifications: payload.unread_notifications,
    readNotifications: payload.read_notifications,
    readRate: payload.read_rate,
    recipients: payload.recipients,

    created: payload.created,
    delivered: payload.delivered,
    failed: payload.failed,
    suppressedByPreference: payload.suppressed_by_preference,
    deduplicated: payload.deduplicated,
    dropped: payload.dropped,
    pending: payload.pending,

    averageDeliveryLatencyMs: payload.average_delivery_latency_ms,

    notificationsByCategory: payload.notifications_by_category,
    createdByRule: payload.created_by_rule,
    failuresByReason: payload.failures_by_reason,

    windowDays: payload.window_days,
  };
}

/**
 * Build the query string for a feed request.
 *
 * "Any" filters are omitted rather than sent as blanks, so the request URL
 * reflects what is actually being asked — which also makes the query a stable
 * cache key for TanStack Query.
 */
export function buildNotificationListParams(query: NotificationListQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
    sort_by: query.sortBy,
    sort_order: query.sortOrder,
  });

  if (query.unreadOnly) params.set("unread_only", "true");

  const optional: Array<[string, string | null]> = [
    ["category", query.category],
    ["notification_type", query.notificationType],
    ["priority", query.priority],
    ["case_id", query.caseId],
    ["language", query.language],
  ];

  for (const [key, value] of optional) {
    if (value) params.set(key, value);
  }

  return params.toString();
}

/** Fetch one page of the caller's own feed. */
export async function fetchNotifications(
  query: NotificationListQuery,
): Promise<NotificationPage> {
  const raw = await apiRequest<unknown>(
    `${NOTIFICATION_ENDPOINTS.list}?${buildNotificationListParams(query)}`,
  );
  const data = notificationPageSchema.parse(raw);

  return {
    items: data.items.map(toNotification),
    totalRecords: data.total_records,
    unreadCount: data.unread_count,
    unreadCountCapped: data.unread_count_capped,
    page: data.page,
    pageSize: data.page_size,
    totalPages: data.total_pages,
  };
}

/** Fetch one notification. Does **not** mark it read — that is a separate call. */
export async function fetchNotification(
  id: string,
  language?: string,
): Promise<Notification> {
  const path = language
    ? `${NOTIFICATION_ENDPOINTS.detail(id)}?language=${encodeURIComponent(language)}`
    : NOTIFICATION_ENDPOINTS.detail(id);
  return toNotification(notificationSchema.parse(await apiRequest<unknown>(path)));
}

/**
 * Fetch the caller's unread state.
 *
 * The bell's call, and the reason it is separate from the feed: this is made on
 * every page of the application, and it returns a handful of numbers rather than
 * a page of notifications.
 */
export async function fetchNotificationSummary(): Promise<NotificationSummary> {
  const raw = await apiRequest<unknown>(NOTIFICATION_ENDPOINTS.summary);
  return toSummary(notificationSummarySchema.parse(raw));
}

/**
 * Mark specific notifications as read.
 *
 * Answers with the **new badge** rather than the marked rows, because what a
 * client does next is redraw it — returning the rows it already has would be a
 * page of data to update a number.
 */
export async function markNotificationsRead(ids: string[]): Promise<NotificationSummary> {
  const raw = await apiRequest<unknown>(NOTIFICATION_ENDPOINTS.read, {
    method: "PATCH",
    body: { notification_ids: ids },
  });
  return toSummary(notificationSummarySchema.parse(raw));
}

/** Mark everything unread as read, optionally within one category. */
export async function markAllNotificationsRead(
  category?: string | null,
): Promise<NotificationSummary> {
  const raw = await apiRequest<unknown>(NOTIFICATION_ENDPOINTS.readAll, {
    method: "PATCH",
    body: category ? { category } : {},
  });
  return toSummary(notificationSummarySchema.parse(raw));
}

/** Fetch every preference the platform offers, with the caller's answer to each. */
export async function fetchNotificationPreferences(): Promise<NotificationPreference[]> {
  const raw = await apiRequest<unknown>(NOTIFICATION_ENDPOINTS.preferences);
  return toPreferences(notificationPreferencesSchema.parse(raw));
}

/**
 * Set some of the caller's preferences.
 *
 * A list of *changes* rather than the whole set, matching the API: two settings
 * panels open at once cannot then silently revert each other's saves.
 *
 * A change carries **only the channels it is actually changing**. Sending
 * `in_app: false` for a switch nobody touched would silence a channel by
 * omission, which is exactly what the API's optional fields exist to prevent.
 */
export async function updateNotificationPreferences(
  changes: NotificationPreferenceChange[],
): Promise<NotificationPreference[]> {
  const raw = await apiRequest<unknown>(NOTIFICATION_ENDPOINTS.preferences, {
    method: "PUT",
    body: {
      preferences: changes.map((entry) => ({
        preference_key: entry.preferenceKey,
        ...(entry.inApp === undefined ? {} : { in_app: entry.inApp }),
        ...(entry.email === undefined ? {} : { email: entry.email }),
      })),
    },
  });
  return toPreferences(notificationPreferencesSchema.parse(raw));
}

/** Publish a platform-wide announcement. Requires `notifications:manage`. */
export async function publishAnnouncement(payload: {
  kind: AnnouncementKind;
  message: string;
}): Promise<AnnouncementResult> {
  const raw = await apiRequest<unknown>(NOTIFICATION_ENDPOINTS.announcements, {
    method: "POST",
    body: { kind: payload.kind, message: payload.message },
  });
  const data = announcementResultSchema.parse(raw);
  return { recipients: data.recipients, skipped: data.skipped, kind: data.kind };
}

/** Platform-wide notification health. Requires `notifications:monitor`. */
export async function fetchNotificationMetrics(
  windowDays?: number,
): Promise<NotificationMetrics> {
  const path = windowDays
    ? `${NOTIFICATION_ENDPOINTS.metrics}?window_days=${windowDays}`
    : NOTIFICATION_ENDPOINTS.metrics;
  return toMetrics(notificationMetricsSchema.parse(await apiRequest<unknown>(path)));
}
