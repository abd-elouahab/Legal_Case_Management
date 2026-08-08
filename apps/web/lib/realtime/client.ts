/**
 * The platform's WebSocket client.
 *
 * One socket per browser tab, owned by `RealtimeProvider` and reached through the
 * hooks in `hooks/use-realtime.ts`. **No component talks to this directly**, for
 * the same reason none of them talks to `lib/api/client.ts` directly: reconnect
 * timers, subscription reference counts, and duplicate suppression are not
 * rendering concerns.
 *
 * Four properties it guarantees, each of which is one of the spec's
 * requirements:
 *
 * * **Automatic reconnect with backoff and jitter.** A dropped connection is
 *   retried on an exponential schedule, and the delay is jittered because a
 *   server restart drops every client at once — an unjittered backoff would then
 *   bring them all back in the same instant, which is the thundering herd that
 *   turns a five-second restart into a minute-long outage.
 * * **Subscriptions survive a reconnect.** The client remembers what it was
 *   asked to follow and re-sends the whole set on every `ready`, so a component
 *   subscribes once and stops caring whether the socket beneath it has been
 *   replaced.
 * * **Duplicate events are dropped.** A reconnect legitimately re-offers events;
 *   every one carries a stable id, and a bounded window of recent ids is what
 *   stops a handler running twice.
 * * **Graceful degradation.** Every failure path ends in a *status*, never a
 *   thrown error. If the socket never connects the application is exactly as
 *   usable as it was before this feature existed — the REST API is unaffected,
 *   and the polling every feature already does is the fallback.
 *
 * **Authentication is the first frame, never the URL.** The access token lives in
 * memory (`lib/api/token-store.ts`) and is sent as `{"type":"authenticate"}`
 * immediately after the socket opens. Putting it in a query string would write a
 * bearer credential into the reverse proxy's access log and the browser's
 * history — the three logs `lib/api/config.ts` records the platform refusing to
 * put a *search query* into, and a credential is considerably worse.
 */

import { API_BASE_URL, API_V1_PREFIX } from "@/lib/api/config";
import { refreshAccessToken } from "@/lib/api/client";
import { getAccessToken } from "@/lib/api/token-store";
import type {
  ConnectionStatus,
  EventTopic,
  RealtimeErrorCode,
  RealtimeEvent,
} from "@/types/realtime";

/** First reconnect delay, in milliseconds. */
const BASE_RECONNECT_DELAY_MS = 1_000;

/**
 * Ceiling on the reconnect delay.
 *
 * Thirty seconds: long enough that a client waiting out a deployment is not
 * hammering the server, short enough that somebody who stepped away comes back
 * to a live page rather than a stale one.
 */
const MAX_RECONNECT_DELAY_MS = 30_000;

/**
 * Consecutive failures before the client gives up and reports `offline`.
 *
 * It gives up rather than retrying forever because forever is indistinguishable
 * from broken: at this point the honest thing is to tell the user updates are
 * not live so they know to refresh, and to offer a manual retry.
 */
const MAX_RECONNECT_ATTEMPTS = 8;

/** How many recent event ids are remembered for duplicate suppression. */
const DEDUPE_WINDOW = 512;

/** Close codes the server uses. Mirrors `apps/api/core/realtime.py`. */
const CLOSE_UNAUTHENTICATED = 4002;
const CLOSE_FORBIDDEN = 4003;

/** A subscriber's callback. */
export type EventListener = (event: RealtimeEvent) => void;

/** A status observer's callback. */
export type StatusListener = (status: ConnectionStatus) => void;

interface ServerFrame {
  type: string;
  [key: string]: unknown;
}

/** The WebSocket URL, derived from the REST base so one variable configures both. */
export function realtimeUrl(): string {
  const base = API_BASE_URL.replace(/^http/, "ws");
  return `${base}${API_V1_PREFIX}/realtime/ws`;
}

export class RealtimeClient {
  private socket: WebSocket | null = null;
  private status: ConnectionStatus = "idle";
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** Set while `close()` has been called, so a deliberate close never retries. */
  private stopped = false;

  /**
   * Topic → how many components asked for it.
   *
   * Reference counted because two panels can legitimately follow the same case —
   * a case workspace and the report list inside it — and the first one to unmount
   * must not cancel the other's updates.
   */
  private readonly subscriptions = new Map<EventTopic, number>();
  /** Topics the server has confirmed. Reset on every reconnect. */
  private readonly confirmed = new Set<EventTopic>();

  private readonly listeners = new Set<EventListener>();
  private readonly statusListeners = new Set<StatusListener>();

  /** Recent event ids, oldest first. A `Set` preserves insertion order in JS. */
  private readonly seen = new Set<string>();

  /** Highest sequence received, sent on reconnect so the server can report a gap. */
  private lastSequence = 0;
  /** Set when a reconnect discovered missed events; read once by the provider. */
  private missedEvents = false;

  // ----------------------------------------------------------- lifecycle -- //

  /** Open the connection. Idempotent — calling it twice does not open two. */
  connect(): void {
    this.stopped = false;
    if (this.socket || this.reconnectTimer) return;
    this.open();
  }

  /**
   * Close the connection and stop retrying.
   *
   * Subscriptions are kept: a provider that closes on sign-out and reopens on
   * sign-in should not make every mounted panel re-register.
   */
  close(): void {
    this.stopped = true;
    this.clearTimer();
    this.confirmed.clear();

    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onclose = null;
      socket.onerror = null;
      socket.onmessage = null;
      socket.onopen = null;
      socket.close(1000, "client closed");
    }

    this.setStatus("idle");
  }

  /** Retry now, resetting the backoff. What the indicator's "Retry" calls. */
  retry(): void {
    this.attempts = 0;
    this.clearTimer();
    if (this.socket) return;
    this.stopped = false;
    this.open();
  }

  // -------------------------------------------------------- subscriptions -- //

  /**
   * Follow a topic until the returned function is called.
   *
   * Returns an unsubscribe rather than exposing a `remove` method, because the
   * caller is a React effect and an effect's cleanup is exactly this shape — it
   * makes "forgot to unsubscribe" impossible to express.
   */
  subscribe(topics: readonly EventTopic[]): () => void {
    const added: EventTopic[] = [];

    for (const topic of topics) {
      const count = this.subscriptions.get(topic) ?? 0;
      this.subscriptions.set(topic, count + 1);
      if (count === 0) added.push(topic);
    }

    if (added.length > 0) this.send({ type: "subscribe", topics: added });

    return () => {
      const removed: EventTopic[] = [];
      for (const topic of topics) {
        const count = this.subscriptions.get(topic) ?? 0;
        if (count <= 1) {
          this.subscriptions.delete(topic);
          this.confirmed.delete(topic);
          removed.push(topic);
        } else {
          this.subscriptions.set(topic, count - 1);
        }
      }
      if (removed.length > 0) this.send({ type: "unsubscribe", topics: removed });
    };
  }

  /** Whether the server has confirmed this topic on the current connection. */
  isFollowing(topic: EventTopic): boolean {
    return this.confirmed.has(topic);
  }

  // ------------------------------------------------------------ observers -- //

  /** Receive every event. Returns an unsubscribe. */
  onEvent(listener: EventListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Observe the connection status, immediately and on every change. */
  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    listener(this.status);
    return () => this.statusListeners.delete(listener);
  }

  get currentStatus(): ConnectionStatus {
    return this.status;
  }

  /**
   * Whether the last reconnect discovered a gap, clearing the flag.
   *
   * Read-and-clear rather than a plain getter: the provider reacts by
   * invalidating every query, and a flag that stayed set would make it do so on
   * every subsequent render.
   */
  consumeMissedEvents(): boolean {
    const missed = this.missedEvents;
    this.missedEvents = false;
    return missed;
  }

  // -------------------------------------------------------------- internal -- //

  private open(): void {
    if (typeof window === "undefined") return;

    this.setStatus(this.attempts === 0 ? "connecting" : "reconnecting");

    let socket: WebSocket;
    try {
      socket = new WebSocket(realtimeUrl());
    } catch {
      // A malformed URL or a blocked scheme. Retrying cannot fix either, but the
      // schedule is what reports `offline` after enough attempts rather than
      // leaving the indicator stuck on "connecting" forever.
      this.scheduleReconnect();
      return;
    }

    this.socket = socket;
    socket.onopen = () => void this.authenticate();
    socket.onmessage = (message) => this.receive(message);
    socket.onerror = () => {
      // Browsers deliberately give no detail here (it would be a cross-origin
      // information leak). `onclose` always follows, and carries the code that
      // decides whether retrying is worth anything — so nothing is done here.
    };
    socket.onclose = (event) => this.handleClose(event);
  }

  private async authenticate(): Promise<void> {
    // The token may have expired while the tab was in the background. Refreshing
    // *before* the first frame turns what would be a 4002-and-reconnect into an
    // ordinary connect — and the shared in-flight refresh in `lib/api/client.ts`
    // means this costs nothing when a REST call is already renewing it.
    const token = getAccessToken() ?? (await refreshAccessToken());

    if (!token) {
      // No session. Not an error and not a retry: the provider closes the client
      // on sign-out, and the sign-in flow opens it again.
      this.close();
      return;
    }

    this.sendRaw({ type: "authenticate", token });
  }

  private receive(message: MessageEvent<string>): void {
    let frame: ServerFrame;
    try {
      frame = JSON.parse(message.data) as ServerFrame;
    } catch {
      return;
    }

    switch (frame.type) {
      case "ready":
        this.handleReady(frame);
        return;
      case "event":
        this.handleEvent(frame);
        return;
      case "subscriptions":
        this.handleSubscriptions(frame);
        return;
      case "resumed":
        this.handleResumed(frame);
        return;
      case "error":
        this.handleError(frame);
        return;
      default:
        // `pong`, and anything a future server version adds. Ignored rather than
        // treated as an error: a client that broke on an unknown frame would make
        // every protocol addition a breaking change.
        return;
    }
  }

  private handleReady(frame: ServerFrame): void {
    this.attempts = 0;
    this.setStatus("connected");
    this.confirmed.clear();

    // Declared before re-subscribing, so the answer arrives while the
    // subscriptions are being authorized rather than after.
    if (this.lastSequence > 0) {
      this.sendRaw({ type: "resume", last_sequence: this.lastSequence });
    }

    const topics = [...this.subscriptions.keys()];
    if (topics.length > 0) this.sendRaw({ type: "subscribe", topics });

    // A `ready` on a socket that carries a *lower* sequence than we have seen
    // means the server restarted and its counter began again. Everything on
    // screen is potentially stale and the sequence is no longer comparable, so
    // the client resets it and asks the application to refetch.
    const sequence = typeof frame.sequence === "number" ? frame.sequence : 0;
    if (sequence < this.lastSequence) {
      this.lastSequence = 0;
      this.missedEvents = true;
    }
  }

  private handleEvent(frame: ServerFrame): void {
    const id = typeof frame.id === "string" ? frame.id : null;
    if (!id || this.seen.has(id)) return;

    this.remember(id);

    const event: RealtimeEvent = {
      id,
      sequence: typeof frame.sequence === "number" ? frame.sequence : 0,
      event: frame.event as RealtimeEvent["event"],
      topic: frame.topic as EventTopic,
      scope: frame.scope as RealtimeEvent["scope"],
      caseId: typeof frame.case_id === "string" ? frame.case_id : null,
      actorId: typeof frame.actor_id === "string" ? frame.actor_id : null,
      occurredAt: typeof frame.occurred_at === "string" ? frame.occurred_at : "",
      payload: (frame.payload ?? {}) as RealtimeEvent["payload"],
    };

    this.lastSequence = Math.max(this.lastSequence, event.sequence);

    for (const listener of this.listeners) {
      try {
        listener(event);
      } catch {
        // One handler that throws must not deny the event to the others — the
        // same isolation the server's dispatcher applies to its subscribers.
      }
    }
  }

  private handleSubscriptions(frame: ServerFrame): void {
    // The server echoes the **complete** active set, so it is adopted wholesale
    // rather than merged. Merging would let a client's idea of what it follows
    // drift from the server's, and the server's is the one that decides what
    // arrives.
    const active = Array.isArray(frame.active) ? (frame.active as EventTopic[]) : [];
    this.confirmed.clear();
    for (const topic of active) this.confirmed.add(topic);
  }

  private handleResumed(frame: ServerFrame): void {
    if (frame.gap === true) this.missedEvents = true;
  }

  private handleError(frame: ServerFrame): void {
    const code = frame.error as RealtimeErrorCode | undefined;

    // A refused topic is not a connection failure: the rest of the subscriptions
    // are live, and the panel that asked for it falls back to the polling it
    // already does. Nothing is surfaced, because the user cannot act on it and a
    // toast reading "you do not have access" for a case they never opened would
    // be alarming and wrong.
    if (code === "topic_forbidden" || code === "invalid_topic") return;

    if (code === "invalid_token") {
      // The socket is about to close with 4002. Refreshing here means the
      // reconnect that follows carries a valid credential.
      void refreshAccessToken();
    }
  }

  private handleClose(event: CloseEvent): void {
    this.socket = null;
    this.confirmed.clear();

    if (this.stopped) return;

    // Terminal: the account may not use this channel at all. Retrying would be
    // retrying a policy decision, so the client stops and the indicator says so.
    if (event.code === CLOSE_FORBIDDEN) {
      this.stopped = true;
      this.setStatus("offline");
      return;
    }

    // An expired credential is *not* terminal, and it is the common case for a
    // tab that has been in the background: the reconnect refreshes first.
    if (event.code === CLOSE_UNAUTHENTICATED) this.attempts = 0;

    // Anything received before the drop is history now; the sequence is kept so
    // the reconnect can ask whether anything was missed.
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;

    if (this.attempts >= MAX_RECONNECT_ATTEMPTS) {
      this.setStatus("offline");
      return;
    }

    // Exponential, capped, then jittered across the full interval. Full jitter
    // rather than a small random addition: the herd this exists to break up is
    // every client of a restarted server, and they all computed the same delay.
    const ceiling = Math.min(
      BASE_RECONNECT_DELAY_MS * 2 ** this.attempts,
      MAX_RECONNECT_DELAY_MS,
    );
    const delay = Math.random() * ceiling;

    this.attempts += 1;
    this.setStatus("reconnecting");

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  private clearTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private remember(id: string): void {
    this.seen.add(id);
    if (this.seen.size > DEDUPE_WINDOW) {
      const oldest = this.seen.values().next();
      if (!oldest.done) this.seen.delete(oldest.value);
    }
  }

  private send(payload: Record<string, unknown>): void {
    // Dropped silently when the socket is not open, and that is correct rather
    // than lossy: every subscription is re-sent in full on the next `ready`, so
    // a frame that could not be delivered now is delivered by the reconnect.
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.sendRaw(payload);
  }

  private sendRaw(payload: Record<string, unknown>): void {
    try {
      this.socket?.send(JSON.stringify(payload));
    } catch {
      // The socket closed between the check and the write. `onclose` handles it.
    }
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const listener of this.statusListeners) listener(status);
  }
}
