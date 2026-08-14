"use client";

import { Info, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { SettingsSectionCard } from "@/components/settings/settings-section-card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
import {
  useSettingsErrorMessage,
  usePlatformSettings,
  useUpdatePlatformSettings,
} from "@/hooks/use-settings";
import type { SettingChange } from "@/types/settings";

/**
 * The Administration section: the deployment's own configuration.
 *
 * **Isolated from the rest of the page by three things at once** — a separate
 * table, a separate registry, and `settings:manage`, which no role but
 * administrator holds. That is `20-settings.md`'s *"administrator settings should
 * remain isolated from regular user settings"*, and it is structural rather than
 * a rule anybody has to remember: this panel reads a different endpoint, and the
 * API does not serve the section descriptor at all to a caller who cannot manage
 * it.
 *
 * Every `default_*` setting here is the fallback for the matching user setting,
 * which is what makes them do something rather than merely be stored: an account
 * that has expressed no opinion follows the platform's answer, and changing one
 * reaches every such account at once — with no backfill, because there is nothing
 * stored to back-fill.
 *
 * **Maintenance mode announces; it does not close the platform.** Turning it on
 * puts a notice on every authenticated client and refuses no request. That is
 * stated on the page rather than left to be discovered, because a switch labelled
 * "maintenance mode" that quietly did nothing to traffic would be worse than one
 * that says what it does.
 */

export function AdministrationSettingsPanel({ canManage }: { canManage: boolean }) {
  const { data, isPending, isError, error } = usePlatformSettings(canManage);
  const update = useUpdatePlatformSettings();
  const t = useTranslations("settings.administration");
  const errorMessage = useSettingsErrorMessage();

  // The API omits this section for a caller who cannot manage it, so this is a
  // second guard rather than the first — and it is here because a client that
  // rendered an administrative panel it could not populate would be a client
  // telling every lawyer which platform settings exist.
  if (!canManage) return null;

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

  const save = (changes: SettingChange[]) => update.mutateAsync(changes);

  return (
    <div className="flex flex-col gap-6">
      <Alert>
        <Info className="h-4 w-4" aria-hidden="true" />
        {/* One message rather than a sentence wrapped around an <em>: markup
            inside a translated sentence is a fragment a translator cannot move,
            and the emphasis was carrying no meaning the words do not. */}
        <AlertDescription>{t("notice")}</AlertDescription>
      </Alert>

      <SettingsSectionCard
        section="administration"
        collection={data}
        onChange={save}
        isSaving={update.isPending}
        idPrefix="platform-setting"
        title={t("title")}
        description={t("description")}
      />
    </div>
  );
}
