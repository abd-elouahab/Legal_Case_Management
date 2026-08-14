"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { Bell } from "lucide-react";

import { NotificationPanel } from "@/components/notifications/notification-panel";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCurrentUser } from "@/hooks/use-current-user";
import { useNotificationSummary } from "@/hooks/use-notifications";
import { usePermissions } from "@/hooks/use-permissions";
import { useRealtimeResource } from "@/hooks/use-realtime";
import { PERMISSION } from "@/types/authorization";
import { cn } from "@/lib/utils";

/**
 * The notification bell.
 *
 * Three things, and nothing else: it shows how many notifications are unread, it
 * opens the panel, and it subscribes to the topic those notifications arrive on.
 *
 * **It subscribes to the reader's own `user:` topic**, and this is the only place
 * that does. That topic carries every notification addressed to this person, and
 * the bell is the one component mounted on every page of the application — so
 * subscribing here means a notification reaches a badge whatever screen somebody
 * is looking at, and a component that unmounts does not take the subscription
 * with it. The topic is authorized by identity equality on the server, so the
 * subscription can carry nothing belonging to anybody else.
 *
 * **The channel is an accelerator, never a requirement.** The badge polls on a
 * slow interval regardless (see `useNotificationSummary`), so a deployment with
 * `REALTIME_ENABLED=false`, a failed connection, and a browser that blocks
 * WebSockets all still show a lawyer that they were assigned a case — the
 * `15-real-time-synchronization.md` rule that nothing may depend on the channel,
 * applied to the first feature built on top of it.
 *
 * **The count is capped and says so.** Past the server's ceiling the badge reads
 * "999+", because counting thousands of unread rows exactly is a database scan to
 * render something that would say that anyway.
 *
 * The unread state is never conveyed by the badge alone: the button's
 * `aria-label` states it in words, so a screen reader announces "Notifications, 3
 * unread" rather than a decoration.
 */

export function NotificationButton() {
  const t = useTranslations("notifications.panel");
  const [open, setOpen] = React.useState(false);
  const user = useCurrentUser();
  const { can, isLoading } = usePermissions();

  // While the session is being restored every check answers `false`, so the bell
  // is held back rather than rendered as "no permission" and then flashed in —
  // the trap `usePermissions` documents.
  const canView = !isLoading && can(PERMISSION.notificationsView);

  // Every notification for this person travels here. Subscribing unconditionally
  // is safe — the hook skips a null identifier — and is what makes the badge
  // update on whichever page they happen to have open.
  useRealtimeResource("user", canView ? (user?.id ?? null) : null);

  const { data } = useNotificationSummary({ enabled: canView });

  if (!canView) return null;

  const unread = data?.unreadCount ?? 0;
  const capped = data?.unreadCountCapped ?? false;
  const hasUnread = unread > 0;
  const label = hasUnread
    ? t("unreadLabel", { count: capped ? t("moreThan", { count: unread }) : unread })
    : t("title");
  const badge = capped ? `${unread}+` : String(unread);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <Button variant="ghost" size="icon" aria-label={label} className="relative">
              <Bell className="h-5 w-5" aria-hidden="true" />
              {hasUnread ? (
                <span
                  aria-hidden="true"
                  className={cn(
                    "absolute -end-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center",
                    "rounded-full bg-info px-1 text-[10px] font-semibold leading-none",
                    "text-background ring-2 ring-background",
                    // A four-character count ("999+") needs the room; a
                    // single digit should not sit in a lozenge.
                    badge.length > 2 && "h-4 px-1.5",
                  )}
                >
                  {badge}
                </span>
              ) : null}
            </Button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>{t("title")}</TooltipContent>
      </Tooltip>

      <PopoverContent align="end" className="w-auto p-0">
        <NotificationPanel onNavigate={() => setOpen(false)} />
      </PopoverContent>
    </Popover>
  );
}
