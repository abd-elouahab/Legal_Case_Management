"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import {
  notificationErrorMessage,
  useNotificationPreferences,
  useUpdateNotificationPreferences,
} from "@/hooks/use-notifications";
import {
  NOTIFICATION_PREFERENCE_LABELS,
  type NotificationPreferenceKey,
} from "@/types/notification";

/**
 * The notification preference form.
 *
 * One switch per preference the platform offers, rendered from **what the server
 * sends** rather than from a list in this file: the API returns the complete set
 * with the caller's answer to each, so a preference added later appears here at
 * its default with no frontend change.
 *
 * **Each switch saves itself.** There is no "Save" button, because there is
 * nothing to lose by saving immediately — a preference is one boolean, the
 * request carries only what changed, and a form that batched them would let
 * somebody close the page believing they had switched notifications off. The
 * failure path restores the previous value and says so, which is the behaviour
 * that makes an immediate save safe.
 *
 * "Default" is stated rather than implied. An account that has never opened this
 * page has no stored row at all, and showing a switch as on without saying it is
 * the platform's choice would suggest somebody made it.
 */

export function NotificationPreferencesForm() {
  const { data, isPending, isError, error } = useNotificationPreferences();
  const update = useUpdateNotificationPreferences();

  const onToggle = React.useCallback(
    (preferenceKey: NotificationPreferenceKey, inApp: boolean) => {
      update.mutate([{ preferenceKey, inApp }], {
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
          Choose what the platform tells you about. Switching something off stops new
          notifications of that kind — the ones already in your feed stay where they are.
        </CardDescription>
      </CardHeader>

      <CardContent className="flex flex-col gap-1 p-0">
        {data.map((preference, index) => {
          const labels = NOTIFICATION_PREFERENCE_LABELS[preference.preferenceKey];
          const id = `notification-preference-${preference.preferenceKey}`;

          return (
            <React.Fragment key={preference.preferenceKey}>
              {index > 0 ? <Separator /> : null}
              <div className="flex items-start gap-3 px-6 py-4">
                <Checkbox
                  id={id}
                  checked={preference.inApp}
                  disabled={update.isPending}
                  onCheckedChange={(checked) =>
                    onToggle(preference.preferenceKey, checked === true)
                  }
                  aria-describedby={`${id}-description`}
                />
                <div className="flex flex-col gap-0.5">
                  <Label htmlFor={id} className="text-sm font-medium">
                    {labels?.title ?? preference.preferenceKey}
                  </Label>
                  <p id={`${id}-description`} className="text-sm text-muted-foreground">
                    {labels?.description}
                    {preference.isDefault ? " (platform default)" : ""}
                  </p>
                </div>
              </div>
            </React.Fragment>
          );
        })}
      </CardContent>
    </Card>
  );
}
