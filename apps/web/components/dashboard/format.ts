"use client";

import * as React from "react";

import { useNumberFormat } from "@/hooks/use-number-format";
import type { DashboardMetric, MetricUnit } from "@/types/dashboard";

/**
 * Formatting for dashboard figures.
 *
 * Small on purpose, and separate from `hooks/use-number-format.ts` because these
 * are about a *unit the API declares* rather than about numbers in general: a
 * metric arrives carrying a {@link MetricUnit}, and this is the one place that
 * decides what `bytes` and `milliseconds` look like once rendered.
 *
 * **A hook rather than four module functions, and that is the localization
 * change.** These used to call `Number.prototype.toLocaleString` with no
 * argument, which formats in *the browser's* locale — so a French lawyer who had
 * switched the platform to French still read `1,024` if their operating system
 * was English, and an Arabic reader got whichever digits Chrome felt like.
 * `code-standards.md` states the rule as *"never format a date or a number by
 * hand"*: a module-level formatter cannot read a setting, so the formatter has to
 * come from the provider, and that makes this a hook.
 *
 * **`null` is never rendered as zero.** A metric whose value is `null` is
 * *undefined* — an average over no observations, a rate with no denominator — and
 * showing `0` for it would be exactly the fabricated statistic
 * `19-dashboard-analytics.md`'s "Analytics Data Integrity" section rules out. It
 * renders as an em dash, which is the same thing the report and OCR panels
 * already do for an unmeasured figure.
 *
 * **Unit symbols are not translated**, for the reason `use-number-format.ts`
 * records about `MB`: `s`, `ms`, and `d` are symbols rather than words, identical
 * in the three languages the platform serves, and translating them would make a
 * latency unreadable to the colleague reading the same screen in another one.
 */

/** The placeholder for a figure that has no value. */
export const NO_VALUE = "—";

export interface MetricFormatters {
  /** Render one metric's value according to the unit the API declared. */
  formatMetricValue: (value: number | null, unit?: MetricUnit) => string;
  /** Render a whole metric. */
  formatMetric: (metric: DashboardMetric) => string;
  /** Render a byte count at a human scale. */
  formatBytes: (value: number | null | undefined) => string;
  /** Render a plain count. */
  formatNumber: (value: number | null | undefined) => string;
}

export function useMetricFormat(): MetricFormatters {
  const { formatNumber, formatBytes, formatPercent } = useNumberFormat();

  return React.useMemo(() => {
    const formatMetricValue = (
      value: number | null,
      unit: MetricUnit = "count",
    ): string => {
      if (value === null || !Number.isFinite(value)) return NO_VALUE;

      switch (unit) {
        case "bytes":
          return formatBytes(value, NO_VALUE);
        case "percent":
          return formatPercent(value, { decimals: 1, fallback: NO_VALUE });
        case "days":
          return `${formatNumber(value, NO_VALUE)} d`;
        case "milliseconds":
          return value >= 1000
            ? `${formatNumber(Math.round(value / 100) / 10, NO_VALUE)} s`
            : `${formatNumber(Math.round(value), NO_VALUE)} ms`;
        case "count":
          return formatNumber(value, NO_VALUE);
      }
    };

    return {
      formatMetricValue,
      formatMetric: (metric: DashboardMetric) =>
        formatMetricValue(metric.value, metric.unit),
      formatBytes: (value: number | null | undefined) => formatBytes(value, NO_VALUE),
      formatNumber: (value: number | null | undefined) => formatNumber(value, NO_VALUE),
    };
  }, [formatBytes, formatNumber, formatPercent]);
}

/**
 * One bucket's share of a total, as a percentage.
 *
 * `0` when the total is zero rather than `NaN`: a breakdown of nothing has no
 * shares, and a bar of width `NaN%` disappears silently instead of rendering
 * empty. Not localized and deliberately not a hook — this is a *width*, consumed
 * by CSS, and never read by a person.
 */
export function share(count: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((count / total) * 1000) / 10;
}
