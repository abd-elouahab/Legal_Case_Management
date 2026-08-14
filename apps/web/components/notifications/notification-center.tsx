"use client";

import { AnnouncementForm } from "@/components/notifications/announcement-form";
import { useTranslations } from "next-intl";
import { NotificationFeed } from "@/components/notifications/notification-feed";
import { NotificationPreferencesForm } from "@/components/notifications/notification-preferences-form";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useCurrentUser } from "@/hooks/use-current-user";
import { usePermissions } from "@/hooks/use-permissions";
import { useRealtimeResource } from "@/hooks/use-realtime";
import { PERMISSION } from "@/types/authorization";

/**
 * The notification centre: the feed, the preferences, and — for an administrator
 * — the announcement form.
 *
 * **Tabs rather than three stacked sections**, because they are three different
 * activities with three different frequencies: reading the feed is daily,
 * changing a preference is once, and publishing an announcement is rare and
 * administrative. Stacking them would put a form almost nobody may use above the
 * list everybody reads.
 *
 * The announcement tab is gated on `notifications:manage`, which lawyers and
 * court representatives do not hold. **That gate is presentation only** — the API
 * authorizes the request independently, so hiding the tab is a courtesy rather
 * than a security boundary, exactly as `<Protected>` is everywhere else.
 *
 * It subscribes to the reader's own topic for the same reason the bell does: an
 * open feed should grow as notifications arrive rather than when somebody
 * refreshes. Subscribing twice is safe — the server treats a repeat subscription
 * as a refreshed grant rather than a duplicate.
 */
export function NotificationCenter() {
  const t = useTranslations("notifications.center");
  const user = useCurrentUser();
  const { can, isLoading } = usePermissions();

  const canAnnounce = !isLoading && can(PERMISSION.notificationsManage);

  useRealtimeResource("user", user?.id ?? null);

  return (
    <Tabs defaultValue="feed" className="flex flex-col gap-4">
      <TabsList>
        <TabsTrigger value="feed">{t("all")}</TabsTrigger>
        <TabsTrigger value="preferences">{t("preferences")}</TabsTrigger>
        {canAnnounce ? <TabsTrigger value="announce">{t("announce")}</TabsTrigger> : null}
      </TabsList>

      <TabsContent value="feed">
        <NotificationFeed />
      </TabsContent>

      <TabsContent value="preferences">
        <NotificationPreferencesForm />
      </TabsContent>

      {canAnnounce ? (
        <TabsContent value="announce">
          <AnnouncementForm />
        </TabsContent>
      ) : null}
    </Tabs>
  );
}
