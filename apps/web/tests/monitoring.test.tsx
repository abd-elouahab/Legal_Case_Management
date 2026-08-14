/**
 * Tests for the monitoring client.
 *
 * Cover what an operator is *shown*: the wire mapping, the partial-page
 * behaviour that makes this view usable during an incident, the em-dash rule
 * that keeps an unknowable figure from being drawn as a zero, and the two
 * properties this feature must never lose — that a lawyer is never offered the
 * destination, and that nothing identifying reaches the screen.
 *
 * The API is the real boundary: its 401/403, the administrator-only permission,
 * and the salted digest behind "distinct sources" are covered by
 * `tests/integration/test_monitoring.py` and
 * `tests/unit/test_security_monitor.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import { MonitoringView } from "@/components/monitoring/monitoring-view";
import { formatCount, formatDuration, formatPercent, humanize } from "@/components/monitoring/format";
import { sidebarNavigation } from "@/config/navigation";
import { MONITORING_ENDPOINTS } from "@/lib/api/config";
import { fetchMonitoringOverview } from "@/lib/api/monitoring";
import { ROUTES } from "@/lib/routes";
import { isAllowed } from "@/lib/authorization/access";
import messages from "@/messages/en.json";
import { ROLE_PERMISSIONS, mockFetch } from "./helpers";

vi.mock("next/navigation", () => ({
  usePathname: () => ROUTES.monitoring,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

// --------------------------------------------------------------------------- //
// Fixtures
// --------------------------------------------------------------------------- //

function overviewPayload(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-08-13T09:00:00Z",
    state: "degraded",
    health: {
      state: "degraded",
      checked_at: "2026-08-13T09:00:00Z",
      system: {
        started_at: "2026-08-10T09:00:00Z",
        uptime_seconds: 273_600,
        uptime: "3d 4h 0m",
        process_id: 42,
        thread_count: 17,
        python_version: "3.13.1",
        platform: "linux",
        environment: "production",
        version: "0.1.0",
        project_name: "Legal Case Management Platform API",
      },
      dependencies: [
        { name: "postgres", state: "healthy", required: true, detail: null },
        { name: "qdrant", state: "unhealthy", required: false, detail: "connection refused" },
      ],
      external_services: [
        { name: "llm", enabled: true, configured: true, state: "healthy", detail: null },
        {
          name: "whatsapp",
          enabled: false,
          configured: false,
          state: "disabled",
          detail: null,
        },
      ],
      workers: [
        { name: "ocr", running: true, concurrency: 2, state: "healthy" },
        { name: "whatsapp", running: false, concurrency: 2, state: "disabled" },
      ],
    },
    performance: {
      since: "2026-08-10T09:00:00Z",
      requests_total: 1_204,
      requests_by_status: { "2xx": 1_180, "4xx": 20, "5xx": 4 },
      error_rate: 0.33,
      latencies: [
        {
          name: "api_response",
          count: 1_204,
          average_ms: 42.5,
          p50_ms: 30,
          p95_ms: 210,
          p99_ms: 480,
          max_ms: 1_900,
        },
        {
          name: "rag.average_latency_ms",
          count: 12,
          average_ms: 2_400,
          p50_ms: null,
          p95_ms: null,
          p99_ms: null,
          max_ms: null,
        },
      ],
      slowest_routes: [{ route: "/api/v1/reports/{report_id}", average_ms: 820 }],
    },
    jobs: {
      queues: [
        { name: "ocr", pending: 3, processing: 1, depth: 4, completed: null, failed: null },
        { name: "email", pending: 0, processing: 0, depth: 0, completed: 91, failed: 2 },
      ],
      workers: [{ name: "ocr", running: true, concurrency: 2, state: "healthy" }],
      total_depth: 4,
      depths_unavailable: false,
    },
    errors: {
      since: "2026-08-10T09:00:00Z",
      total_errors: 6,
      distinct_errors: 1,
      errors_by_category: { unhandled_exception: 6 },
      errors_by_component: { api: 6 },
      evicted_groups: 0,
      groups: [
        {
          fingerprint: "a1b2c3d4",
          category: "unhandled_exception",
          component: "api",
          exception_type: "IntegrityError",
          location: "case.py:118",
          operation: "/api/v1/cases",
          sample_message: "duplicate key value",
          occurrences: 6,
          first_seen: "2026-08-13T08:00:00Z",
          last_seen: "2026-08-13T08:55:00Z",
          status_code: 500,
          last_trace_id: "0af7651916cd43dd8448eb211c80319c",
        },
      ],
    },
    security: {
      since: "2026-08-10T09:00:00Z",
      total_events: 9,
      events_by_type: { login_failed: 3, login_succeeded: 6 },
      events_by_severity: { warning: 3, info: 6 },
      recent_rates: { login_failed: { "1m": 0, "5m": 1, "15m": 3 } },
      distinct_sources: 2,
      sources_capped: false,
      login_attempts: 9,
      failed_logins: 3,
      login_failure_rate: 33.33,
      recent: [
        {
          occurred_at: "2026-08-13T08:59:00Z",
          event: "login_failed",
          severity: "warning",
          role: null,
          reason: "invalid_credentials",
          source: "9f2c1a4b",
          trace_id: null,
        },
      ],
    },
    traces: {
      since: "2026-08-10T09:00:00Z",
      traces_started: 40,
      traces_recorded: 38,
      spans_started: 214,
      spans_dropped: 0,
      failed_traces: 1,
      traces: [
        {
          trace_id: "0af7651916cd43dd8448eb211c80319c",
          name: "GET /api/v1/cases",
          component: "api",
          started_at: "2026-08-13T08:59:00Z",
          duration_ms: 91.4,
          status: "ok",
          failed: false,
          remote_parent: false,
          dropped_spans: 0,
          spans: [
            {
              name: "GET /api/v1/cases",
              component: "api",
              kind: "server",
              span_id: "b7ad6b7169203331",
              parent_span_id: null,
              started_at: "2026-08-13T08:59:00Z",
              duration_ms: 91.4,
              status: "ok",
              error_type: null,
              error_message: null,
            },
          ],
        },
      ],
    },
    alerts: {
      evaluated_at: "2026-08-13T09:00:00Z",
      firing: 1,
      alerts: [
        {
          key: "vector_unavailable",
          severity: "warning",
          component: "vector",
          summary: "Qdrant is not reachable.",
          firing: true,
          value: null,
          threshold: null,
          detail: "connection refused",
        },
        {
          key: "error_rate_high",
          severity: "critical",
          component: "api",
          summary: "The share of requests answered with a server error is above its threshold.",
          firing: false,
          value: 0.33,
          threshold: 5,
          detail: null,
        },
      ],
    },
    unavailable: [],
    ...overrides,
  };
}

function renderView() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return render(
    <NextIntlClientProvider locale="en" messages={messages} timeZone="UTC">
      <QueryClientProvider client={queryClient}>
        <MonitoringView />
      </QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

// --------------------------------------------------------------------------- //
// Tests
// --------------------------------------------------------------------------- //

describe("the monitoring navigation entry", () => {
  it("is gated on monitoring:view", () => {
    const item = sidebarNavigation
      .flatMap((section) => section.items)
      .find((candidate) => candidate.href === ROUTES.monitoring);

    expect(item?.access).toEqual({ permission: "monitoring:view" });
  });

  it("is offered to an administrator and to nobody else", () => {
    // The sidebar filter and the route guard read this same rule, so a role that
    // fails it is never shown the destination *and* cannot open it by URL.
    const rule = { permission: "monitoring:view" } as const;

    expect(
      isAllowed(rule, {
        role: "administrator",
        permissions: ROLE_PERMISSIONS.administrator,
      }),
    ).toBe(true);
    expect(
      isAllowed(rule, { role: "lawyer", permissions: ROLE_PERMISSIONS.lawyer }),
    ).toBe(false);
    expect(
      isAllowed(rule, { role: "court", permissions: ROLE_PERMISSIONS.court }),
    ).toBe(false);
  });
});

describe("the monitoring wire mapping", () => {
  it("maps the aggregate into domain types", async () => {
    mockFetch({ [MONITORING_ENDPOINTS.overview]: { body: overviewPayload() } });

    const overview = await fetchMonitoringOverview();

    expect(overview.state).toBe("degraded");
    expect(overview.health.system.uptime).toBe("3d 4h 0m");
    expect(overview.jobs.queues[0]).toMatchObject({ name: "ocr", pending: 3, processing: 1 });
    expect(overview.errors.groups[0].exceptionType).toBe("IntegrityError");
    expect(overview.security.loginFailureRate).toBeCloseTo(33.33);
    expect(overview.traces.traces[0].traceId).toBe("0af7651916cd43dd8448eb211c80319c");
  });

  it("rejects a payload whose health state it cannot draw", async () => {
    // Strict where a value decides a colour, loose where the server's vocabulary
    // is open — see `lib/validation/monitoring.ts`.
    mockFetch({
      [MONITORING_ENDPOINTS.overview]: { body: overviewPayload({ state: "on fire" }) },
    });

    await expect(fetchMonitoringOverview()).rejects.toThrow();
  });

  it("accepts a component this build has never heard of", async () => {
    const payload = overviewPayload();
    (payload.errors as { groups: { component: string }[] }).groups[0].component = "quantum";
    mockFetch({ [MONITORING_ENDPOINTS.overview]: { body: payload } });

    const overview = await fetchMonitoringOverview();
    expect(overview.errors.groups[0].component).toBe("quantum");
  });
});

describe("the monitoring view", () => {
  it("shows the platform state, its dependencies, and what is firing", async () => {
    mockFetch({ [MONITORING_ENDPOINTS.overview]: { body: overviewPayload() } });
    renderView();

    await waitFor(() => expect(screen.getAllByText("Degraded").length).toBeGreaterThan(0));
    expect(screen.getByText("Postgres")).toBeInTheDocument();
    expect(screen.getByText(/Qdrant is not reachable/)).toBeInTheDocument();
    expect(screen.getByText("3d 4h 0m")).toBeInTheDocument();
  });

  it("shows only the alerts that are firing", async () => {
    mockFetch({ [MONITORING_ENDPOINTS.overview]: { body: overviewPayload() } });
    renderView();

    await waitFor(() => expect(screen.getByText(/Qdrant is not reachable/)).toBeInTheDocument());
    expect(
      screen.queryByText("Too many requests are being answered with a server error."),
    ).not.toBeInTheDocument();
  });

  it("says which sections are missing rather than going blank", async () => {
    // A page that failed because one of its eight parts was unavailable would be
    // useless at exactly the moment it is needed.
    mockFetch({
      [MONITORING_ENDPOINTS.overview]: { body: overviewPayload({ unavailable: ["jobs"] }) },
    });
    renderView();

    await waitFor(() => expect(screen.getByText(/Some sections/)).toBeInTheDocument());
    expect(screen.getByText(/Jobs/)).toBeInTheDocument();
  });

  it("never renders an account, an address, or a credential", async () => {
    mockFetch({ [MONITORING_ENDPOINTS.overview]: { body: overviewPayload() } });
    const { container } = renderView();

    await waitFor(() => expect(screen.getByText("Postgres")).toBeInTheDocument());
    const rendered = container.textContent ?? "";
    expect(rendered).not.toMatch(/@example\.com/);
    expect(rendered).not.toMatch(/\d{1,3}(\.\d{1,3}){3}/);
  });
});

describe("the monitoring formatters", () => {
  it("renders an em dash for a figure that cannot be known", () => {
    // `null` means *not knowable from a mean-and-count recorder*; `0` would read
    // as *instantaneous*, which is a confidently wrong statement.
    expect(formatDuration(null)).toBe("—");
    expect(formatCount(null)).toBe("—");
    expect(formatPercent(null)).toBe("—");
  });

  it("scales a duration so it is read at a glance", () => {
    expect(formatDuration(0.42)).toBe("0.42 ms");
    expect(formatDuration(42.5)).toBe("42.5 ms");
    expect(formatDuration(1_483.2)).toBe("1.48 s");
  });

  it("humanizes an identifier this build has no translation for", () => {
    expect(humanize("background_job")).toBe("Background job");
    expect(humanize("external-service")).toBe("External service");
  });
});
