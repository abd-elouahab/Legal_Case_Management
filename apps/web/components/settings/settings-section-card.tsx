"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";


import { SettingControl } from "@/components/settings/setting-control";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { useSettingsErrorMessage } from "@/hooks/use-settings";
import type {
  SettingChange,
  SettingValue,
  SettingsCollection,
  SettingsSection,
} from "@/types/settings";

/**
 * Every setting belonging to one section, rendered from what the API sent.
 *
 * **One component serves Appearance, Language & Region, AI, Dashboard, and
 * Administration**, because none of them is structurally different: each is a
 * list of settings whose controls are described by the server. A panel per
 * section would be five panels and a sixth for every section added — which is the
 * redesign `20-settings.md` asks the implementation to avoid, applied to the
 * client rather than only to the storage.
 *
 * **Each control saves itself, and saves only itself.** A change is sent as a
 * one-entry list, which is what stops toggling streaming from also rewriting a
 * theme somebody changed in another tab — the API's *"a list of changes rather
 * than the whole set"* honoured by the caller as well as offered by the server.
 *
 * **The failure path is the reason an immediate save is safe.** A rejected change
 * restores the previous value (the mutation never wrote it locally, so this is
 * simply not applying it) and says why, using the server's own per-field message
 * rather than a generic sentence.
 */

export interface SettingsSectionCardProps {
  section: SettingsSection;
  collection: SettingsCollection;
  /** Applies one change. The owner decides which endpoint that reaches. */
  onChange: (changes: SettingChange[]) => Promise<unknown>;
  isSaving: boolean;
  /** Prefix for input ids, so user and platform settings can share a page. */
  idPrefix: string;
  /** Overrides the section's own heading — used by the Administration panel. */
  title?: string;
  description?: string;
}

export function SettingsSectionCard({
  section,
  collection,
  onChange,
  isSaving,
  idPrefix,
  title,
  description,
}: SettingsSectionCardProps) {
  const t = useTranslations("settings.sections");
  const errorMessage = useSettingsErrorMessage();

  const definitions = React.useMemo(
    () => new Map(collection.definitions.map((entry) => [entry.key, entry])),
    [collection.definitions],
  );

  const rows = collection.settings.filter((setting) => setting.section === section);

  const save = React.useCallback(
    (key: string, value: SettingValue) => {
      void onChange([{ key, value }]).catch((error: unknown) => {
        toast.error(errorMessage(error));
      });
    },
    [errorMessage, onChange],
  );

  if (rows.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title ?? t(`${section}.title`)}</CardTitle>
        <CardDescription>
          {description ?? t(`${section}.description`)}
        </CardDescription>
      </CardHeader>

      <CardContent className="flex flex-col px-6 py-0">
        {rows.map((setting, index) => {
          const definition = definitions.get(setting.key);
          // A setting with no definition cannot be rendered — there is nothing to
          // say what control it needs. Skipping is the honest outcome and it
          // cannot happen while the API serves both from one registry; the guard
          // exists so a partial response degrades into a missing row rather than
          // a page that throws.
          if (!definition) return null;

          return (
            <React.Fragment key={setting.key}>
              {index > 0 ? <Separator /> : null}
              <SettingControl
                definition={definition}
                value={setting.value}
                isDefault={setting.isDefault}
                disabled={isSaving}
                idPrefix={idPrefix}
                onChange={(value) => save(setting.key, value)}
              />
            </React.Fragment>
          );
        })}
      </CardContent>
    </Card>
  );
}
