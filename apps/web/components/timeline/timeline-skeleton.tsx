"use client";

import { useTranslations } from "next-intl";

import { Skeleton } from "@/components/ui/skeleton";

/**
 * Loading skeleton for a case timeline.
 *
 * Mirrors the real entry's layout — icon, title, description, meta line — so the
 * section does not reflow when the data arrives. A skeleton that is the wrong
 * shape is worse than a spinner, because it promises a layout it then changes.
 */
export function TimelineSkeleton({ rows = 4 }: { rows?: number }) {
  const t = useTranslations("timeline");

  return (
    <ul
      className="flex flex-col"
      aria-busy="true"
      aria-label={t("loading")}
      data-testid="timeline-skeleton"
    >
      {Array.from({ length: rows }, (_, index) => (
        <li key={index} className="flex gap-3">
          <div className="flex flex-col items-center">
            <Skeleton className="size-8 shrink-0 rounded-full" />
            <span className="mt-1 w-px flex-1 bg-border last:hidden" aria-hidden="true" />
          </div>
          <div className="flex flex-1 flex-col gap-2 pb-6">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-4 w-full max-w-md" />
            <Skeleton className="h-3 w-56" />
          </div>
        </li>
      ))}
    </ul>
  );
}
