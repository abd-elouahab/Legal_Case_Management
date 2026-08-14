"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Separator } from "@/components/ui/separator";
import {
  useNotificationErrorMessage,
  useNotificationPreferences,
  useUpdateNotificationPreferences,
} from "@/hooks/use-notifications";
import {
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
 * Note what the outbound columns do **not** promise: switching one on does not
 * mean every notification of that kind arrives there. Only the kinds the platform
 * marks for that channel travel on it — a case assignment and a hearing change
 * do, a document upload deliberately does not — and WhatsApp additionally needs a
 * phone number on the account, which is optional here. The description below the
 * heading says both rather than leaving somebody to discover them.
 */

/** The columns, in the order they are rendered. */
/**
 * The delivery channels, in column order.
 *
 * Keys only: the column headings live in `notifications.channels`, because "In
 * app" is a phrase and `WhatsApp` is a product name that stays as it is in every
 * language — which is a distinction only a catalogue can express.
 */
const CHANNELS: readonly NotificationChannel[] = ["inApp", "email", "whatsapp"];

export function NotificationPreferencesForm() {
  const { data, isPending, isError, error } = useNotificationPreferences();
  const update = useUpdateNotificationPreferences();
  const t = useTranslations("notifications.preferencesForm");
  const tPreferences = useTranslations("notifications.preferences");
  const tChannels = useTranslations("notifications.channels");
  const tStates = useTranslations("common.states");
  const errorMessage = useNotificationErrorMessage();

  const onToggle = React.useCallback(
    (
      preferenceKey: NotificationPreferenceKey,
      channel: NotificationChannel,
      enabled: boolean,
    ) => {
      update.mutate([{ preferenceKey, [channel]: enabled }], {
        onError: (failure) => toast.error(errorMessage(failure)),
      });
    },
    [errorMessage, update],
  );

  if (isPending) {
    return (
      <Card>
        <CardContent className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          {t("loading")}
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardContent className="py-8">
          <p role="alert" className="text-sm text-destructive">
            {errorMessage(error)}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>

      <CardContent className="flex flex-col gap-1 p-0">
        <div className="flex items-center gap-4 px-6 pb-1 text-xs font-medium text-muted-foreground">
          <span className="min-w-0 flex-1" />
          {CHANNELS.map((channel) => (
            // `shrink-0`, because a third column arrived: without it the labels
            // compress before the description does, and "WhatsApp" is the first
            // one to wrap.
            <span key={channel} className="w-16 shrink-0 text-center" aria-hidden="true">
              {tChannels(channel)}
            </span>
          ))}
        </div>

        {data.map((preference, index) => {
          // A preference key the server added and this build has no copy for
          // resolves through the provider's fallback to a humanized form of
          // itself, so a new preference is legible before it is translated.
          const title = tPreferences(`${preference.preferenceKey}.title`);
          const rowId = `notification-preference-${preference.preferenceKey}`;

          return (
            <React.Fragment key={preference.preferenceKey}>
              {index > 0 ? <Separator /> : null}
              <div className="flex items-start gap-4 px-6 py-4">
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span id={`${rowId}-label`} className="text-sm font-medium">
                    {title}
                  </span>
                  <p id={`${rowId}-description`} className="text-sm text-muted-foreground">
                    {tPreferences(`${preference.preferenceKey}.description`)}
                    {preference.isDefault ? ` ${tStates("platformDefault")}` : ""}
                  </p>
                </div>

                {CHANNELS.map((channel) => (
                  <div key={channel} className="flex w-16 shrink-0 justify-center pt-0.5">
                    <Checkbox
                      id={`${rowId}-${channel}`}
                      checked={preference[channel]}
                      disabled={update.isPending}
                      onCheckedChange={(checked) =>
                        onToggle(preference.preferenceKey, channel, checked === true)
                      }
                      // The visible column heading is decorative (this is a grid
                      // rather than a table, so there is no header relationship a
                      // screen reader could follow), which is why each checkbox
                      // carries its full name — the preference *and* the channel —
                      // rather than relying on one.
                      aria-label={t("channelToggle", {
                        preference: title,
                        channel: tChannels(channel),
                      })}
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
