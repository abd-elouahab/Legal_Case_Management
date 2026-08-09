"use client";

import * as React from "react";
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
  notificationErrorMessage,
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
            `Announcement sent to ${result.recipients} ${
              result.recipients === 1 ? "person" : "people"
            }.` +
              (result.skipped > 0
                ? ` ${result.skipped} have platform announcements switched off.`
                : ""),
          );
        },
        onError: (failure) => toast.error(notificationErrorMessage(failure)),
      },
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Megaphone className="h-4 w-4" aria-hidden="true" />
          Platform announcement
        </CardTitle>
        <CardDescription>
          Sent to every active account, as an in-app notification. People who have switched
          platform announcements off are not notified.
        </CardDescription>
      </CardHeader>

      <CardContent>
        <form className="flex flex-col gap-4" onSubmit={onSubmit}>
          <div className="flex flex-col gap-2">
            <Label htmlFor="announcement-kind">Kind</Label>
            <Select
              value={kind}
              onValueChange={(value) => setKind(value as AnnouncementKind)}
            >
              <SelectTrigger id="announcement-kind" className="w-full sm:w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="announcement">Announcement</SelectItem>
                <SelectItem value="maintenance">Scheduled maintenance</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              Maintenance is delivered as a high-priority warning; an announcement is
              ordinary news.
            </p>
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="announcement-message">Message</Label>
            <Textarea
              id="announcement-message"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              rows={4}
              placeholder="The platform will be read-only on Sunday between 08:00 and 10:00."
              aria-describedby="announcement-message-count"
              aria-invalid={tooLong}
            />
            <p
              id="announcement-message-count"
              className={
                tooLong ? "text-sm text-destructive" : "text-sm text-muted-foreground"
              }
            >
              {trimmed.length} / {MAX_ANNOUNCEMENT_LENGTH} characters
            </p>
          </div>

          <div className="flex justify-end">
            <Button type="submit" disabled={!canSubmit}>
              {publish.isPending ? "Sending…" : "Send announcement"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
