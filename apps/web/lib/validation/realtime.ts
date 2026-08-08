/**
 * Zod schemas for the real-time module's HTTP responses.
 *
 * The two administrative reads and the status probe. **The socket's own frames
 * are deliberately not validated here** — they are parsed in
 * `lib/realtime/client.ts` with narrowing type guards instead, because a frame
 * arrives thousands of times more often than a request and running a schema per
 * event would be paying request-shaped costs on a message-shaped path. The
 * client reads only the fields it needs and treats an unknown frame as one to
 * ignore, which is what keeps a protocol addition from being a breaking change.
 *
 * There are **no form schemas**: the channel is read-only by design. A client
 * subscribes and receives; every write on this platform goes through the
 * authorized REST API.
 */

import { z } from "zod";

/** `GET /realtime/status`. */
export const realtimeStatusSchema = z.object({
  enabled: z.boolean(),
  heartbeat_seconds: z.number().int().positive(),
  max_subscriptions: z.number().int().positive(),
  connected: z.boolean(),
});

/** One entry of `GET /realtime/presence`. */
export const presenceEntrySchema = z.object({
  user_id: z.string().uuid(),
  // A plain string rather than the role enum, deliberately: this is an
  // administrative read of whatever the server reports, and a role added to the
  // platform must not turn a valid response into a client error.
  role: z.string(),
  connections: z.number().int().nonnegative(),
  since: z.string(),
});

export const presenceListSchema = z.object({
  items: z.array(presenceEntrySchema),
  total: z.number().int().nonnegative(),
});

/** `GET /realtime/metrics`. */
export const realtimeMetricsSchema = z.object({
  since: z.string(),
  enabled: z.boolean(),
  active_connections: z.number().int().nonnegative(),
  present_users: z.number().int().nonnegative(),
  total_connections: z.number().int().nonnegative(),
  total_disconnections: z.number().int().nonnegative(),
  reconnections: z.number().int().nonnegative(),
  rejected_connections: z.number().int().nonnegative(),
  subscribed_topics: z.number().int().nonnegative(),
  pending_dispatches: z.number().int().nonnegative(),
  events_published: z.number().int().nonnegative(),
  events_rejected: z.number().int().nonnegative(),
  events_delivered: z.number().int().nonnegative(),
  events_denied: z.number().int().nonnegative(),
  events_deduplicated: z.number().int().nonnegative(),
  failed_deliveries: z.number().int().nonnegative(),
  average_delivery_latency_ms: z.number().nullable(),
  delivery_success_rate: z.number(),
  average_fanout: z.number().nullable(),
  events_by_type: z.record(z.string(), z.number().int()),
  failures_by_code: z.record(z.string(), z.number().int()),
  subscriber_failures: z.record(z.string(), z.number().int()),
  subscribers: z.array(z.string()),
});
