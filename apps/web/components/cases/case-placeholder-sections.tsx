"use client";

import { useTranslations } from "next-intl";
import type { LucideIcon } from "lucide-react";
import { StickyNote } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * Placeholder cards for the modules that will attach to a case.
 *
 * **Layout only — no functionality.** The case workspace's shape exists now so
 * that Notes, the AI Assistant, and Reports each land in a place already reserved
 * for them, rather than forcing a redesign of the details page when the next one
 * ships.
 *
 * Each card says plainly that the feature is not built yet, so nobody mistakes an
 * empty card for a loading failure.
 *
 * **Documents, Timeline, the AI Assistant, and Reports no longer have cards
 * here.** All four modules shipped, so the case details page renders the real
 * list (`components/documents/case-documents`), the real history
 * (`components/timeline/case-timeline`), the real assistant
 * (`components/ai/case-assistant`), and the real report history
 * (`components/reports/case-reports`) in their place — a placeholder beside a
 * working feature would read as a bug.
 *
 * **One card left.** When Notes ships this file goes with it: a section headed
 * "Coming to this case" with nothing under it is worse than no section.
 */

/**
 * The sections that do not exist yet, by key.
 *
 * Their words live in `cases.upcoming.<key>` — a placeholder that stayed English
 * on an Arabic screen would read as a rendering fault rather than as a feature
 * nobody has built.
 */
const SECTIONS: { key: string; icon: LucideIcon }[] = [
  { key: "notes", icon: StickyNote },
];

export function CasePlaceholderSections() {
  const t = useTranslations("cases.upcoming");

  return (
    <section className="flex flex-col gap-4" aria-labelledby="case-upcoming-modules">
      <h3 id="case-upcoming-modules" className="text-sm font-medium text-muted-foreground">
        {t("heading")}
      </h3>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {SECTIONS.map(({ key, icon: Icon }) => (
          <Card key={key} className="border-dashed">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Icon className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                {t(`${key}.title`)}
              </CardTitle>
              <CardDescription>{t(`${key}.description`)}</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground">{t("availability")}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}
