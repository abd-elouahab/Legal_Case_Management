"use client";

import Link from "next/link";
import { Bell } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ROUTES } from "@/lib/routes";

/**
 * Notification bell — PLACEHOLDER.
 *
 * Links to the Notifications route and shows a static unread indicator. No real
 * notification data, counts, or real-time updates in this spec — those arrive
 * with the Notifications feature. The unread dot is mocked purely to validate
 * the shell's visual state.
 */
export function NotificationButton() {
  // Mocked "has unread" state — replaced by real notification data later.
  const hasUnread = true;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          asChild
          variant="ghost"
          size="icon"
          aria-label={
            hasUnread ? "Notifications, unread items" : "Notifications"
          }
        >
          <Link href={ROUTES.notifications} className="relative">
            <Bell className="h-5 w-5" />
            {hasUnread ? (
              <span
                aria-hidden="true"
                className="absolute right-2 top-2 h-2 w-2 rounded-full bg-info ring-2 ring-background"
              />
            ) : null}
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent>Notifications</TooltipContent>
    </Tooltip>
  );
}
