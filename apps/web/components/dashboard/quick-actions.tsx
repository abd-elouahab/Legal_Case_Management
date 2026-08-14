"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { QUICK_ACTION_CHROME } from "@/components/dashboard/labels";
import type { QuickActionKey } from "@/types/dashboard";

/**
 * The dashboard's shortcuts.
 *
 * **The server decides which ones appear**, against the permissions the
 * destination endpoint itself requires — so a shortcut can never be shown to
 * somebody the page behind it would refuse. This component decides only what each
 * one says and where it goes, which is why `19-dashboard-analytics.md`'s *"quick
 * actions should respect authorization"* needs no check in this file: there is
 * nothing here to check, because an action the caller may not use never arrives.
 *
 * That is a deliberate difference from the rest of the app, where a control is
 * usually wrapped in `<Protected>`. A `<Protected>` here would be a *second*
 * policy — one this component would have to keep in step with five different
 * endpoints' requirements — and the first time it drifted, a button would either
 * vanish for somebody entitled to it or 403 for somebody who is not.
 *
 * Rendered in the page header rather than inside the quick-actions widget, so the
 * shortcuts stay reachable from any scroll position. The widget's card carries
 * nothing; it exists in the catalog so the layout can name its position.
 */

export function QuickActions({ actions }: { actions: readonly QuickActionKey[] }) {
  const t = useTranslations("dashboard.quickActions");
  const tWidgets = useTranslations("dashboard.widgets");

  if (actions.length === 0) return null;

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      aria-label={tWidgets("quick_actions.title")}
    >
      {actions.map((key) => {
        const chrome = QUICK_ACTION_CHROME[key];
        const Icon = chrome.icon;

        return (
          <Button key={key} asChild variant="outline" size="sm">
            <Link href={chrome.href}>
              <Icon className="h-4 w-4" aria-hidden="true" />
              {t(key)}
            </Link>
          </Button>
        );
      })}
    </div>
  );
}
