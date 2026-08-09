"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Separator } from "@/components/ui/separator";
import {
  notificationErrorMessage,
  useNotificationPreferences,
  useUpdateNotificationPreferences,
} from "@/hooks/use-notifications";
import {
  NOTIFICATION_PREFERENCE_LABELS,
  type NotificationChannel,
  type NotificationPreferenceKey,
} from "@/types/notification";

/**
 * The notification preference form.
 *
 * One row per preference the platform offers and **one switch per delivery
 * channel**, rendered from what the server sends rather than from a list in this
 * file: the API returns the complete set with the caller's answer on each
 * channel, so a preference — or a channel — added later appears here at its
 * default with no frontend change.
 *
 * **Each switch saves itself, and saves only itself.** There is no "Save"
 * button, because there is nothing to lose by saving immediately — a preference
 * is one boolean, and a form that batched them would let somebody close the page
 * believing they had switched notifications off. Each request carries **only the
 * channel that changed**, which is what stops toggling "Email" from silently
 * rewriting "In app". The failure path restores the previous value and says so,
 * which is the behaviour that makes an immediate save safe.
 *
 * "Default" is stated rather than implied. An account that has never opened this
 * page has no stored row at all, and showing a switch as on without saying it is
 * the platform's choice would suggest somebody made it.
 *
 * Note what the email column does **not** promise: switching it on does not mean
 * every notification of that kind arrives by email. Only the kinds the platform
 * marks for email delivery travel on that channel — a case assignment and a
 * hearing change do, a document upload deliberately does not — which the
 * description below the heading says rather than leaving somebody to discover.
 */

/** The columns, in the order they are rendered. */
const CHANNELS: ReadonlyArray<{ key: NotificationChannel; label: string }> = [
  { key: "inApp", label: "In app" },
  { key: "email", label: "Email" },
];

export function NotificationPreferencesForm() {
  const { data, isPending, isError, error } = useNotificationPreferences();
  const update = useUpdateNotificationPreferences();

  const onToggle = React.useCallback(
    (
      preferenceKey: NotificationPreferenceKey,
      channel: NotificationChannel,
      enabled: boolean,
    ) => {
      update.mutate([{ preferenceKey, [channel]: enabled }], {
        onError: (failure) => toast.error(notificationErrorMessage(failure)),
      });
    },
    [update],
  );

  if (isPending) {
    return (
      <Card>
        <CardContent className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Loading your preferences…
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardContent className="py-8">
          <p role="alert" className="text-sm text-destructive">
            {notificationErrorMessage(error)}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Notification preferences</CardTitle>
        <CardDescription>
          Choose what the platform tells you about, and how it reaches you. Switching
          something off stops new notifications of that kind — the ones already in your
          feed stay where they are. Only important updates are sent by email; routine
          activity stays in the app whatever you choose here.
        </CardDescription>
      </CardHeader>

      <CardContent className="flex flex-col gap-1 p-0">
        <div className="flex items-center gap-4 px-6 pb-1 text-xs font-medium text-muted-foreground">
          <span className="flex-1" />
          {CHANNELS.map((channel) => (
            <span key={channel.key} className="w-16 text-center" aria-hidden="true">
              {channel.label}
            </span>
          ))}
        </div>

        {data.map((preference, index) => {
          const labels = NOTIFICATION_PREFERENCE_LABELS[preference.preferenceKey];
          const rowId = `notification-preference-${preference.preferenceKey}`;

          return (
            <React.Fragment key={preference.preferenceKey}>
              {index > 0 ? <Separator /> : null}
              <div className="flex items-start gap-4 px-6 py-4">
                <div className="flex flex-1 flex-col gap-0.5">
                  <span id={`${rowId}-label`} className="text-sm font-medium">
                    {labels?.title ?? preference.preferenceKey}
                  </span>
                  <p id={`${rowId}-description`} className="text-sm text-muted-foreground">
                    {labels?.description}
                    {preference.isDefault ? " (platform default)" : ""}
                  </p>
                </div>

                {CHANNELS.map((channel) => (
                  <div key={channel.key} className="flex w-16 justify-center pt-0.5">
                    <Checkbox
                      id={`${rowId}-${channel.key}`}
                      checked={preference[channel.key]}
                      disabled={update.isPending}
                      onCheckedChange={(checked) =>
                        onToggle(preference.preferenceKey, channel.key, checked === true)
                      }
                      // The visible column heading is decorative (this is a grid
                      // rather than a table, so there is no header relationship a
                      // screen reader could follow), which is why each checkbox
                      // carries its full name — the preference *and* the channel —
                      // rather than relying on one.
                      aria-label={`${labels?.title ?? preference.preferenceKey} — ${channel.label}`}
                      aria-describedby={`${rowId}-description`}
                    />
                  </div>
                ))}
              </div>
            </React.Fragment>
          );
        })}
      </CardContent>
    </Card>
  );
}
