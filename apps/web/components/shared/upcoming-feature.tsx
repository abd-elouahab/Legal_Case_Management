"use client";

import { Gavel, Users, type LucideIcon } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";

/**
 * The "this feature is not built yet" panel, for the placeholder pages.
 *
 * **It exists because an icon cannot cross the server boundary.** `EmptyState`
 * became a Client Component when `21-localization.md` gave it translation keys,
 * and a Lucide icon is a *function component* — React refuses to serialize one
 * from a Server Component into a client one. The placeholder pages are Server
 * Components (they export `metadata`), so the icon has to be chosen on this side
 * of the line.
 *
 * A closed registry rather than an icon prop, for the same reason
 * `config/navigation.ts` keeps one: a page names *which* placeholder it is, and
 * a name is a string, which crosses the boundary perfectly well.
 */
const ICONS: Record<string, LucideIcon> = {
  courtUpdates: Gavel,
  lawyers: Users,
};

export function UpcomingFeature({ page }: { page: keyof typeof ICONS | string }) {
  return (
    <EmptyState
      icon={ICONS[page]}
      titleKey={`pages.${page}.emptyTitle`}
      descriptionKey={`pages.${page}.emptyDescription`}
    />
  );
}
