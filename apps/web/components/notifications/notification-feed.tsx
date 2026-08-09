"use client";

import * as React from "react";
import { Bell, Loader2 } from "lucide-react";

import { NotificationFilters } from "@/components/notifications/notification-filters";
import { NotificationItem } from "@/components/notifications/notification-item";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  notificationErrorMessage,
  useMarkAllNotificationsRead,
  useMarkNotificationsRead,
  useNotifications,
} from "@/hooks/use-notifications";
import {
  DEFAULT_NOTIFICATION_QUERY,
  type NotificationListQuery,
} from "@/types/notification";

/**
 * The notification history.
 *
 * The full feed the panel previews: filtered, paged, and marked read from one
 * place. Everything that narrows it happens on the server (see
 * {@link NotificationFilters}), so the totals and the page count are always about
 * the same set the rows came from.
 *
 * **"Mark all as read" follows the category filter.** A feed narrowed to
 * *Hearings* beside a button that silently marked *everything* is the most
 * reliable way to lose a notification somebody meant to keep — which is why the
 * API takes an optional category rather than only offering the blunt form.
 *
 * The query lives in component state rather than in the URL. A notification feed
 * is not a view anybody links to or shares — unlike a case list, where a filtered
 * URL is how one person hands a colleague a caseload — and a notification is
 * private to its recipient, so a shared link would be a link to somebody else's
 * empty feed.
 */

export function NotificationFeed() {
  const [query, setQuery] = React.useState<NotificationListQuery>(
    DEFAULT_NOTIFICATION_QUERY,
  );

  const { data, isPending, isError, error, refetch, isFetching } = useNotifications(query);
  const markRead = useMarkNotificationsRead();
  const markAllRead = useMarkAllNotificationsRead();

  const patch = React.useCallback((changes: Partial<NotificationListQuery>) => {
    setQuery((current) => ({ ...current, ...changes }));
  }, []);

  const reset = React.useCallback(() => setQuery(DEFAULT_NOTIFICATION_QUERY), []);

  const items = data?.items ?? [];
  const hasUnread = (data?.unreadCount ?? 0) > 0;
  const totalPages = data?.totalPages ?? 1;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <NotificationFilters query={query} onChange={patch} onReset={reset} />

        <Button
          variant="outline"
          size="sm"
          onClick={() => markAllRead.mutate(query.category)}
          disabled={!hasUnread || markAllRead.isPending}
        >
          {markAllRead.isPending
            ? "Marking…"
            : query.category
              ? "Mark this category as read"
              : "Mark all as read"}
        </Button>
      </div>

      {isPending ? (
        <Card className="flex items-center justify-center gap-2 p-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Loading notifications…
        </Card>
      ) : isError ? (
        <ErrorState
          title="Could not load your notifications"
          description={notificationErrorMessage(error)}
          onRetry={() => void refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          icon={Bell}
          title="Nothing here"
          description={
            query.unreadOnly || query.category || query.notificationType || query.priority
              ? "No notifications match these filters."
              : "Case updates, document activity, hearings, and AI results will appear here."
          }
        />
      ) : (
        <Card className="overflow-hidden p-0">
          <ul className="flex flex-col" aria-busy={isFetching}>
            {items.map((notification) => (
              <NotificationItem
                key={notification.id}
                notification={notification}
                onMarkRead={(id) => markRead.mutate([id])}
              />
            ))}
          </ul>
        </Card>
      )}

      {totalPages > 1 ? (
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            Page {data?.page ?? 1} of {totalPages}
            {data ? ` • ${data.totalRecords} notification${data.totalRecords === 1 ? "" : "s"}` : ""}
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => patch({ page: Math.max(1, query.page - 1) })}
              disabled={query.page <= 1 || isFetching}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => patch({ page: Math.min(totalPages, query.page + 1) })}
              disabled={query.page >= totalPages || isFetching}
            >
              Next
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
