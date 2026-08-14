/**
 * Monitoring domain types.
 *
 * The browser's view of `GET /api/v1/monitoring/overview`, in camelCase. Every
 * value that decides how something is *drawn* — a health state, an alert
 * severity, a component, a security event — is a **stable identifier** rather
 * than a sentence, exactly as `code-standards.md` requires of every API on this
 * platform: the words live in `messages/*.json` and are chosen by the reader's
 * language, not by the server's.
 *
 * **There is no `createMonitoring` and there will not be one.** Monitoring owns
 * no data, writes nothing, and exposes no mutation; every type here describes a
 * read.
 */

/** How well one thing — a dependency, a subsystem, the platform — is doing. */
export const HEALTH_STATES = [
  "healthy",
  "degraded",
  "unhealthy",
  "disabled",
  "unknown",
] as const;

export type HealthState = (typeof HEALTH_STATES)[number];

/** Severity of a declared alert condition. */
export type AlertSeverity = "info" | "warning" | "critical";

/** Severity of one security event. */
export type SecuritySeverity = "info" | "warning" | "critical";

/** The process and its runtime. */
export interface SystemInfo {
  startedAt: string;
  uptimeSeconds: number;
  /** Pre-rendered by the server, so two screens cannot round it differently. */
  uptime: string;
  processId: number;
  threadCount: number;
  pythonVersion: string;
  platform: string;
  environment: string;
  version: string;
  projectName: string;
}

/** One backing service's reachability. */
export interface DependencyHealth {
  name: string;
  state: HealthState;
  /** Whether the platform can serve at all without it. */
  required: boolean;
  detail: string | null;
}

/** One outward-facing integration, as configuration describes it. */
export interface ExternalService {
  name: string;
  enabled: boolean;
  configured: boolean;
  state: HealthState;
  /** Which setting is missing, by name — never a value. */
  detail: string | null;
}

/** One background worker pool. */
export interface WorkerPool {
  name: string;
  running: boolean;
  concurrency: number;
  state: HealthState;
}

/** The platform's operational state. */
export interface HealthReport {
  state: HealthState;
  checkedAt: string;
  system: SystemInfo;
  dependencies: DependencyHealth[];
  externalServices: ExternalService[];
  workers: WorkerPool[];
}

/** One latency distribution. `null` percentiles mean *not knowable*, never zero. */
export interface Latency {
  name: string;
  count: number;
  averageMs: number | null;
  p50Ms: number | null;
  p95Ms: number | null;
  p99Ms: number | null;
  maxMs: number | null;
}

/** Throughput, latency, and the error rate. */
export interface Performance {
  since: string;
  requestsTotal: number;
  requestsByStatus: Record<string, number>;
  errorRate: number;
  latencies: Latency[];
  slowestRoutes: { route: string; averageMs: number }[];
}

/** One background queue's depth, counted from persisted rows. */
export interface JobQueue {
  name: string;
  pending: number;
  processing: number;
  depth: number;
  completed: number | null;
  failed: number | null;
}

/** Background processing across every queue and pool. */
export interface JobsReport {
  queues: JobQueue[];
  workers: WorkerPool[];
  totalDepth: number;
  /** True when the depths could not be read — empty is then not the same as zero. */
  depthsUnavailable: boolean;
}

/** One *class* of failure, grouped by type and location. */
export interface TrackedError {
  fingerprint: string;
  category: string;
  component: string;
  exceptionType: string;
  location: string | null;
  operation: string | null;
  sampleMessage: string | null;
  occurrences: number;
  firstSeen: string;
  lastSeen: string;
  statusCode: number | null;
  lastTraceId: string | null;
}

/** Tracked failures and their totals. */
export interface ErrorsReport {
  since: string;
  totalErrors: number;
  distinctErrors: number;
  errorsByCategory: Record<string, number>;
  errorsByComponent: Record<string, number>;
  evictedGroups: number;
  groups: TrackedError[];
}

/** One security event. Carries no account, no address, and no credential. */
export interface SecurityEvent {
  occurredAt: string;
  event: string;
  severity: SecuritySeverity;
  role: string | null;
  reason: string | null;
  /** A salted digest prefix — correlates without naming, meaningless elsewhere. */
  source: string | null;
  traceId: string | null;
}

/** Security counters, windowed rates, and the recent feed. */
export interface SecurityReport {
  since: string;
  totalEvents: number;
  eventsByType: Record<string, number>;
  eventsBySeverity: Record<string, number>;
  recentRates: Record<string, Record<string, number>>;
  distinctSources: number;
  sourcesCapped: boolean;
  loginAttempts: number;
  failedLogins: number;
  loginFailureRate: number;
  recent: SecurityEvent[];
}

/** One timed unit of work inside a trace. */
export interface Span {
  name: string;
  component: string;
  kind: string;
  spanId: string;
  parentSpanId: string | null;
  startedAt: string;
  durationMs: number;
  status: string;
  errorType: string | null;
  errorMessage: string | null;
}

/** One completed trace. */
export interface TracedRequest {
  traceId: string;
  name: string;
  component: string;
  startedAt: string;
  durationMs: number;
  status: string;
  failed: boolean;
  remoteParent: boolean;
  droppedSpans: number;
  spans: Span[];
}

/** The tracer's counters and its most recent traces. */
export interface TracesReport {
  since: string;
  tracesStarted: number;
  tracesRecorded: number;
  spansStarted: number;
  spansDropped: number;
  failedTraces: number;
  traces: TracedRequest[];
}

/** One declared condition and whether it currently holds. */
export interface Alert {
  key: string;
  severity: AlertSeverity;
  component: string;
  /** The server's own sentence. Rendered only when the client has no key for it. */
  summary: string;
  firing: boolean;
  value: number | null;
  threshold: number | null;
  detail: string | null;
}

/** Every declared condition, evaluated. Nothing is delivered. */
export interface AlertsReport {
  evaluatedAt: string;
  firing: number;
  alerts: Alert[];
}

/** Everything an operator's first screen needs. */
export interface MonitoringOverview {
  generatedAt: string;
  state: HealthState;
  health: HealthReport;
  performance: Performance;
  jobs: JobsReport;
  errors: ErrorsReport;
  security: SecurityReport;
  traces: TracesReport;
  alerts: AlertsReport;
  /** Sections that could not be assembled. Empty on a healthy read. */
  unavailable: string[];
}

/**
 * Tailwind classes per health state.
 *
 * A map rather than a conditional at each call site, so the badge, the
 * dependency row, and the worker chip cannot disagree about what "degraded"
 * looks like. **Tokens only** — `ui-context.md` forbids hardcoded colours, and
 * every value here resolves through the design system's palette.
 */
export const HEALTH_STATE_CLASSES: Record<HealthState, string> = {
  healthy: "bg-success/15 text-success border-success/30",
  degraded: "bg-warning/15 text-warning border-warning/30",
  unhealthy: "bg-destructive/15 text-destructive border-destructive/30",
  disabled: "bg-muted text-muted-foreground border-border",
  unknown: "bg-muted text-muted-foreground border-border",
};

/** Tailwind classes per alert severity. */
export const ALERT_SEVERITY_CLASSES: Record<AlertSeverity, string> = {
  info: "bg-info/15 text-info border-info/30",
  warning: "bg-warning/15 text-warning border-warning/30",
  critical: "bg-destructive/15 text-destructive border-destructive/30",
};
