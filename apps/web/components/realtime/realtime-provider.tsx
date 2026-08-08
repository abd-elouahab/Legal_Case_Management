"use client";

import * as React from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchRealtimeStatus } from "@/lib/api/realtime";
import { RealtimeClient } from "@/lib/realtime/client";
import { applyRealtimeEvent } from "@/lib/realtime/sync";
import { useSessionStore } from "@/stores/session-store";
import type { ConnectionStatus } from "@/types/realtime";

/**
 * Owns the platform's one WebSocket connection.
 *
 * Sits **inside** `SessionProvider` and inside `QueryClientProvider`, and needs
 * both: it opens the socket only once a session exists (the socket authenticates
 * with the access token that provider obtains), and every event it receives is
 * turned into a cache invalidation on the query client.
 *
 * Three responsibilities, and nothing else:
 *
 * 1. **Lifecycle** — open on sign-in, close on sign-out. A socket that outlived
 *    a sign-out would keep delivering one user's case updates into the next
 *    user's tab, which is the one way this feature could leak.
 * 2. **Cache synchronization** — every event is routed through
 *    `lib/realtime/sync.ts`. That table is the whole of "what does this event
 *    make stale", and this component is the only thing that consults it.
 * 3. **Gap recovery** — when a reconnect reports that events were missed,
 *    *everything* is invalidated rather than guessed at. The client does not
 *    know what it missed; the server deliberately keeps no history to replay
 *    (see `services/events.py`), so the honest response to a gap is a full
 *    refetch, which is authoritative where a replay would only be a hint.
 *
 * **It renders nothing and blocks nothing.** A deployment with the channel
 * disabled, a failed connection, and a browser with WebSockets blocked all leave
 * the application exactly as it was before this feature: every list, pipeline,
 * and report still polls.
 */

interface RealtimeContextValue {
  client: RealtimeClient | null;
  status: ConnectionStatus;
  /** Whether this deployment offers live updates at all. */
  enabled: boolean;
}

export const RealtimeContext = React.createContext<RealtimeContextValue | null>(null);

/** How long the deployment's channel configuration is trusted. */
const STATUS_STALE_TIME_MS = 5 * 60 * 1000;

export function RealtimeProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const sessionStatus = useSessionStore((state) => state.status);
  const userId = useSessionStore((state) => state.user?.id ?? null);

  const [status, setStatus] = React.useState<ConnectionStatus>("idle");

  // One client per mounted provider. A lazy `useState` initializer rather than a
  // ref, for two reasons: the factory runs exactly once per mount (a bare
  // `new RealtimeClient()` in the body would build one on every render and throw
  // all but the first away), and it never runs during server rendering, where
  // `WebSocket` does not exist.
  const [client] = React.useState<RealtimeClient | null>(() =>
    typeof window === "undefined" ? null : new RealtimeClient(),
  );

  // Asked once, and only when signed in: the endpoint requires a session, and a
  // deployment's answer changes when it is redeployed rather than while somebody
  // is using it.
  const { data: channel } = useQuery({
    queryKey: ["realtime", "status"],
    queryFn: fetchRealtimeStatus,
    enabled: sessionStatus === "authenticated",
    staleTime: STATUS_STALE_TIME_MS,
    // A failure here means "we could not ask", not "it is off". One retry, then
    // the provider treats the channel as unavailable and the app polls — which is
    // the same outcome as an honest `enabled: false`, reached more slowly.
    retry: 1,
  });

  const enabled = channel?.enabled ?? false;

  // --- 1. Open on sign-in, close on sign-out ------------------------------- //
  React.useEffect(() => {
    if (!client) return;

    if (sessionStatus !== "authenticated" || !enabled) {
      client.close();
      return;
    }

    client.connect();

    // Closed on unmount **and** whenever the identity changes. `userId` is in the
    // dependency list precisely so that switching accounts in one tab tears the
    // socket down: the connection was authorized for the previous user, and its
    // subscriptions were granted to them.
    return () => client.close();
  }, [client, sessionStatus, enabled, userId]);

  // --- 2. Observe the status ------------------------------------------------ //
  React.useEffect(() => {
    if (!client) return;
    return client.onStatus(setStatus);
  }, [client]);

  // --- 3. Turn events into cache invalidations ------------------------------ //
  React.useEffect(() => {
    if (!client) return;
    return client.onEvent((event) => applyRealtimeEvent(queryClient, event));
  }, [client, queryClient]);

  // --- 4. Recover from a gap ------------------------------------------------ //
  React.useEffect(() => {
    if (!client || status !== "connected") return;
    if (!client.consumeMissedEvents()) return;

    // Everything, because the client does not know what it missed. Expensive and
    // rare — it happens once per reconnect that spanned a real change — and the
    // alternative is a screen that is quietly wrong, which is the failure mode a
    // synchronization feature exists to prevent.
    void queryClient.invalidateQueries();
  }, [client, queryClient, status]);

  const value = React.useMemo<RealtimeContextValue>(
    () => ({ client, status, enabled }),
    [client, status, enabled],
  );

  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>;
}
