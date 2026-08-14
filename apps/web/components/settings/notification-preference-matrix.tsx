"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
 * A preference matrix over a chosen set of delivery channels.
 *
 * **The Settings module stores nothing for this**, and that is the point.
 * `20-settings.md` requires that *"notification preferences should integrate with
 * the Notification Service"* and that *"the Settings module should not contain
 * delivery logic"* — so both panels below read and write
 * `/notifications/preferences`, the endpoint that feature owns, through that
 * feature's own hooks. There is no `/settings/notifications`, and adding one would
 * create a second path to one stored thing.
 *
 * **Two sections over one store, split by axis.** The spec asks for *Notification
 * Preferences* (which notifications you receive) and *Communication Preferences*
 * (how they reach you) as separate sections; the platform stores one row per
 * `(user, preference)` with a boolean per channel, so the two sections are two
 * projections of that grid rather than two stores. This component takes the
 * channels to render, and is used twice.
 *
 * **Each switch saves itself, and saves only itself.** Each request carries only
 * the channel that changed, which is what stops toggling Email from silently
 * rewriting In-app — the API's per-channel partial update honoured by the caller.
 * The failure path restores the previous value and says so, which is what makes
 * an immediate save safe.
 *
 * Note what the outbound channels do **not** promise: switching one on does not
 * mean every notification of that kind arrives there. Only the kinds the platform
 * marks for that channel travel on it, and WhatsApp additionally needs a phone
 * number on the account.
 */

export interface NotificationPreferenceMatrixProps {
  /** Already translated by the caller. */
  title: string;
  description: string;
  /**
   * Which channel columns to render, by key.
   *
   * Keys rather than `{key, label}` pairs, since `21-localization.md`: a column
   * heading is a word, and a word that travels as a prop is a word a caller had
   * to hardcode. The headings live in `notifications.channels`, shared with the
   * notification centre's own copy of this grid.
   */
  channels: readonly NotificationChannel[];
  /** Prefix for input ids, so both panels can coexist on one page. */
  idPrefix: string;
}

export function NotificationPreferenceMatrix({
  title,
  description,
  channels,
  idPrefix,
}: NotificationPreferenceMatrixProps) {
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
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>

      <CardContent className="flex flex-col gap-1 p-0">
        <div className="flex items-center gap-4 px-6 pb-1 text-xs font-medium text-muted-foreground">
          <span className="min-w-0 flex-1" />
          {channels.map((channel) => (
            <span key={channel} className="w-16 shrink-0 text-center" aria-hidden="true">
              {tChannels(channel)}
            </span>
          ))}
        </div>

        {data.map((preference, index) => {
          // A preference key the server added and this build has no copy for
          // resolves through the provider's fallback to a humanized form of
          // itself, so a new preference is legible before it is translated.
          const rowTitle = tPreferences(`${preference.preferenceKey}.title`);
          const rowId = `${idPrefix}-${preference.preferenceKey}`;

          return (
            <React.Fragment key={preference.preferenceKey}>
              {index > 0 ? <Separator /> : null}
              <div className="flex items-start gap-4 px-6 py-4">
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span id={`${rowId}-label`} className="text-sm font-medium">
                    {rowTitle}
                  </span>
                  <p id={`${rowId}-description`} className="text-sm text-muted-foreground">
                    {tPreferences(`${preference.preferenceKey}.description`)}
                    {preference.isDefault ? ` ${tStates("platformDefault")}` : ""}
                  </p>
                </div>

                {channels.map((channel) => (
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
                      // carries its full name — the preference *and* the channel.
                      aria-label={t("channelToggle", {
                        preference: rowTitle,
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

/** The Notifications section: *what* the platform tells you about. */
export function NotificationSettingsPanel() {
  const t = useTranslations("settings.preferenceSections");

  return (
    <NotificationPreferenceMatrix
      title={t("notifications.title")}
      description={t("notifications.description")}
      channels={["inApp"]}
      idPrefix="notification-preference"
    />
  );
}

/** The Communication section: *how* those notifications reach you. */
export function CommunicationSettingsPanel() {
  const t = useTranslations("settings.preferenceSections");

  return (
    <NotificationPreferenceMatrix
      title={t("communication.title")}
      description={t("communication.description")}
      channels={["email", "whatsapp"]}
      idPrefix="communication-preference"
    />
  );
}
