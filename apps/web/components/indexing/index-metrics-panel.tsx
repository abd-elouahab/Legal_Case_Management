"use client";

import { Layers, TriangleAlert } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { IndexStatusBadge } from "@/components/indexing/index-status-badge";
import { useIndexMetrics } from "@/hooks/use-indexing";
import { indexFailureLabel } from "@/types/indexing";

/**
 * Platform-wide document-indexing health.
 *
 * The spec's "Monitoring" section: **indexed documents, indexed chunks, average
 * indexing duration, and failures**, plus the rates, the counts behind them, and
 * a breakdown of failures by cause. Rendered only for a caller holding
 * `indexing:monitor` — the caller decides, because the panel has no useful "you
 * cannot see this" state to show.
 *
 * It reports **counts, timings, and configuration only**: no document, no case,
 * no filename, and no indexed passage. That is a property of the endpoint rather
 * than of this component, and it is what lets an operational view be shown
 * without it becoming a second, unscoped way to read the case file.
 *
 * The two banners are the reason the endpoint reports availability at all: a
 * platform indexing nothing because no embedding model is installed, one indexing
 * nothing because Qdrant is down, and one with nothing to index all show the same
 * zeros — and need entirely different responses.
 */

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-lg font-medium tabular-nums text-foreground">{value}</span>
    </div>
  );
}

export function IndexMetricsPanel() {
  const { data, isLoading, isError } = useIndexMetrics();

  if (isError) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Layers className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          Search indexing
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
                Document indexing is disabled on this deployment. Existing indexes stay
                readable; no new work is scheduled.
              </p>
            ) : !data.embeddingAvailable ? (
              <p
                role="alert"
                className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
              >
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  The {data.embeddingModel} embedding model cannot be loaded, so new
                  indexing will fail. Documents and their extracted text are unaffected,
                  and every run can be rebuilt once it is available.
                </span>
              </p>
            ) : !data.vectorStoreAvailable ? (
              <p
                role="alert"
                className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
              >
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  The search index is not reachable, so new indexing will fail. Documents
                  and their extracted text are unaffected.
                </span>
              </p>
            ) : null}

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Documents indexed" value={data.indexed.toLocaleString()} />
              <Stat label="Passages indexed" value={data.totalChunks.toLocaleString()} />
              <Stat
                label="Average time"
                value={
                  data.averageDurationSeconds !== null
                    ? `${data.averageDurationSeconds}s`
                    : "—"
                }
              />
              <Stat label="Failures" value={data.failed.toLocaleString()} />
            </div>

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Success rate" value={`${data.successRate}%`} />
              <Stat label="Failure rate" value={`${data.failureRate}%`} />
              <Stat
                label="Passages per document"
                value={
                  data.averageChunksPerDocument !== null
                    ? data.averageChunksPerDocument.toLocaleString()
                    : "—"
                }
              />
              {/* Reported by the vector database itself rather than summed from
                  the rows: a divergence between the two is exactly what an
                  operator opens this panel to find. */}
              <Stat
                label="Vectors stored"
                value={
                  data.storedVectors !== null ? data.storedVectors.toLocaleString() : "—"
                }
              />
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <IndexStatusBadge status="pending" />
              <span className="text-sm tabular-nums text-muted-foreground">
                {data.pending}
              </span>
              <IndexStatusBadge status="indexing" />
              <span className="text-sm tabular-nums text-muted-foreground">
                {data.indexing}
              </span>
              <IndexStatusBadge status="indexed" />
              <span className="text-sm tabular-nums text-muted-foreground">
                {data.indexed}
              </span>
              <IndexStatusBadge status="failed" />
              <span className="text-sm tabular-nums text-muted-foreground">
                {data.failed}
              </span>
            </div>

            {Object.keys(data.failuresByCode).length > 0 ? (
              <dl className="flex flex-col gap-1">
                <dt className="text-xs text-muted-foreground">Failures by cause</dt>
                {Object.entries(data.failuresByCode)
                  .sort(([, a], [, b]) => b - a)
                  .map(([code, count]) => (
                    <dd key={code} className="flex justify-between gap-4 text-xs">
                      <span className="text-foreground">{indexFailureLabel(code)}</span>
                      <span className="tabular-nums text-muted-foreground">{count}</span>
                    </dd>
                  ))}
              </dl>
            ) : null}

            <p className="text-xs text-muted-foreground">
              {data.embeddingModel} · {data.embeddingDimensions} dimensions ·{" "}
              {data.chunker} · {data.chunkSize}/{data.chunkOverlap} chars ·{" "}
              {data.vectorCollection}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
