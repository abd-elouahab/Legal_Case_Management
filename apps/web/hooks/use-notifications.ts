"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { ApiError, NetworkError } from "@/lib/api/errors";
import {
  fetchNotification,
  fetchNotificationMetrics,
  fetchNotificationPreferences,
  fetchNotificationSummary,
  fetchNotifications,
  markAllNotificationsRead,
  markNotificationsRead,
  publishAnnouncement,
  updateNotificationPreferences,
} from "@/lib/api/notifications";
import type {
  AnnouncementKind,
  AnnouncementResult,
  Notification,
  NotificationListQuery,
  NotificationMetrics,
  NotificationPage,
  NotificationPreference,
  NotificationPreferenceKey,
  NotificationSummary,
} from "@/types/notification";

/**
 * Server state for notifications.
 *
 * TanStack Query per `architecture.md`: a notification is server state, so it is
 * cached and invalidated rather than mirrored into a client store.
 *
 * **This module polls, and the polling is the point rather than a fallback.**
 * Notifications arrive over the WebSocket channel when it is available — the
 * event invalidates {@link notificationKeys}, and the badge and the panel refetch
 * — but `15-real-time-synchronization.md`'s rule that *"nothing may depend on the
 * channel"* applies here as much as anywhere: a deployment with
 * `REALTIME_ENABLED=false`, a failed connection, and a browser that blocks
 * WebSockets must all still show a lawyer that they were assigned a case. So the
 * badge polls on a slow interval, and the channel is what makes it feel instant.
 *
 * The interval is deliberately the slowest on the platform. A notification is not
 * a progress bar: being told about a hearing thirty seconds late costs nothing,
 * and a badge query on every page of the application at OCR's cadence would be
 * the most frequent request the platform makes.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the notification API.
 */

/** How often the unread badge re-checks when the channel is not carrying it. */
export const NOTIFICATION_POLL_INTERVAL_MS = 30_000;

/** Query keys. */
export const notificationKeys = {
  all: ["notifications"] as const,
  lists: () => [...notificationKeys.all, "list"] as const,
  list: (query: NotificationListQuery) => [...notificationKeys.lists(), query] as const,
  details: () => [...notificationKeys.all, "detail"] as const,
  detail: (id: string) => [...notificationKeys.details(), id] as const,
  summary: () => [...notificationKeys.all, "summary"] as const,
  preferences: () => [...notificationKeys.all, "preferences"] as const,
  metrics: (windowDays?: number) =>
    [...notificationKeys.all, "metrics", windowDays ?? "all"] as const,
};

/**
 * Translate a failure into a message safe to show the user.
 *
 * Branches on the API's machine-readable `code` rather than on message text,
 * which is localizable and may change — the same rule every other hook module
 * here follows.
 */
export function notificationErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "notification_not_found":
        return "This notification is no longer available.";
      case "notifications_disabled":
        return "Notifications are switched off on this platform right now.";
      case "forbidden":
        return "You do not have permission to do that.";
      default:
        return error.message;
    }
  }

  return "Something went wrong. Please try again.";
}

// --------------------------------------------------------------------------- //
// Reading
// --------------------------------------------------------------------------- //

/**
 * The unread badge.
 *
 * Its own query rather than a field on the feed, because it is asked for on every
 * page of the application: fetching a page of notifications to render a number
 * would download a feed nobody opened.
 */
export function useNotificationSummary(
  options: { enabled?: boolean } = {},
): UseQueryResult<NotificationSummary, unknown> {
  return useQuery({
    queryKey: notificationKeys.summary(),
    queryFn: fetchNotificationSummary,
    enabled: options.enabled ?? true,
    refetchInterval: NOTIFICATION_POLL_INTERVAL_MS,
    // A badge that is a few seconds stale is not wrong in any way a user can
    // perceive, and this query is mounted for the whole session.
    staleTime: NOTIFICATION_POLL_INTERVAL_MS / 2,
  });
}

/** One page of the caller's feed. */
export function useNotifications(
  query: NotificationListQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<NotificationPage, unknown> {
  return useQuery({
    queryKey: notificationKeys.list(query),
    queryFn: () => fetchNotifications(query),
    enabled: options.enabled ?? true,
    // Kept while a new page loads, so paging and filtering do not blank the list
    // out and back in.
    placeholderData: (previous) => previous,
  });
}

/** One notification. Reading it does **not** mark it read. */
export function useNotification(
  id: string | null,
  language?: string,
): UseQueryResult<Notification, unknown> {
  return useQuery({
    queryKey: notificationKeys.detail(id ?? ""),
    queryFn: () => fetchNotification(id as string, language),
    enabled: Boolean(id),
  });
}

// --------------------------------------------------------------------------- //
// Read state
// --------------------------------------------------------------------------- //

/**
 * Mark specific notifications as read.
 *
 * The response **is** the new badge, so it is written straight into the summary
 * cache rather than invalidated: the server has just told us the number, and
 * asking again for what we were handed would be a round trip to learn nothing.
 * The *lists* are invalidated, because their rows' read state changed and the
 * response does not carry them.
 */
export function useMarkNotificationsRead(): UseMutationResult<
  NotificationSummary,
  unknown,
  string[]
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: markNotificationsRead,
    onSuccess: (summary) => {
      queryClient.setQueryData(notificationKeys.summary(), summary);
      void queryClient.invalidateQueries({ queryKey: notificationKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: notificationKeys.details() });
    },
  });
}

/** Mark everything unread as read, optionally within one category. */
export function useMarkAllNotificationsRead(): UseMutationResult<
  NotificationSummary,
  unknown,
  string | null | undefined
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (category: string | null | undefined) =>
      markAllNotificationsRead(category),
    onSuccess: (summary) => {
      queryClient.setQueryData(notificationKeys.summary(), summary);
      void queryClient.invalidateQueries({ queryKey: notificationKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: notificationKeys.details() });
    },
  });
}

// --------------------------------------------------------------------------- //
// Preferences
// --------------------------------------------------------------------------- //

/** Every preference the platform offers, with the caller's answer to each. */
export function useNotificationPreferences(
  options: { enabled?: boolean } = {},
): UseQueryResult<NotificationPreference[], unknown> {
  return useQuery({
    queryKey: notificationKeys.preferences(),
    queryFn: fetchNotificationPreferences,
    enabled: options.enabled ?? true,
    // Preferences change when this user changes them and at no other time.
    staleTime: 5 * 60 * 1000,
  });
}

/**
 * Set some of the caller's preferences.
 *
 * The response is the complete set, so it replaces the cache directly. Nothing
 * else is invalidated: switching a preference off stops *new* notifications of
 * that kind being created and deliberately leaves the ones already in the feed,
 * which is the only behaviour that makes "turn this off" reversible.
 */
export function useUpdateNotificationPreferences(): UseMutationResult<
  NotificationPreference[],
  unknown,
  Array<{ preferenceKey: NotificationPreferenceKey; inApp: boolean }>
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: updateNotificationPreferences,
    onSuccess: (preferences) => {
      queryClient.setQueryData(notificationKeys.preferences(), preferences);
    },
  });
}

// --------------------------------------------------------------------------- //
// Administration
// --------------------------------------------------------------------------- //

/**
 * Publish a platform-wide announcement. Requires `notifications:manage`.
 *
 * Invalidates the *caller's own* feed as well as the badge, because an
 * administrator is an active account and therefore one of the recipients.
 */
export function usePublishAnnouncement(): UseMutationResult<
  AnnouncementResult,
  unknown,
  { kind: AnnouncementKind; message: string }
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: publishAnnouncement,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: notificationKeys.all });
    },
  });
}

/**
 * Platform-wide notification health. Requires `notifications:monitor`.
 *
 * Polled rather than pushed, and the irony is deliberate — the same one
 * `useRealtimeMetrics` records: a panel that updated from the channel it measures
 * would stop updating precisely when the thing it measures broke, which is the
 * moment somebody is looking at it.
 */
export function useNotificationMetrics(
  options: { enabled?: boolean; windowDays?: number } = {},
): UseQueryResult<NotificationMetrics, unknown> {
  return useQuery({
    queryKey: notificationKeys.metrics(options.windowDays),
    queryFn: () => fetchNotificationMetrics(options.windowDays),
    enabled: options.enabled ?? true,
    refetchInterval: 15_000,
  });
}
