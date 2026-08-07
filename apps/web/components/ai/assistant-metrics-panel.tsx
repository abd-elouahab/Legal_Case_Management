"use client";

import { Bot } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAssistantMetrics } from "@/hooks/use-assistant";
import { usePermissions } from "@/hooks/use-permissions";
import { PERMISSION } from "@/types/authorization";

/**
 * Platform-wide AI assistant health.
 *
 * The spec's "Monitoring" section: **active conversations, average response time,
 * average conversation length, successful requests, failed requests, and user
 * feedback statistics**, plus the rates and a breakdown of failures by cause.
 * Rendered only for a caller holding `ai:monitor` — the caller decides, because
 * the panel has no useful "you cannot see this" state.
 *
 * It reports **counts, rates, and configuration only**: no conversation, no
 * title, no question, no answer, and no citation. That is a property of the
 * endpoint rather than of this component, and it is what lets an operational view
 * be shown without becoming a second, unscoped way to read somebody's research.
 *
 * **The two halves have different windows, and the footnote says so.** The
 * conversation and feedback figures are read from the database and cover
 * everything; the request counters live in the API process, so they reset on
 * restart and each instance counts only its own traffic. Saying that is what
 * keeps the numbers from quietly meaning less than they appear to.
 *
 * The three configuration banners exist because the counters cannot tell those
 * situations apart: an assistant that is switched off, one whose suggestions are
 * unavailable, and one nobody has used yet all show the same zeros.
 */

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-lg font-medium tabular-nums text-foreground">{value}</span>
    </div>
  );
}

export function AssistantMetricsPanel() {
  const { can, isLoading: sessionLoading } = usePermissions();
  const allowed = !sessionLoading && can(PERMISSION.aiMonitor);
  const { data, isLoading, isError } = useAssistantMetrics({ enabled: allowed });

  // Gated here rather than by the page, so every surface that wants the panel
  // gets the check with it — and so the request is never sent by a caller the
  // API would refuse, which would put a 403 in the console on every page load.
  if (!allowed || isError) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Bot className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          AI assistant
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
                The assistant is disabled on this deployment. Existing conversations stay
                readable; only new ones and new messages are refused.
              </p>
            ) : null}

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Active conversations" value={String(data.activeConversations)} />
              <Stat
                label="Average length"
                value={
                  data.averageConversationLength === null
                    ? "—"
                    : `${data.averageConversationLength} messages`
                }
              />
              <Stat
                label="Average response"
                value={
                  data.averageResponseSeconds === null
                    ? "—"
                    : `${data.averageResponseSeconds.toFixed(1)}s`
                }
              />
              <Stat label="Grounded" value={`${data.groundingRate}%`} />
            </div>

            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat label="Messages answered" value={String(data.successfulRequests)} />
              <Stat label="Failed" value={String(data.failedRequests)} />
              <Stat label="Streamed" value={String(data.streamedRequests)} />
              <Stat
                label="Rated helpful"
                value={
                  data.helpfulRate === null
                    ? "—"
                    : `${data.helpfulRate}% of ${data.totalFeedback}`
                }
              />
            </div>

            {Object.keys(data.failuresByCode).length > 0 ? (
              <div className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Failures by cause</span>
                <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
                  {Object.entries(data.failuresByCode).map(([code, count]) => (
                    <li key={code} className="text-secondary-foreground">
                      {code.replace(/_/g, " ")}:{" "}
                      <span className="tabular-nums">{count}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <p className="text-xs text-muted-foreground">
              Conversations and ratings cover the whole platform. Request counts and
              timings are from this API process since{" "}
              {new Date(data.since).toLocaleString()} and reset when it restarts.
              {data.suggestionsEnabled ? null : " Follow-up suggestions are unavailable."}
              {data.streamingEnabled ? null : " Streaming is turned off."}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
