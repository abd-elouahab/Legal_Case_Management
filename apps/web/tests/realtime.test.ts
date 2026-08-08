/**
 * Tests for the real-time client and the cache-synchronization table.
 *
 * Two halves, and neither of them needs a server:
 *
 * * `RealtimeClient` is exercised against a fake `WebSocket`, which is what makes
 *   reconnect, backoff, re-subscription, and duplicate suppression testable at
 *   all — every one of them is a timing behaviour that a real socket would make
 *   flaky;
 * * `staleKeysFor` is a pure function, so "does a completed OCR run refresh the
 *   document's badge?" is an assertion about a table rather than about React.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RealtimeClient, realtimeUrl } from "@/lib/realtime/client";
import { staleKeysFor } from "@/lib/realtime/sync";
import { setAccessToken, resetTokenStore } from "@/lib/api/token-store";
import type { EventTopic, RealtimeEvent } from "@/types/realtime";
import { topicFor } from "@/types/realtime";

// --------------------------------------------------------------------------- //
// A fake socket
// --------------------------------------------------------------------------- //

/**
 * A minimal `WebSocket` double.
 *
 * Records what was sent and lets a test drive `onopen`, `onmessage`, and
 * `onclose` by hand — which is the only way to assert on behaviour that is
 * defined entirely by *when* things happen.
 */
class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly OPEN = 1;

  readyState = FakeSocket.OPEN;
  sent: string[] = [];
  closed: { code?: number; reason?: string } | null = null;

  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(code?: number, reason?: string): void {
    this.closed = { code, reason };
  }

  /** Drive the handshake through to `ready`. */
  becomeReady(sequence = 0): void {
    this.onopen?.();
    this.deliver({ type: "ready", connection_id: "c1", sequence, heartbeat_seconds: 25 });
  }

  deliver(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  drop(code = 1006): void {
    this.onclose?.({ code });
  }

  /** The decoded frames this socket was asked to send. */
  frames(): Record<string, unknown>[] {
    return this.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>);
  }

  framesOfType(type: string): Record<string, unknown>[] {
    return this.frames().filter((frame) => frame.type === type);
  }
}

const CASE_ID = "11111111-1111-4111-8111-111111111111";
const DOCUMENT_ID = "22222222-2222-4222-8222-222222222222";
const REPORT_ID = "33333333-3333-4333-8333-333333333333";

const CASE_TOPIC: EventTopic = topicFor("case", CASE_ID);

function event(overrides: Partial<RealtimeEvent> = {}): RealtimeEvent {
  return {
    id: crypto.randomUUID(),
    sequence: 1,
    event: "case.updated",
    topic: CASE_TOPIC,
    scope: "case",
    caseId: CASE_ID,
    actorId: null,
    occurredAt: new Date().toISOString(),
    payload: {},
    ...overrides,
  };
}

/** Let the client's async `authenticate()` settle. */
async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe("RealtimeClient", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeSocket);
    setAccessToken("test-access-token");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    resetTokenStore();
  });

  it("derives its URL from the REST base so one variable configures both", () => {
    expect(realtimeUrl()).toMatch(/^wss?:\/\//);
    expect(realtimeUrl()).toContain("/realtime/ws");
  });

  it("sends the token in the first frame, never in the URL", async () => {
    const client = new RealtimeClient();
    client.connect();

    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    await settle();

    // The credential is a bearer token: a query string would write it into the
    // reverse proxy's access log and the browser's history.
    expect(socket.url).not.toContain("test-access-token");
    expect(socket.framesOfType("authenticate")[0]).toEqual({
      type: "authenticate",
      token: "test-access-token",
    });

    client.close();
  });

  it("reports connected once the server says ready", async () => {
    const client = new RealtimeClient();
    const statuses: string[] = [];
    client.onStatus((status) => statuses.push(status));

    client.connect();
    FakeSocket.instances[0].onopen?.();
    await settle();
    FakeSocket.instances[0].deliver({ type: "ready", sequence: 0 });

    expect(client.currentStatus).toBe("connected");
    expect(statuses).toContain("connecting");
    expect(statuses).toContain("connected");

    client.close();
  });

  it("queues a subscribe frame and reference-counts it", async () => {
    const client = new RealtimeClient();
    client.connect();
    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    await settle();
    socket.deliver({ type: "ready", sequence: 0 });

    // Two panels can legitimately follow the same case; the first to unmount
    // must not cancel the other's updates.
    const first = client.subscribe([CASE_TOPIC]);
    const second = client.subscribe([CASE_TOPIC]);

    expect(socket.framesOfType("subscribe")).toHaveLength(1);

    first();
    expect(socket.framesOfType("unsubscribe")).toHaveLength(0);

    second();
    expect(socket.framesOfType("unsubscribe")).toHaveLength(1);

    client.close();
  });

  it("re-sends every subscription on reconnect", async () => {
    vi.useFakeTimers();
    const client = new RealtimeClient();
    client.connect();

    const first = FakeSocket.instances[0];
    first.onopen?.();
    await settle();
    first.deliver({ type: "ready", sequence: 0 });
    client.subscribe([CASE_TOPIC]);

    first.drop();
    // The backoff is jittered across the interval, so advancing past the ceiling
    // is what makes the assertion deterministic.
    await vi.advanceTimersByTimeAsync(2_000);

    const second = FakeSocket.instances[1];
    expect(second).toBeDefined();
    second.onopen?.();
    await settle();
    second.deliver({ type: "ready", sequence: 5 });

    // A component subscribes once and stops caring that the socket beneath it
    // was replaced.
    expect(second.framesOfType("subscribe")[0]?.topics).toEqual([CASE_TOPIC]);

    client.close();
  });

  it("declares its last sequence on reconnect so the server can report a gap", async () => {
    vi.useFakeTimers();
    const client = new RealtimeClient();
    client.connect();

    const first = FakeSocket.instances[0];
    first.onopen?.();
    await settle();
    first.deliver({ type: "ready", sequence: 0 });
    first.deliver({
      type: "event",
      id: crypto.randomUUID(),
      sequence: 41,
      event: "case.updated",
      topic: CASE_TOPIC,
      scope: "case",
      payload: {},
    });

    first.drop();
    await vi.advanceTimersByTimeAsync(2_000);

    const second = FakeSocket.instances[1];
    second.onopen?.();
    await settle();
    second.deliver({ type: "ready", sequence: 60 });

    expect(second.framesOfType("resume")[0]).toMatchObject({ last_sequence: 41 });

    client.close();
  });

  it("records a gap so the application can refetch", async () => {
    const client = new RealtimeClient();
    client.connect();
    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    await settle();
    socket.deliver({ type: "ready", sequence: 0 });

    socket.deliver({ type: "resumed", last_sequence: 10, current_sequence: 20, gap: true });

    // Read-and-clear: the provider invalidates everything once, not on every
    // subsequent render.
    expect(client.consumeMissedEvents()).toBe(true);
    expect(client.consumeMissedEvents()).toBe(false);

    client.close();
  });

  it("treats a server whose sequence went backwards as a gap", async () => {
    vi.useFakeTimers();
    const client = new RealtimeClient();
    client.connect();

    const first = FakeSocket.instances[0];
    first.onopen?.();
    await settle();
    first.deliver({ type: "ready", sequence: 0 });
    first.deliver({
      type: "event",
      id: crypto.randomUUID(),
      sequence: 90,
      event: "case.updated",
      topic: CASE_TOPIC,
      scope: "case",
      payload: {},
    });

    first.drop();
    await vi.advanceTimersByTimeAsync(2_000);

    // The server restarted and its counter began again: everything on screen is
    // potentially stale and the sequence is no longer comparable.
    FakeSocket.instances[1].onopen?.();
    await settle();
    FakeSocket.instances[1].deliver({ type: "ready", sequence: 3 });

    expect(client.consumeMissedEvents()).toBe(true);

    client.close();
  });

  it("delivers each event once, however often the server offers it", async () => {
    const client = new RealtimeClient();
    const received: RealtimeEvent[] = [];
    client.onEvent((incoming) => received.push(incoming));

    client.connect();
    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    await settle();
    socket.deliver({ type: "ready", sequence: 0 });

    const frame = {
      type: "event",
      id: "the-same-event",
      sequence: 7,
      event: "document.uploaded",
      topic: topicFor("document", DOCUMENT_ID),
      scope: "document",
      case_id: CASE_ID,
      payload: { document_id: DOCUMENT_ID },
    };
    socket.deliver(frame);
    socket.deliver(frame);

    expect(received).toHaveLength(1);
    expect(received[0].caseId).toBe(CASE_ID);

    client.close();
  });

  it("isolates one listener's failure from the others", async () => {
    const client = new RealtimeClient();
    const reached: string[] = [];
    client.onEvent(() => {
      throw new Error("broken handler");
    });
    client.onEvent(() => reached.push("second"));

    client.connect();
    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    await settle();
    socket.deliver({ type: "ready", sequence: 0 });
    socket.deliver({
      type: "event",
      id: crypto.randomUUID(),
      sequence: 1,
      event: "case.updated",
      topic: CASE_TOPIC,
      scope: "case",
      payload: {},
    });

    expect(reached).toEqual(["second"]);

    client.close();
  });

  it("adopts the server's active set wholesale rather than merging", async () => {
    const client = new RealtimeClient();
    client.connect();
    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    await settle();
    socket.deliver({ type: "ready", sequence: 0 });

    client.subscribe([CASE_TOPIC, topicFor("case", DOCUMENT_ID)]);
    socket.deliver({
      type: "subscriptions",
      granted: [CASE_TOPIC],
      refused: [topicFor("case", DOCUMENT_ID)],
      active: [CASE_TOPIC],
    });

    // The server's idea of what this connection follows is the one that decides
    // what arrives.
    expect(client.isFollowing(CASE_TOPIC)).toBe(true);
    expect(client.isFollowing(topicFor("case", DOCUMENT_ID))).toBe(false);

    client.close();
  });

  it("stops retrying when the account may not use the channel", async () => {
    vi.useFakeTimers();
    const client = new RealtimeClient();
    client.connect();

    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    await settle();
    // 4003: authenticated, but without `realtime:connect`. Retrying would be
    // retrying a policy decision.
    socket.drop(4003);
    await vi.advanceTimersByTimeAsync(60_000);

    expect(FakeSocket.instances).toHaveLength(1);
    expect(client.currentStatus).toBe("offline");
  });

  it("gives up after enough consecutive failures", async () => {
    vi.useFakeTimers();
    const client = new RealtimeClient();
    client.connect();

    for (let attempt = 0; attempt < 12; attempt += 1) {
      const socket = FakeSocket.instances.at(-1);
      socket?.onopen?.();
      await settle();
      socket?.drop();
      await vi.advanceTimersByTimeAsync(35_000);
    }

    // "Forever" is indistinguishable from broken; the indicator says so and
    // offers a retry.
    expect(client.currentStatus).toBe("offline");
  });

  it("closing does not retry", async () => {
    vi.useFakeTimers();
    const client = new RealtimeClient();
    client.connect();
    const socket = FakeSocket.instances[0];
    socket.onopen?.();
    await settle();

    client.close();
    socket.drop();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(FakeSocket.instances).toHaveLength(1);
    expect(client.currentStatus).toBe("idle");
  });

  it("closes rather than connecting when there is no session", async () => {
    resetTokenStore();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401 }));

    const client = new RealtimeClient();
    client.connect();
    FakeSocket.instances[0].onopen?.();
    await settle();

    // Not an error and not a retry: the provider opens the client again on
    // sign-in.
    expect(FakeSocket.instances[0].framesOfType("authenticate")).toHaveLength(0);
  });
});

// --------------------------------------------------------------------------- //
// Cache synchronization
// --------------------------------------------------------------------------- //

function keyStrings(incoming: RealtimeEvent): string[] {
  return staleKeysFor(incoming).map((key) => JSON.stringify(key));
}

describe("staleKeysFor", () => {
  it("refreshes the case list and the case itself when a case changes", () => {
    const keys = keyStrings(event({ event: "case.status_changed" }));
    expect(keys.some((key) => key.includes("cases") && key.includes("list"))).toBe(true);
    expect(keys.some((key) => key.includes(CASE_ID))).toBe(true);
  });

  it("refreshes only the list when a case is created", () => {
    // There is no detail to invalidate for a case this client has never read,
    // and inventing a key would leave an empty cache entry behind.
    const keys = keyStrings(event({ event: "case.created" }));
    expect(keys.some((key) => key.includes(CASE_ID))).toBe(false);
  });

  it("restarts both derived pipelines when a document is replaced", () => {
    const keys = keyStrings(
      event({
        event: "document.replaced",
        scope: "document",
        topic: topicFor("document", DOCUMENT_ID),
        payload: { document_id: DOCUMENT_ID },
      }),
    );

    // Without these the badges beside the file would keep showing the previous
    // version's completed extraction and index.
    expect(keys.some((key) => key.includes("ocr"))).toBe(true);
    expect(keys.some((key) => key.includes("indexing"))).toBe(true);
  });

  it("refetches the extracted text only once extraction has completed", () => {
    const started = keyStrings(
      event({
        event: "ocr.started",
        scope: "document",
        topic: topicFor("document", DOCUMENT_ID),
        payload: { document_id: DOCUMENT_ID },
      }),
    );
    const completed = keyStrings(
      event({
        event: "ocr.completed",
        scope: "document",
        topic: topicFor("document", DOCUMENT_ID),
        payload: { document_id: DOCUMENT_ID },
      }),
    );

    // The text is a large response; refetching it when a run merely started
    // would download the previous version's text to replace it with itself.
    expect(started.some((key) => key.includes('"text"'))).toBe(false);
    expect(completed.some((key) => key.includes('"text"'))).toBe(true);
  });

  it("does not refresh the report history on every progress tick", () => {
    const keys = keyStrings(
      event({
        event: "report.progress",
        scope: "report",
        topic: topicFor("report", REPORT_ID),
        payload: { report_id: REPORT_ID, sections_completed: 3, sections_total: 7 },
      }),
    );

    // Progress moves once per section for the length of a run; invalidating the
    // table on each tick would be a dozen refetches of a row whose only changing
    // cell is a bar the open dialog already shows.
    expect(keys.some((key) => key.includes("list"))).toBe(false);
    expect(keys.some((key) => key.includes(REPORT_ID))).toBe(true);
  });

  it("refreshes the report history when a run finishes", () => {
    const keys = keyStrings(
      event({
        event: "report.generated",
        scope: "report",
        topic: topicFor("report", REPORT_ID),
        payload: { report_id: REPORT_ID },
      }),
    );
    expect(keys.some((key) => key.includes("list"))).toBe(true);
  });

  it("scopes a timeline refresh to the case whose history grew", () => {
    const keys = keyStrings(event({ event: "timeline.updated" }));
    expect(keys).toHaveLength(1);
    expect(keys[0]).toContain(CASE_ID);
  });

  it("makes nothing stale for a presence change", () => {
    // Presence visualization is out of scope, so there is no cache to refresh —
    // listed explicitly so that "handled" is visible rather than looking like an
    // omission.
    expect(staleKeysFor(event({ event: "presence.changed", scope: "user" }))).toEqual([]);
  });
});
