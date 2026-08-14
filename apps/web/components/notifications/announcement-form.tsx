"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { Megaphone } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  useNotificationErrorMessage,
  usePublishAnnouncement,
} from "@/hooks/use-notifications";
import { MAX_ANNOUNCEMENT_LENGTH } from "@/lib/validation/notification";
import type { AnnouncementKind } from "@/types/notification";

/**
 * The platform announcement form. Requires `notifications:manage`.
 *
 * **The only place in the application that creates a notification.** Everything
 * else in a feed arrived through the API's event dispatcher, from business
 * modules that do not know notifications exist; an announcement has no business
 * action behind it and no resource it is about, so there is no event it could
 * have been published as.
 *
 * The result is reported in full — how many people were notified *and* how many
 * have announcements switched off — because "12 recipients" alone leaves an
 * administrator unable to tell a quiet platform from a silenced one.
 *
 * The character counter is a live count rather than a validation message: the
 * limit is the API's, and a person writing a maintenance notice should see the
 * room they have left rather than discover it when the submission is refused.
 */

export function AnnouncementForm() {
  const t = useTranslations("notifications.announcement");
  const errorMessage = useNotificationErrorMessage();
  const [kind, setKind] = React.useState<AnnouncementKind>("announcement");
  const [message, setMessage] = React.useState("");

  const publish = usePublishAnnouncement();
  const trimmed = message.trim();
  const tooLong = trimmed.length > MAX_ANNOUNCEMENT_LENGTH;
  const canSubmit = trimmed.length > 0 && !tooLong && !publish.isPending;

  function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;

    publish.mutate(
      { kind, message: trimmed },
      {
        onSuccess: (result) => {
          setMessage("");
          toast.success(
            t("sent", { count: result.recipients }) +
              (result.skipped > 0 ? ` ${t("skipped", { count: result.skipped })}` : ""),
          );
        },
        onError: (failure) => toast.error(errorMessage(failure)),
      },
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Megaphone className="h-4 w-4" aria-hidden="true" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>

      <CardContent>
        <form className="flex flex-col gap-4" onSubmit={onSubmit}>
          <div className="flex flex-col gap-2">
            <Label htmlFor="announcement-kind">{t("kind")}</Label>
            <Select
              value={kind}
              onValueChange={(value) => setKind(value as AnnouncementKind)}
            >
              <SelectTrigger id="announcement-kind" className="w-full sm:w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="announcement">{t("kinds.announcement")}</SelectItem>
                <SelectItem value="maintenance">{t("kinds.maintenance")}</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">{t("kindHint")}</p>
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="announcement-message">{t("message")}</Label>
            <Textarea
              id="announcement-message"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              rows={4}
              placeholder={t("messagePlaceholder")}
              aria-describedby="announcement-message-count"
              aria-invalid={tooLong}
            />
            <p
              id="announcement-message-count"
              className={
                tooLong ? "text-sm text-destructive" : "text-sm text-muted-foreground"
              }
            >
              {t("characterCount", {
                used: trimmed.length,
                max: MAX_ANNOUNCEMENT_LENGTH,
              })}
            </p>
          </div>

          <div className="flex justify-end">
            <Button type="submit" disabled={!canSubmit}>
              {publish.isPending ? t("sending") : t("submit")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
