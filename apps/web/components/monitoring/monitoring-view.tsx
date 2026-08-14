"use client";

import { RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/shared/error-state";
import { HealthBadge } from "@/components/monitoring/state-badge";
import {
  AlertsPanel,
  ErrorsPanel,
  HealthPanel,
  JobsPanel,
  PerformancePanel,
  SecurityPanel,
  TracesPanel,
} from "@/components/monitoring/panels";
import { humanize } from "@/components/monitoring/format";
import { useDateFormat } from "@/hooks/use-date-format";
import { useMonitoringOverview } from "@/hooks/use-monitoring";

/**
 * The monitoring page's client view.
 *
 * **One request for the whole page**, because the API's aggregate loops over the
 * very loaders its narrow endpoints call: a page assembled from eight parallel
 * reads could show a health state from one moment beside a queue depth from
 * another, which during an incident is the difference between a diagnosis and a
 * wild goose chase.
 *
 * **A partial page is a first-class outcome, not an error.** Each section on the
 * server is assembled inside its own `try`, and one that could not be read arrives
 * named in `unavailable` with the response still a 200. So this view renders what
 * it has and says plainly what is missing — a monitoring page that went blank
 * because one of its eight parts was unavailable would be a page nobody could rely
 * on, and the moment it is most needed is the moment some of what it reads from is
 * broken.
 *
 * It refreshes on a timer rather than through the event channel, and
 * `hooks/use-monitoring.ts` records why: nothing publishes a domain event when a
 * queue backs up, and the channel is itself one of the things this page watches.
 */
export function MonitoringView() {
  const t = useTranslations("monitoring");
  const { data, isLoading, isError, refetch, isFetching } = useMonitoringOverview();
  const { formatDateTime } = useDateFormat();

  if (isError) {
    return <ErrorState description={t("loadFailed")} onRetry={() => void refetch()} />;
  }

  if (isLoading || !data) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true">
        <Skeleton className="h-20" />
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card p-4">
        <div className="flex flex-col gap-1">
          <span className="flex items-center gap-2">
            <HealthBadge state={data.state} />
            <span className="text-sm font-medium text-foreground">
              {data.health.system.projectName}
            </span>
          </span>
          <span className="text-xs text-muted-foreground">
            {t("generatedAt", { at: formatDateTime(data.generatedAt) })}
          </span>
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => void refetch()}
          disabled={isFetching}
        >
          <RefreshCw
            className={isFetching ? "h-4 w-4 animate-spin" : "h-4 w-4"}
            aria-hidden="true"
          />
          {t("refresh")}
        </Button>
      </header>

      {data.unavailable.length > 0 ? (
        <p className="rounded-md border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
          {t("partial", {
            sections: data.unavailable.map((section) => humanize(section)).join(", "),
          })}
        </p>
      ) : null}

      <AlertsPanel report={data.alerts} />

      <div className="grid gap-6 lg:grid-cols-2">
        <HealthPanel report={data.health} />
        <PerformancePanel report={data.performance} />
        <JobsPanel report={data.jobs} />
        <SecurityPanel report={data.security} />
        <ErrorsPanel report={data.errors} />
        <TracesPanel report={data.traces} />
      </div>
    </div>
  );
}
