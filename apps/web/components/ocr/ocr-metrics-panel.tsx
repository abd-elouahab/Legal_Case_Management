"use client";

import { Activity, TriangleAlert } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { OcrStatusBadge } from "@/components/ocr/ocr-status-badge";
import { useOcrMetrics } from "@/hooks/use-ocr";
import { ocrFailureLabel } from "@/types/ocr";

/**
 * Platform-wide text-extraction health.
 *
 * The spec's "Monitoring" section: success rate, failure rate, and average
 * processing time, plus the counts behind them and a breakdown of failures by
 * cause. Rendered only for a caller holding `ocr:monitor` — the caller decides,
 * because the panel has no useful "you cannot see this" state to show.
 *
 * It reports **counts and timings only**: no document, no case, no filename, and
 * no extracted text. That is a property of the endpoint rather than of this
 * component, and it is what lets an operational view be shown without it becoming
 * a second, unscoped way to read the case file.
 *
 * The banner when the engine is unavailable is the reason the endpoint reports
 * it at all: a missing Tesseract install and a stack of unreadable scans produce
 * the same failure rate and need entirely different responses.
 */

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-lg font-medium tabular-nums text-foreground">{value}</span>
    </div>
  );
}

export function OcrMetricsPanel() {
  const { data, isLoading, isError } = useOcrMetrics();

  if (isError) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Activity className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          Text extraction
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
                Text extraction is disabled on this deployment. Existing results stay
                readable; no new work is scheduled.
              </p>
            ) : !data.engineAvailable ? (
              <p
                role="alert"
                className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
              >
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  The {data.engine} engine is not reachable, so new extractions will fail.
                  Uploaded documents are unaffected and every run can be retried once it is
                  installed.
                </span>
              </p>
            ) : null}

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Success rate" value={`${data.successRate}%`} />
              <Stat label="Failure rate" value={`${data.failureRate}%`} />
              <Stat
                label="Average time"
                value={
                  data.averageDurationSeconds !== null
                    ? `${data.averageDurationSeconds}s`
                    : "—"
                }
              />
              <Stat label="Runs" value={data.totalRuns.toLocaleString()} />
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <OcrStatusBadge status="pending" />
              <span className="text-sm tabular-nums text-muted-foreground">{data.pending}</span>
              <OcrStatusBadge status="processing" />
              <span className="text-sm tabular-nums text-muted-foreground">
                {data.processing}
              </span>
              <OcrStatusBadge status="completed" />
              <span className="text-sm tabular-nums text-muted-foreground">
                {data.completed}
              </span>
              <OcrStatusBadge status="failed" />
              <span className="text-sm tabular-nums text-muted-foreground">{data.failed}</span>
            </div>

            {Object.keys(data.failuresByCode).length > 0 ? (
              <dl className="flex flex-col gap-1">
                <dt className="text-xs text-muted-foreground">Failures by cause</dt>
                {Object.entries(data.failuresByCode)
                  .sort(([, a], [, b]) => b - a)
                  .map(([code, count]) => (
                    <dd key={code} className="flex justify-between gap-4 text-xs">
                      <span className="text-foreground">{ocrFailureLabel(code)}</span>
                      <span className="tabular-nums text-muted-foreground">{count}</span>
                    </dd>
                  ))}
              </dl>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
