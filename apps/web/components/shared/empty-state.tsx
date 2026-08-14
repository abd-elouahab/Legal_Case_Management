"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import type { LucideIcon } from "lucide-react";
import { Inbox } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Empty state.
 *
 * Reusable placeholder for lists/sections with no data. Optional `action` slot
 * (e.g. a "Create" button) and a customizable icon.
 *
 * **Two ways to give it words**, for the reason `page-header.tsx` records: a
 * *section* that is empty has copy which belongs in a catalogue (`titleKey`), while
 * a **search that found nothing** has copy naming the query somebody typed, which
 * is data (`title`). The keys here are **fully qualified** — `cases.empty.title`
 * rather than `empty.title` — because this component is used by a dozen modules and
 * cannot have a namespace of its own without every caller passing one anyway.
 *
 * Falls back to the shared `common.states` wording when given neither, so a caller
 * that has nothing specific to say still says something in the reader's language.
 */
export function EmptyState({
  icon: Icon = Inbox,
  title,
  titleKey,
  description,
  descriptionKey,
  action,
  className,
}: {
  icon?: LucideIcon;
  /** An already-resolved heading — use for anything naming user input or a row. */
  title?: string;
  /** A fully-qualified catalogue key, e.g. `"cases.empty.title"`. */
  titleKey?: string;
  description?: string;
  /** A fully-qualified catalogue key. */
  descriptionKey?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  // No namespace: the keys callers pass are absolute paths into the catalogue.
  const t = useTranslations();
  const tStates = useTranslations("common.states");

  const heading = titleKey ? t(titleKey) : (title ?? tStates("emptyTitle"));
  const subheading = descriptionKey ? t(descriptionKey) : description;

  return (
    <div
      className={cn(
        "flex min-h-64 flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border p-8 text-center",
        className,
      )}
    >
      <span className="flex size-16 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <Icon className="h-10 w-10" aria-hidden="true" />
      </span>
      <div className="flex flex-col gap-1">
        <h3 className="text-base font-medium text-foreground">{heading}</h3>
        {subheading ? (
          <p className="max-w-sm text-sm text-muted-foreground">{subheading}</p>
        ) : null}
      </div>
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}
