"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { fetchMonitoringOverview } from "@/lib/api/monitoring";
import type { MonitoringOverview } from "@/types/monitoring";

/**
 * Server state for the monitoring page.
 *
 * TanStack Query per `architecture.md`: operational state is server state, so it
 * is cached and refetched rather than mirrored into a client store.
 *
 * **It polls, and it deliberately does not subscribe to the event channel.**
 * Every other live surface on this platform refreshes when a domain event says
 * something changed — but monitoring watches the *platform*, not its subject
 * matter, and nothing publishes a domain event when a queue backs up, a
 * dependency goes away, or a worker pool stops. More pointedly: the channel is
 * one of the things this page exists to observe, so a page that learned about
 * problems through it would go blank exactly when it was needed. A short poll is
 * both simpler and more honest.
 *
 * **One request, not eight.** The API's aggregate loops over the same loaders its
 * narrow endpoints call, so a single read cannot show a health state from one
 * moment beside a queue depth from another — which on an operational page is the
 * difference between a diagnosis and a wild goose chase.
 */

/** How often the page re-reads. Short: this is the screen somebody watches. */
export const MONITORING_POLL_INTERVAL_MS = 15_000;

/** Query keys. */
export const monitoringKeys = {
  all: ["monitoring"] as const,
  overview: () => [...monitoringKeys.all, "overview"] as const,
};

/** Read the platform's operational state, refreshed on an interval. */
export function useMonitoringOverview(): UseQueryResult<MonitoringOverview> {
  return useQuery({
    queryKey: monitoringKeys.overview(),
    queryFn: ({ signal }) => fetchMonitoringOverview(signal),
    refetchInterval: MONITORING_POLL_INTERVAL_MS,
    // Kept fresh while the tab is in the background as well: an operator who
    // switches away during an incident and switches back should not read a
    // stale page for a beat before it updates.
    refetchIntervalInBackground: false,
    staleTime: 0,
    // Never retried on a 4xx: an operator without `monitoring:view` gets one
    // clean refusal rather than three, and the page says so.
    retry: false,
  });
}
