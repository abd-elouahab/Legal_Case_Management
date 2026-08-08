/**
 * Real-time API calls.
 *
 * Thin, typed wrappers over the module's three HTTP endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape.
 *
 * **The socket is not here.** It is not a request, it authenticates differently,
 * and its lifecycle is a connection rather than a round trip — see
 * `lib/realtime/client.ts`. What lives here is what a client asks *about* the
 * channel over ordinary HTTP.
 */

import { apiRequest } from "@/lib/api/client";
import { REALTIME_ENDPOINTS } from "@/lib/api/config";
import {
  presenceListSchema,
  realtimeMetricsSchema,
  realtimeStatusSchema,
} from "@/lib/validation/realtime";
import type { PresenceEntry, RealtimeMetrics, RealtimeStatus } from "@/types/realtime";

/**
 * Whether this deployment offers live updates, and on what terms.
 *
 * Called once before the socket is opened. A deployment with the channel turned
 * off is a supported configuration rather than a failure, and asking first is
 * what turns "live updates are unavailable here" into a single quiet answer
 * instead of a reconnect loop against a server that will never accept one.
 */
export async function fetchRealtimeStatus(): Promise<RealtimeStatus> {
  const payload = realtimeStatusSchema.parse(
    await apiRequest<unknown>(REALTIME_ENDPOINTS.status),
  );

  return {
    enabled: payload.enabled,
    heartbeatSeconds: payload.heartbeat_seconds,
    maxSubscriptions: payload.max_subscriptions,
    connected: payload.connected,
  };
}

/** Who currently holds a live connection. Requires `realtime:monitor`. */
export async function fetchPresence(): Promise<PresenceEntry[]> {
  const payload = presenceListSchema.parse(
    await apiRequest<unknown>(REALTIME_ENDPOINTS.presence),
  );

  return payload.items.map((entry) => ({
    userId: entry.user_id,
    role: entry.role,
    connections: entry.connections,
    since: entry.since,
  }));
}

/** Platform-wide channel health. Requires `realtime:monitor`. */
export async function fetchRealtimeMetrics(): Promise<RealtimeMetrics> {
  const payload = realtimeMetricsSchema.parse(
    await apiRequest<unknown>(REALTIME_ENDPOINTS.metrics),
  );

  return {
    since: payload.since,
    enabled: payload.enabled,
    activeConnections: payload.active_connections,
    presentUsers: payload.present_users,
    totalConnections: payload.total_connections,
    totalDisconnections: payload.total_disconnections,
    reconnections: payload.reconnections,
    rejectedConnections: payload.rejected_connections,
    subscribedTopics: payload.subscribed_topics,
    pendingDispatches: payload.pending_dispatches,
    eventsPublished: payload.events_published,
    eventsRejected: payload.events_rejected,
    eventsDelivered: payload.events_delivered,
    eventsDenied: payload.events_denied,
    eventsDeduplicated: payload.events_deduplicated,
    failedDeliveries: payload.failed_deliveries,
    averageDeliveryLatencyMs: payload.average_delivery_latency_ms,
    deliverySuccessRate: payload.delivery_success_rate,
    averageFanout: payload.average_fanout,
    eventsByType: payload.events_by_type,
    failuresByCode: payload.failures_by_code,
    subscriberFailures: payload.subscriber_failures,
    subscribers: payload.subscribers,
  };
}
