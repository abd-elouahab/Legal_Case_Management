"use client";

import * as React from "react";
import Link from "next/link";
import { BellOff, Loader2 } from "lucide-react";

import { NotificationItem } from "@/components/notifications/notification-item";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  notificationErrorMessage,
  useMarkAllNotificationsRead,
  useMarkNotificationsRead,
  useNotifications,
} from "@/hooks/use-notifications";
import { ROUTES } from "@/lib/routes";
import { DEFAULT_NOTIFICATION_QUERY } from "@/types/notification";

/**
 * The notification panel: the most recent notifications, inside the bell.
 *
 * **A preview, not the feed.** It asks for one short page and offers a way to the
 * history page; a popover that paginated would be a second implementation of the
 * page it links to. The filter it does offer is the one somebody actually reaches
 * for from a bell — "only the ones I have not read" — and it executes on the
 * server like every other filter.
 *
 * **It fetches only while it is open.** The badge is a separate, tiny query that
 * runs everywhere (see `useNotificationSummary`); downloading a page of
 * notifications to render a number would be the most wasteful request the
 * platform makes.
 */

/** How many notifications the popover previews. */
const PANEL_PAGE_SIZE = 8;

export function NotificationPanel({ onNavigate }: { onNavigate?: () => void }) {
  const [unreadOnly, setUnreadOnly] = React.useState(false);

  const query = React.useMemo(
    () => ({
      ...DEFAULT_NOTIFICATION_QUERY,
      pageSize: PANEL_PAGE_SIZE,
      unreadOnly,
    }),
    [unreadOnly],
  );

  const { data, isPending, isError, error } = useNotifications(query);
  const markRead = useMarkNotificationsRead();
  const markAllRead = useMarkAllNotificationsRead();

  const items = data?.items ?? [];
  const hasUnread = (data?.unreadCount ?? 0) > 0;

  return (
    <div className="flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col sm:w-96">
      <div className="flex items-center justify-between gap-2 px-3 py-2">
        <p className="text-sm font-semibold text-foreground">Notifications</p>
        <Button
          variant="ghost"
          size="sm"
          className="text-xs"
          onClick={() => setUnreadOnly((current) => !current)}
          aria-pressed={unreadOnly}
        >
          {unreadOnly ? "Show all" : "Unread only"}
        </Button>
      </div>

      <Separator />

      <ScrollArea className="max-h-96">
        {isPending ? (
          <div className="flex items-center justify-center gap-2 px-3 py-10 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Loading notifications…
          </div>
        ) : isError ? (
          <p
            role="alert"
            className="px-3 py-8 text-center text-sm text-destructive"
          >
            {notificationErrorMessage(error)}
          </p>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-3 py-10 text-center">
            <BellOff className="h-6 w-6 text-muted-foreground" aria-hidden="true" />
            <p className="text-sm text-muted-foreground">
              {unreadOnly ? "Nothing unread." : "No notifications yet."}
            </p>
          </div>
        ) : (
          <ul className="flex flex-col">
            {items.map((notification) => (
              <NotificationItem
                key={notification.id}
                notification={notification}
                onMarkRead={(id) => markRead.mutate([id])}
                onNavigate={onNavigate}
              />
            ))}
          </ul>
        )}
      </ScrollArea>

      <Separator />

      <div className="flex items-center justify-between gap-2 px-3 py-2">
        <Button
          variant="ghost"
          size="sm"
          className="text-xs"
          // Deliberately unscoped by the panel's own "unread only" toggle: that
          // toggle changes what is *shown*, and a button that quietly marked only
          // the visible page would leave a badge that does not match the list it
          // was pressed on. The category-scoped variant lives on the history
          // page, where a category filter is an explicit choice.
          onClick={() => markAllRead.mutate(null)}
          disabled={!hasUnread || markAllRead.isPending}
        >
          {markAllRead.isPending ? "Marking…" : "Mark all as read"}
        </Button>

        <Button asChild variant="ghost" size="sm" className="text-xs">
          <Link href={ROUTES.notifications} onClick={onNavigate}>
            View all
          </Link>
        </Button>
      </div>
    </div>
  );
}
