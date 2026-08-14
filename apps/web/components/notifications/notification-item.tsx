"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { NotificationIcon } from "@/components/notifications/notification-icon";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useDateFormat } from "@/hooks/use-date-format";
import { cn } from "@/lib/utils";
import { notificationHref, type Notification } from "@/types/notification";

/**
 * One notification, as it appears in the panel and in the history page.
 *
 * **The whole row is the link**, not a "view" button beside it: a notification is
 * a pointer at something, and a target somebody has to aim at inside a 56-pixel
 * row is a target they will miss on a phone. A notification with no target — a
 * withdrawn document, a case somebody was just removed from — renders as plain
 * text rather than as a link that leads to a refusal.
 *
 * **Unread is marked three ways**, and deliberately not one: a dot, a heavier
 * title, and a tinted background. Colour alone would fail WCAG AA and a bold
 * weight alone is invisible in a list where every title is short.
 *
 * **Priority is shown only when it is high or critical.** A badge reading
 * "Normal" on nine rows out of ten is furniture people learn to ignore, and it
 * would make the one row that says "Critical" harder to see rather than easier —
 * the same reasoning `ConnectionStatusIndicator` records for rendering nothing
 * while the channel is healthy.
 *
 * Marking as read is an explicit control. Opening a notification deliberately
 * does *not* mark it — only the reader knows whether they actually took it in,
 * and a list that emptied its own badge on a stray click is a list that loses
 * things.
 */

export function NotificationItem({
  notification,
  onMarkRead,
  onNavigate,
  className,
}: {
  notification: Notification;
  onMarkRead?: (id: string) => void;
  /** Called when the row is followed, so a popover can close itself. */
  onNavigate?: () => void;
  className?: string;
}) {
  const { formatEventTime } = useDateFormat();
  const t = useTranslations("notifications.item");
  // The category registry is open on the server, so a category this build has
  // never heard of resolves through the provider's fallback to a humanized form
  // of itself — which is exactly what `categoryLabel` used to do by hand.
  const tCategories = useTranslations("notifications.categories");
  const tPriorities = useTranslations("notifications.priorities");
  const href = notificationHref(notification);
  const timestamp = formatEventTime(notification.createdAt);
  const isUrgent =
    notification.priority === "high" || notification.priority === "critical";

  const body = (
    <div className="flex flex-1 flex-col gap-1 overflow-hidden">
      <div className="flex items-start gap-2">
        <p
          className={cn(
            "flex-1 text-sm leading-snug text-foreground",
            !notification.isRead && "font-semibold",
          )}
        >
          {notification.title}
        </p>
        {!notification.isRead ? (
          <span
            className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-info"
            aria-hidden="true"
          />
        ) : null}
      </div>

      <p className="text-sm leading-snug text-muted-foreground">{notification.message}</p>

      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        <span>{timestamp}</span>
        <span aria-hidden="true">•</span>
        <span>{tCategories(notification.category)}</span>
        {notification.actor ? (
          <>
            <span aria-hidden="true">•</span>
            <span>{notification.actor.fullName}</span>
          </>
        ) : null}
        {isUrgent ? (
          <Badge
            variant="outline"
            className={cn(
              "ms-1",
              notification.priority === "critical"
                ? "border-destructive/30 bg-destructive/10 text-destructive"
                : "border-warning/30 bg-warning/10 text-warning",
            )}
          >
            {tPriorities(notification.priority)}
          </Badge>
        ) : null}
      </div>
    </div>
  );

  return (
    <li
      className={cn(
        "flex items-start gap-3 border-b border-border px-3 py-3 last:border-b-0",
        !notification.isRead && "bg-info/5",
        className,
      )}
    >
      <NotificationIcon
        category={notification.category}
        notificationType={notification.notificationType}
      />

      {href ? (
        <Link
          href={href}
          onClick={onNavigate}
          className="flex flex-1 overflow-hidden rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {body}
        </Link>
      ) : (
        body
      )}

      {!notification.isRead && onMarkRead ? (
        <Button
          variant="ghost"
          size="sm"
          className="shrink-0 text-xs"
          onClick={() => onMarkRead(notification.id)}
          // The title is the only thing that distinguishes one of these buttons
          // from the next in a list of ten, so it is what a screen reader
          // announces rather than a bare "Mark as read".
          aria-label={t("markReadFor", { title: notification.title })}
        >
          {t("markRead")}
        </Button>
      ) : null}
    </li>
  );
}
