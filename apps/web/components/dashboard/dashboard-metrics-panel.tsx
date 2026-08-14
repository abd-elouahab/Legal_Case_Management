"use client";

import { useTranslations } from "next-intl";
import { Gauge } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useMetricFormat } from "@/components/dashboard/format";
import { useDashboardMetrics } from "@/hooks/use-dashboard";
import { useDateFormat } from "@/hooks/use-date-format";
import { useNumberFormat } from "@/hooks/use-number-format";
import { isKnownWidget } from "@/types/dashboard";

/**
 * Platform-wide dashboard health.
 *
 * The spec's "Monitoring" section: **dashboard load time, widget load time,
 * refresh frequency, failed widget requests, and active dashboard users**, plus
 * the per-widget breakdown that turns "the dashboard is slow" into "`storage_usage`
 * is slow" — the only form of that sentence anybody can act on. Rendered only for
 * a caller holding `dashboard:monitor`; the caller decides, because the panel has
 * no useful "you cannot see this" state to show.
 *
 * It reports **counts, durations, and widget keys only**: no case, no document, no
 * figure from anybody's dashboard, and not whose it was.
 *
 * **Every number carries `since`, without exception** — unlike the notification,
 * email, and WhatsApp panels, which caveat some of their figures and not others.
 * The dashboard persists nothing, so there is no exact SQL half to prefer and no
 * split to explain: these are this process's counters, and they reset when it
 * restarts.
 *
 * `activeUsers` is a distinct-person count derived from salted digests the server
 * cannot reverse, which is why it can say how many people opened a dashboard and
 * never who.
 *
 * **A widget key with no label still renders**, through the provider's message
 * fallback rather than through a branch here — the same property the widget cards
 * themselves have, so a metric for a widget this build has never heard of is a
 * readable row instead of a raw identifier.
 */

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-lg font-medium tabular-nums text-foreground">{value}</span>
    </div>
  );
}

export function DashboardMetricsPanel() {
  const { data, isLoading, isError } = useDashboardMetrics();
  const t = useTranslations("dashboard.health");
  const tWidgets = useTranslations("dashboard.widgets");
  const tFailures = useTranslations("dashboard.errors.widget");
  const { formatMetricValue } = useMetricFormat();
  const { formatNumber, formatPercent } = useNumberFormat();
  const { formatDateTime } = useDateFormat();

  if (isError) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Gauge className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          {t("title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {isLoading || !data ? (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4" aria-busy="true">
            {Array.from({ length: 4 }, (_, index) => (
              <Skeleton key={index} className="h-12" />
            ))}
          </div>
        ) : (
          <>
            {!data.enabled ? (
              <p className="rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground">
                {t("disabled")}
              </p>
            ) : null}

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label={t("loads")} value={formatNumber(data.loads)} />
              <Stat
                label={t("averageLoad")}
                value={formatMetricValue(data.averageLoadMs, "milliseconds")}
              />
              <Stat
                label={t("averageWidget")}
                value={formatMetricValue(data.averageWidgetMs, "milliseconds")}
              />
              <Stat
                label={t("activeUsers")}
                value={`${formatNumber(data.activeUsers)}${data.activeUsersCapped ? "+" : ""}`}
              />
            </div>

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label={t("refreshes")} value={formatNumber(data.refreshes)} />
              <Stat label={t("widgetsLoaded")} value={formatNumber(data.widgetsLoaded)} />
              <Stat label={t("widgetsFailed")} value={formatNumber(data.widgetsFailed)} />
              <Stat
                label={t("successRate")}
                value={formatPercent(data.widgetSuccessRate, { decimals: 0 })}
              />
            </div>

            {Object.keys(data.averageMsByWidget).length > 0 ? (
              <div className="flex flex-col gap-2">
                <span className="text-xs text-muted-foreground">
                  {t("slowestWidgets")}
                </span>
                <ul className="flex flex-col gap-1 text-sm">
                  {Object.entries(data.averageMsByWidget)
                    .sort(([, a], [, b]) => b - a)
                    .slice(0, 5)
                    .map(([key, ms]) => (
                      <li key={key} className="flex items-center justify-between gap-2">
                        <span className="text-muted-foreground">
                          {isKnownWidget(key) ? tWidgets(`${key}.title`) : key}
                        </span>
                        <span className="tabular-nums text-foreground">
                          {formatMetricValue(ms, "milliseconds")}
                        </span>
                      </li>
                    ))}
                </ul>
              </div>
            ) : null}

            {Object.keys(data.failuresByReason).length > 0 ? (
              <div className="flex flex-col gap-2">
                <span className="text-xs text-muted-foreground">
                  {t("failuresByCause")}
                </span>
                <ul className="flex flex-col gap-1 text-sm">
                  {Object.entries(data.failuresByReason).map(([reason, count]) => (
                    <li key={reason} className="flex items-center justify-between gap-2">
                      <span className="text-muted-foreground">
                        {/* `budget_exhausted` is load shedding rather than a
                            fault, and it is worth an operator being able to tell
                            the two apart at a glance. Both read from the same
                            keys the widget cards explain themselves with, in
                            their short form — a reason in a list is a label, not
                            a sentence. */}
                        {reason === "budget_exhausted"
                          ? tFailures("budgetExhaustedShort")
                          : reason === "query_failed"
                            ? tFailures("queryFailedShort")
                            : reason}
                      </span>
                      <span className="tabular-nums text-foreground">
                        {formatNumber(count)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <p className="text-xs text-muted-foreground">
              {t("since", { at: formatDateTime(data.since) })}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
