"use client";

import { FileBarChart, TriangleAlert } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useReportMetrics } from "@/hooks/use-reports";
import { reportFailureLabel, reportTypeLabel } from "@/types/report";

/**
 * Platform-wide AI report health.
 *
 * The spec's "Monitoring" section: **generated reports, average generation time,
 * export count, failed generations, average report size, and token usage**, plus
 * the rates and the breakdowns by type and by cause. Rendered only for a caller
 * holding `reports:monitor` — the caller decides, because the panel has no useful
 * "you cannot see this" state to show.
 *
 * It reports **counts, durations, sizes, and configuration only**: no report, no
 * title, no section, no citation, and not whose it was. That is a property of the
 * endpoint rather than of this component, and it is what lets an operational view
 * be shown without becoming a second, unscoped way to read other people's work.
 *
 * **No "since" caveat, unlike the search, RAG, and assistant panels.** Every
 * figure here is a SQL aggregate over persisted rows, because a report *is* a
 * persisted run — so the numbers are exact, they survive a restart, and every API
 * instance reports the same ones.
 *
 * The two banners are the reason the endpoint reports availability at all: a
 * platform generating nothing because no credential is configured, one generating
 * nothing because its prompts are missing, and one nobody has asked yet all show
 * the same zeros — and need entirely different responses.
 */

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-lg font-medium tabular-nums text-foreground">{value}</span>
    </div>
  );
}

export function ReportMetricsPanel() {
  const { data, isLoading, isError } = useReportMetrics();

  if (isError) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <FileBarChart className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          AI report generation
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
                Report generation is disabled on this deployment. Existing reports stay
                readable and exportable; no new ones are queued.
              </p>
            ) : !data.llmAvailable ? (
              <p
                role="alert"
                className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
              >
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  No AI provider is reachable, so new reports will fail. Cases, documents,
                  and existing reports are unaffected, and every run can be generated
                  again once it is available.
                </span>
              </p>
            ) : !data.promptAvailable ? (
              <p
                role="alert"
                className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
              >
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  The pipeline&apos;s answer template cannot be loaded, so every section
                  will fail — a report section is a pipeline run. Cases and documents are
                  unaffected.
                </span>
              </p>
            ) : null}

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Reports generated" value={data.completed.toLocaleString()} />
              <Stat
                label="Average time"
                value={
                  data.averageDurationSeconds !== null
                    ? `${data.averageDurationSeconds}s`
                    : "—"
                }
              />
              <Stat label="Exports" value={data.totalExports.toLocaleString()} />
              <Stat label="Failures" value={data.failed.toLocaleString()} />
            </div>

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Success rate" value={`${data.successRate}%`} />
              {/* The number to watch, and not an AI metric: a falling rate means
                  the corpus no longer covers what reports ask of it. */}
              <Stat label="Sections grounded" value={`${data.groundingRate}%`} />
              <Stat
                label="Average size"
                value={
                  data.averageCharacters !== null
                    ? `${Math.round(data.averageCharacters).toLocaleString()} chars`
                    : "—"
                }
              />
              {/* Absent rather than zero when no provider has reported usage:
                  zero would read as "this platform's reports are free". */}
              <Stat
                label="Tokens used"
                value={
                  data.totalPromptTokens !== null || data.totalCompletionTokens !== null
                    ? (
                        (data.totalPromptTokens ?? 0) + (data.totalCompletionTokens ?? 0)
                      ).toLocaleString()
                    : "—"
                }
              />
            </div>

            {Object.keys(data.reportsByType).length > 0 ? (
              <div className="flex flex-col gap-2">
                <span className="text-xs text-muted-foreground">Reports by type</span>
                <ul className="flex flex-wrap gap-x-4 gap-y-1">
                  {Object.entries(data.reportsByType).map(([type, count]) => (
                    <li key={type} className="text-sm text-secondary-foreground">
                      {reportTypeLabel(type)}:{" "}
                      <span className="tabular-nums">{count.toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {Object.keys(data.failuresByCode).length > 0 ? (
              <div className="flex flex-col gap-2">
                {/* A failure rate says something is wrong; this says what — an
                    unreachable vector database, a missing credential, and a case
                    with no indexed documents read identically otherwise. */}
                <span className="text-xs text-muted-foreground">Failures by cause</span>
                <ul className="flex flex-wrap gap-x-4 gap-y-1">
                  {Object.entries(data.failuresByCode).map(([code, count]) => (
                    <li key={code} className="text-sm text-secondary-foreground">
                      {reportFailureLabel(code)}:{" "}
                      <span className="tabular-nums">{count.toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <p className="text-xs text-muted-foreground">
              Export formats available here:{" "}
              {data.availableFormats.length > 0
                ? data.availableFormats.join(", ")
                : "none"}
              . Template set v{data.templateVersion}.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
