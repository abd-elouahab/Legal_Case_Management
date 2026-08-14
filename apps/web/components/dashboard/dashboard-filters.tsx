"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { DASHBOARD_RANGES, type DashboardQuery, type DashboardRange } from "@/types/dashboard";

/**
 * The dashboard's time filter.
 *
 * The spec's four windows — Today, Last 7 Days, Last 30 Days, Custom — and it
 * changes **one thing**: the query the page is read with. Every widget then
 * measures the same interval, because the server resolves the window once per
 * request and hands it to all of them. A filter that each card applied for itself
 * is how two tiles end up describing different fortnights.
 *
 * The custom bounds only appear for the custom range, and the "Apply" button only
 * enables once both are set and ordered — the API refuses an incomplete or
 * inverted range, and being told that by a disabled button is better than being
 * told it by a 422.
 *
 * Rendered as a row of buttons rather than a select: four options that people
 * switch between constantly are worth one click, and the pressed state carries
 * `aria-pressed` so the current window is announced rather than only shaded.
 */

export interface DashboardFiltersProps {
  query: DashboardQuery;
  onChange: (query: DashboardQuery) => void;
  disabled?: boolean;
}

export function DashboardFilters({
  query,
  onChange,
  disabled = false,
}: DashboardFiltersProps) {
  const [start, setStart] = React.useState(query.startDate ?? "");
  const [end, setEnd] = React.useState(query.endDate ?? "");
  const t = useTranslations("dashboard.filters");
  const tRanges = useTranslations("dashboard.ranges");
  const tActions = useTranslations("common.actions");

  const canApply = start !== "" && end !== "" && end >= start;

  const selectRange = (range: DashboardRange) => {
    if (range === "custom") {
      // Switching *to* custom does not fetch: there is nothing to fetch until
      // both dates exist, and sending a half-built range would be a request the
      // API is right to refuse.
      onChange({ ...query, range, startDate: start || null, endDate: end || null });
      return;
    }
    // Leaving custom clears the bounds, so a stale pair left in the form cannot
    // travel with a fixed range and change what "last 7 days" means.
    onChange({ ...query, range, startDate: null, endDate: null });
  };

  return (
    <div className="flex flex-col gap-3">
      <div
        className="flex flex-wrap items-center gap-1"
        role="group"
        aria-label={t("period")}
      >
        {DASHBOARD_RANGES.map((range) => (
          <Button
            key={range}
            type="button"
            size="sm"
            variant={query.range === range ? "secondary" : "ghost"}
            aria-pressed={query.range === range}
            disabled={disabled}
            onClick={() => selectRange(range)}
          >
            {tRanges(range)}
          </Button>
        ))}
      </div>

      {query.range === "custom" ? (
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="dashboard-start">{t("from")}</Label>
            <Input
              id="dashboard-start"
              type="date"
              value={start}
              max={end || undefined}
              disabled={disabled}
              onChange={(event) => setStart(event.target.value)}
              className="w-44"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="dashboard-end">{t("to")}</Label>
            <Input
              id="dashboard-end"
              type="date"
              value={end}
              min={start || undefined}
              disabled={disabled}
              onChange={(event) => setEnd(event.target.value)}
              className="w-44"
            />
          </div>
          <Button
            type="button"
            size="sm"
            disabled={disabled || !canApply}
            onClick={() =>
              onChange({ ...query, range: "custom", startDate: start, endDate: end })
            }
          >
            {tActions("apply")}
          </Button>
          {start !== "" && end !== "" && end < start ? (
            <p
              role="alert"
              className={cn("text-sm text-destructive", disabled && "opacity-60")}
            >
              {t("invalidRange")}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
