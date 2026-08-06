"use client";

import { Search, TriangleAlert } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useSearchMetrics } from "@/hooks/use-search";
import { searchFailureLabel } from "@/types/search";

/**
 * Platform-wide semantic-search health.
 *
 * The spec's "Monitoring" section: **search count, average latency, average
 * relevance score, and failed searches**, plus the rates and a breakdown of
 * failures by cause. Rendered only for a caller holding `search:monitor` — the
 * caller decides, because the panel has no useful "you cannot see this" state.
 *
 * It reports **counts, timings, and configuration only**: no query, no document,
 * no case, and no passage. That is a property of the endpoint rather than of this
 * component, and it is what lets an operational view be shown without it becoming
 * a second, unscoped way to read the case file.
 *
 * The two banners are the reason the endpoint reports availability at all: a
 * platform answering no searches because no embedding model is installed, one
 * answering none because Qdrant is down, and one nobody has searched yet all show
 * the same zeros — and need entirely different responses.
 *
 * The "since" line is not a footnote either. These counters live in the API
 * process rather than in a table, so they reset on restart and each instance
 * counts only its own traffic; saying so is what keeps the figures from quietly
 * meaning less than they appear to.
 */

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-lg font-medium tabular-nums text-foreground">{value}</span>
    </div>
  );
}

export function SearchMetricsPanel() {
  const { data, isLoading, isError } = useSearchMetrics();

  if (isError) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Search className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          Semantic search
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
                Semantic search is disabled on this deployment. Indexed documents are
                unaffected; only searching them is refused.
              </p>
            ) : !data.embeddingAvailable ? (
              <p
                role="alert"
                className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
              >
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  The {data.embeddingModel} embedding model cannot be loaded, so every
                  search will fail. Documents and their indexes are unaffected.
                </span>
              </p>
            ) : !data.vectorStoreAvailable ? (
              <p
                role="alert"
                className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
              >
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  The search index is not reachable, so every search will fail. Documents
                  and their extracted text are unaffected.
                </span>
              </p>
            ) : null}

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Searches" value={data.totalSearches.toLocaleString()} />
              <Stat
                label="Average time"
                value={
                  data.averageLatencyMs !== null
                    ? `${Math.round(data.averageLatencyMs)} ms`
                    : "—"
                }
              />
              <Stat
                label="Average relevance"
                value={
                  data.averageScore !== null
                    ? `${Math.round(data.averageScore * 100)}%`
                    : "—"
                }
              />
              <Stat label="Failures" value={data.failedSearches.toLocaleString()} />
            </div>

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Success rate" value={`${data.successRate}%`} />
              <Stat label="Failure rate" value={`${data.failureRate}%`} />
              <Stat
                label="Results per search"
                value={
                  data.averageResults !== null ? data.averageResults.toLocaleString() : "—"
                }
              />
              <Stat label="Passages returned" value={data.totalResults.toLocaleString()} />
            </div>

            {Object.keys(data.failuresByCode).length > 0 ? (
              <dl className="flex flex-col gap-1">
                <dt className="text-xs text-muted-foreground">Failures by cause</dt>
                {Object.entries(data.failuresByCode)
                  .sort(([, a], [, b]) => b - a)
                  .map(([code, count]) => (
                    <dd key={code} className="flex justify-between gap-4 text-xs">
                      <span className="text-foreground">{searchFailureLabel(code)}</span>
                      <span className="tabular-nums text-muted-foreground">{count}</span>
                    </dd>
                  ))}
              </dl>
            ) : null}

            <p className="text-xs text-muted-foreground">
              {data.embeddingModel} · {data.embeddingDimensions} dimensions ·{" "}
              {data.ranker} ranking · {data.vectorCollection} · up to {data.maxLimit}{" "}
              results
            </p>
            <p className="text-xs text-muted-foreground">
              Counted on this API instance since{" "}
              <time dateTime={data.since}>{new Date(data.since).toLocaleString()}</time>.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
