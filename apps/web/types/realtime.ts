/**
 * Real-time event and connection types.
 *
 * The client half of the contract `apps/api/core/events.py` and
 * `apps/api/core/realtime.py` define. Every string union here mirrors a
 * `StrEnum` there — deliberately as unions rather than as TypeScript `enum`s,
 * per `code-standards.md`, so a value that arrives from the wire narrows without
 * a runtime lookup table.
 *
 * **These are the shapes the server sends, not what a component renders.** The
 * hooks in `hooks/use-realtime.ts` turn a stream of these into cache
 * invalidations; nothing in `components/` should need to import an event type.
 */

/** What a topic — and therefore an event — is about. */
export type EventScope = "case" | "document" | "report" | "user";

/**
 * A subscription target, `"<scope>:<uuid>"`.
 *
 * A template literal type rather than a plain `string`, so a topic built by
 * concatenation in a component is a compile error rather than a subscription the
 * server silently refuses.
 */
export type EventTopic = `${EventScope}:${string}`;

/** Every event the platform publishes. Mirrors `DomainEventType`. */
export type DomainEventType =
  | "case.created"
  | "case.updated"
  | "case.archived"
  | "case.restored"
  | "case.status_changed"
  | "case.priority_changed"
  | "case.assignment_changed"
  | "document.uploaded"
  | "document.updated"
  | "document.replaced"
  | "document.deleted"
  | "ocr.started"
  | "ocr.completed"
  | "ocr.failed"
  | "indexing.started"
  | "indexing.completed"
  | "indexing.failed"
  | "report.started"
  | "report.progress"
  | "report.generated"
  | "report.failed"
  | "timeline.updated"
  | "user.activated"
  | "user.deactivated"
  | "user.role_changed"
  | "user.password_reset"
  | "notification.created"
  | "notification.read"
  | "presence.changed";

/**
 * One event as it arrives.
 *
 * `payload` is deliberately `Record<string, EventPayloadValue>` rather than a
 * discriminated union keyed on `event`. The server screens and bounds it (see
 * `normalize_payload`), and a union would have to be widened here every time a
 * publisher adds a field — turning "future event types without redesign", which
 * is the property the backend went out of its way to keep, into a frontend
 * release. Consumers read the one or two keys they know about and ignore the
 * rest.
 */
export type EventPayloadValue = string | number | boolean | null | (string | number)[];

export interface RealtimeEvent {
  /** Stable per event. Deduplicate on this — a reconnect can re-offer one. */
  id: string;
  /** Monotonic. A gap means events were missed and the view must refetch. */
  sequence: number;
  event: DomainEventType;
  topic: EventTopic;
  scope: EventScope;
  /** Which case is now stale, including for document and report events. */
  caseId: string | null;
  actorId: string | null;
  occurredAt: string;
  payload: Record<string, EventPayloadValue>;
}

/** Why a frame was refused, or a connection closed. Mirrors `RealtimeErrorCode`. */
export type RealtimeErrorCode =
  | "malformed_frame"
  | "not_authenticated"
  | "already_authenticated"
  | "invalid_token"
  | "forbidden"
  | "invalid_topic"
  | "topic_forbidden"
  | "too_many_subscriptions"
  | "rate_limited"
  | "slow_consumer"
  | "internal";

/**
 * What the connection is doing, as the indicator shows it.
 *
 * Five states rather than a boolean, because they call for different words and
 * different behaviour: `connecting` and `reconnecting` are both "not live yet"
 * but only the second means something was lost, and `offline` is terminal in a
 * way `reconnecting` is not — it is where the client stops retrying because
 * retrying cannot help.
 */
export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "offline";

/** What `/realtime/status` reports. */
export interface RealtimeStatus {
  enabled: boolean;
  heartbeatSeconds: number;
  maxSubscriptions: number;
  connected: boolean;
}

/** One connected account, from `/realtime/presence`. */
export interface PresenceEntry {
  userId: string;
  role: string;
  connections: number;
  since: string;
}

/** Platform-wide channel health, from `/realtime/metrics`. */
export interface RealtimeMetrics {
  since: string;
  enabled: boolean;
  activeConnections: number;
  presentUsers: number;
  totalConnections: number;
  totalDisconnections: number;
  reconnections: number;
  rejectedConnections: number;
  subscribedTopics: number;
  pendingDispatches: number;
  eventsPublished: number;
  eventsRejected: number;
  eventsDelivered: number;
  eventsDenied: number;
  eventsDeduplicated: number;
  failedDeliveries: number;
  averageDeliveryLatencyMs: number | null;
  deliverySuccessRate: number;
  averageFanout: number | null;
  eventsByType: Record<string, number>;
  failuresByCode: Record<string, number>;
  subscriberFailures: Record<string, number>;
  subscribers: string[];
}

/** Build a topic string. The only sanctioned way to make one. */
export function topicFor(scope: EventScope, id: string): EventTopic {
  return `${scope}:${id}`;
}
