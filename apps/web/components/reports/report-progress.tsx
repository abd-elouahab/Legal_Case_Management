"use client";

import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import type { Report } from "@/types/report";

/**
 * Progress indicator for a report being generated.
 *
 * ``14-ai-report-agent.md`` asks for a progress indicator and for the experience
 * to *"remain responsive during long-running report generation"*. Three decisions
 * follow from what a report actually is:
 *
 * * **the denominator is real, and it is the server's.** `sectionsTotal` is
 *   published when the run is queued, so the bar has a true scale from the first
 *   poll rather than being an indeterminate stripe that says only "something is
 *   happening";
 * * **the numerator is sections, not seconds.** "3 of 7 sections" is a statement
 *   about the work; a time estimate would be a guess about a language model's
 *   latency, and a wrong one is worse than none;
 * * **a queued run says so** rather than showing 0%. Zero on a bar reads as
 *   "started and got nowhere", which is a different and more alarming thing than
 *   "waiting for a worker".
 *
 * The bar is an ARIA `progressbar` with its real values, and the same figures are
 * written as text beside it — so the state is never conveyed by the bar alone.
 */
export function ReportProgress({
  report,
  className,
}: {
  report: Report;
  className?: string;
}) {
  const t = useTranslations("reports.progress");

  if (!report.isActive) return null;

  const total = report.sectionsTotal ?? 0;
  const done = report.sectionsCompleted;
  const queued = report.status === "pending";

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        <span aria-live="polite">
          {queued
            ? t("queued")
            : total > 0
              ? t("writingSection", { current: Math.min(done + 1, total), total })
              : t("preparing")}
        </span>
      </div>

      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={report.progressPercent}
        aria-label={t("label")}
        className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className={cn(
            "h-full rounded-full bg-info transition-[width] duration-500 ease-out",
            // A queued run gets a visible sliver rather than nothing: a bar with
            // no fill at all is indistinguishable from a bar that failed to
            // render.
            queued && "w-2 animate-pulse",
          )}
          style={queued ? undefined : { width: `${report.progressPercent}%` }}
        />
      </div>
    </div>
  );
}
