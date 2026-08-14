"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { Loader2, Wrench } from "lucide-react";

import { AdministrationSettingsPanel } from "@/components/settings/administration-settings-panel";

import {
  CommunicationSettingsPanel,
  NotificationSettingsPanel,
} from "@/components/settings/notification-preference-matrix";
import { ProfileSettingsForm } from "@/components/settings/profile-settings-form";
import { SecuritySettingsPanel } from "@/components/settings/security-settings-panel";
import { SettingsSectionCard } from "@/components/settings/settings-section-card";
import { ErrorState } from "@/components/shared/error-state";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { usePermissions } from "@/hooks/use-permissions";
import {
  useSettingsErrorMessage,
  useSettingsOverview,
  useUpdateSettings,
} from "@/hooks/use-settings";
import { cn } from "@/lib/utils";
import { PERMISSION } from "@/types/authorization";
import type { SettingChange, SettingsSection } from "@/types/settings";

/**
 * The Settings page.
 *
 * **The navigation is built from what the API served, not from a list here.**
 * `20-settings.md` requires that *"the implementation should support future
 * sections without redesign"*, and a section list written in this file would have
 * to be edited every time one was added — which is precisely that redesign. The
 * API returns an ordered list of section descriptors, so a tenth section reaches
 * a browser nobody redeployed. This component maps a section *key* onto a panel,
 * and a key it has no panel for is skipped rather than rendered blank; that is
 * the one place a new section still needs client work, and it needs only the
 * panel.
 *
 * **An administrative section never arrives for a caller who cannot manage it.**
 * The API omits it entirely rather than serving it disabled, so this component
 * cannot show every lawyer which platform settings exist and that somebody else
 * controls them.
 *
 * **One request feeds four of the panels; two fetch their own.** Profile,
 * Appearance, Language & Region, AI, and Dashboard come from `GET /settings`.
 * Notifications and Communication read `/notifications/preferences` — the
 * Notification Service owns those, and the Settings page presents them rather
 * than storing them, which is the spec's ownership rule visible in the network
 * tab.
 *
 * Responsive per `ui-context.md`: the section list is a sidebar on a laptop and a
 * horizontally-scrolling row of chips on a phone.
 */

/** Sections rendered by the generic, server-described card. */
const GENERIC_SECTIONS: ReadonlySet<SettingsSection> = new Set<SettingsSection>([
  "appearance",
  "language",
  "ai",
  "dashboard",
]);

export function SettingsWorkspace() {
  const { data, isPending, isError, error, refetch } = useSettingsOverview();
  const updateSettings = useUpdateSettings();
  const { can } = usePermissions();
  const t = useTranslations("settings.workspace");
  const tSections = useTranslations("settings.sections");
  const errorMessage = useSettingsErrorMessage();

  const canManage = can(PERMISSION.settingsManage);
  const [active, setActive] = React.useState<SettingsSection | null>(null);

  const sections = React.useMemo(() => data?.sections ?? [], [data]);
  const current = active ?? sections[0]?.section ?? "profile";

  const saveSettings = React.useCallback(
    (changes: SettingChange[]) => updateSettings.mutateAsync(changes),
    [updateSettings],
  );

  if (isPending) {
    return (
      <Card>
        <CardContent className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          {t("loading")}
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <ErrorState
        title={t("loadFailed")}
        description={errorMessage(error)}
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {data.maintenance.maintenanceMode ? (
        <Alert>
          <Wrench className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            {data.maintenance.message ?? t("maintenanceFallback")}
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="flex flex-col gap-6 lg:flex-row lg:gap-8">
        {/* A `nav` with real buttons rather than links: the panels are one page
            with no separate URL, so a route per section would be five routes
            rendering the same query. */}
        <nav
          aria-label={t("sectionsLabel")}
          className="-mx-1 flex gap-1 overflow-x-auto pb-1 lg:mx-0 lg:w-56 lg:shrink-0 lg:flex-col lg:overflow-visible lg:pb-0"
        >
          {sections.map((descriptor) => {
            const isActive = descriptor.section === current;

            return (
              <Button
                key={descriptor.section}
                type="button"
                variant="ghost"
                aria-current={isActive ? "page" : undefined}
                className={cn(
                  "shrink-0 justify-start whitespace-nowrap",
                  isActive && "bg-accent text-accent-foreground",
                )}
                onClick={() => setActive(descriptor.section)}
              >
                {tSections(`${descriptor.section}.title`)}
              </Button>
            );
          })}
        </nav>

        <div className="min-w-0 flex-1">
          {current === "profile" ? <ProfileSettingsForm profile={data.profile} /> : null}

          {current === "security" ? (
            <SecuritySettingsPanel mustChangePassword={data.profile.mustChangePassword} />
          ) : null}

          {current === "notifications" ? <NotificationSettingsPanel /> : null}
          {current === "communication" ? <CommunicationSettingsPanel /> : null}

          {GENERIC_SECTIONS.has(current) ? (
            <SettingsSectionCard
              section={current}
              collection={data.settings}
              onChange={saveSettings}
              isSaving={updateSettings.isPending}
              idPrefix="user-setting"
            />
          ) : null}

          {current === "administration" ? (
            <AdministrationSettingsPanel canManage={canManage} />
          ) : null}
        </div>
      </div>
    </div>
  );
}
