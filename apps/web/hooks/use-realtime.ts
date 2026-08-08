"use client";

import * as React from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { RealtimeContext } from "@/components/realtime/realtime-provider";
import { fetchPresence, fetchRealtimeMetrics } from "@/lib/api/realtime";
import type { RealtimeClient } from "@/lib/realtime/client";
import type {
  ConnectionStatus,
  EventScope,
  EventTopic,
  PresenceEntry,
  RealtimeEvent,
  RealtimeMetrics,
} from "@/types/realtime";
import { topicFor } from "@/types/realtime";

/**
 * Client state for the real-time channel.
 *
 * The hooks a component actually uses. They are deliberately small and
 * declarative — `useRealtimeTopic(topicFor("case", id))` in an effect-free line
 * — because the interesting parts (reconnect, backoff, re-subscription,
 * duplicate suppression, cache invalidation) are the client's and the provider's,
 * and a component that had to think about any of them would be a component with
 * business logic in it.
 *
 * **A subscription is a hint, never a requirement.** Every screen on this
 * platform works with the socket closed: the lists poll, the pipelines poll, and
 * the reports poll. Subscribing is what lets those polls be slower and the
 * screen still feel immediate — so a refused topic, a disabled deployment, and a
 * failed connection all degrade to exactly the behaviour that existed before this
 * feature.
 */

/** Query keys for the module's HTTP reads. */
export const realtimeKeys = {
  all: ["realtime"] as const,
  status: () => [...realtimeKeys.all, "status"] as const,
  presence: () => [...realtimeKeys.all, "presence"] as const,
  metrics: () => [...realtimeKeys.all, "metrics"] as const,
};

/** How often the administrative views re-read. */
const MONITOR_POLL_INTERVAL_MS = 10_000;

/**
 * The connection, for a component that needs to act on it.
 *
 * Returns `null` outside a `RealtimeProvider`, which is a real situation rather
 * than a misuse: the authentication pages render outside the protected shell,
 * and a hook that threw there would make the provider's placement a trap.
 */
export function useRealtimeClient(): RealtimeClient | null {
  return React.useContext(RealtimeContext)?.client ?? null;
}

/**
 * The connection status, re-rendering on every change.
 *
 * `idle` outside a provider — the honest answer for a tree with no channel in
 * it, and the one that makes the indicator render nothing rather than "offline".
 */
export function useConnectionStatus(): ConnectionStatus {
  const context = React.useContext(RealtimeContext);
  return context?.status ?? "idle";
}

/** Whether live updates are available at all on this deployment. */
export function useRealtimeEnabled(): boolean {
  return React.useContext(RealtimeContext)?.enabled ?? false;
}

/**
 * Follow one or more topics for as long as the component is mounted.
 *
 * The topics are joined into a string dependency rather than passed as an array,
 * so a caller writing the common `useRealtimeTopics([topicFor("case", id)])`
 * inline does not resubscribe on every render — the array is a new reference each
 * time, and the identity of its *contents* is what actually matters.
 *
 * `null` and `undefined` entries are skipped, so a component whose identifier
 * arrives asynchronously can call this unconditionally instead of branching
 * around a hook.
 */
export function useRealtimeTopics(
  topics: readonly (EventTopic | null | undefined)[],
): void {
  const client = useRealtimeClient();
  const resolved = topics.filter((topic): topic is EventTopic => Boolean(topic));
  const key = resolved.slice().sort().join(",");

  React.useEffect(() => {
    if (!client || !key) return;
    return client.subscribe(key.split(",") as EventTopic[]);
  }, [client, key]);
}

/** Follow one topic. The common case, spelled without an array. */
export function useRealtimeTopic(topic: EventTopic | null | undefined): void {
  useRealtimeTopics([topic]);
}

/** Follow one resource by scope and identifier. */
export function useRealtimeResource(
  scope: EventScope,
  id: string | null | undefined,
): void {
  useRealtimeTopic(id ? topicFor(scope, id) : null);
}

/**
 * Run a callback on every event, without re-subscribing when it changes.
 *
 * For the rare component that needs to *react* rather than merely refetch — a
 * toast when a report finishes, say. The callback is held in a ref so an inline
 * arrow function does not tear the listener down and rebuild it on every render,
 * which is the mistake this hook exists to make impossible.
 *
 * Most components should not use this. Invalidating the right cache is already
 * done centrally (`lib/realtime/sync.ts`), and a second place that reacts to the
 * same event is a second place to keep in step.
 */
export function useRealtimeEvents(listener: (event: RealtimeEvent) => void): void {
  const client = useRealtimeClient();
  const held = React.useRef(listener);

  React.useEffect(() => {
    held.current = listener;
  }, [listener]);

  React.useEffect(() => {
    if (!client) return;
    return client.onEvent((event) => held.current(event));
  }, [client]);
}

// --------------------------------------------------------------------------- //
// Monitoring
// --------------------------------------------------------------------------- //

/**
 * Platform-wide channel health. Requires `realtime:monitor`.
 *
 * Polled rather than pushed, and the irony is deliberate: a metrics panel that
 * updated from the channel it measures would report zero events while it was the
 * only thing generating them, and would stop updating precisely when the channel
 * broke — which is the moment somebody is looking at it.
 */
export function useRealtimeMetrics(
  options: { enabled?: boolean } = {},
): UseQueryResult<RealtimeMetrics, unknown> {
  return useQuery({
    queryKey: realtimeKeys.metrics(),
    queryFn: fetchRealtimeMetrics,
    enabled: options.enabled ?? true,
    refetchInterval: MONITOR_POLL_INTERVAL_MS,
  });
}

/** Who is currently connected. Requires `realtime:monitor`. */
export function usePresence(
  options: { enabled?: boolean } = {},
): UseQueryResult<PresenceEntry[], unknown> {
  return useQuery({
    queryKey: realtimeKeys.presence(),
    queryFn: fetchPresence,
    enabled: options.enabled ?? true,
    refetchInterval: MONITOR_POLL_INTERVAL_MS,
  });
}
