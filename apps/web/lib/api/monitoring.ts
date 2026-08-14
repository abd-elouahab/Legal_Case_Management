/**
 * Monitoring API calls.
 *
 * Thin, typed wrappers over the `/monitoring` endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape —
 * and a backend change that alters a payload fails here, loudly, instead of
 * surfacing as `undefined` on a page somebody is reading during an incident.
 *
 * **Every function here is a read.** Monitoring owns no data and exposes no
 * mutation; there is no `createX` in this file and there will not be one.
 */

import { apiRequest } from "@/lib/api/client";
import { MONITORING_ENDPOINTS } from "@/lib/api/config";
import {
  alertsSchema,
  errorsSchema,
  healthReportSchema,
  jobsSchema,
  monitoringOverviewSchema,
  performanceSchema,
  securitySchema,
  tracesSchema,
} from "@/lib/validation/monitoring";
import type {
  Alert,
  AlertsReport,
  DependencyHealth,
  ErrorsReport,
  ExternalService,
  HealthReport,
  JobQueue,
  JobsReport,
  Latency,
  MonitoringOverview,
  Performance,
  SecurityEvent,
  SecurityReport,
  Span,
  TracedRequest,
  TracesReport,
  TrackedError,
  WorkerPool,
} from "@/types/monitoring";

type HealthWire = ReturnType<typeof healthReportSchema.parse>;
type PerformanceWire = ReturnType<typeof performanceSchema.parse>;
type JobsWire = ReturnType<typeof jobsSchema.parse>;
type ErrorsWire = ReturnType<typeof errorsSchema.parse>;
type SecurityWire = ReturnType<typeof securitySchema.parse>;
type TracesWire = ReturnType<typeof tracesSchema.parse>;
type AlertsWire = ReturnType<typeof alertsSchema.parse>;
type OverviewWire = ReturnType<typeof monitoringOverviewSchema.parse>;

// --------------------------------------------------------------------------- //
// Wire → domain
// --------------------------------------------------------------------------- //

function toWorker(payload: JobsWire["workers"][number]): WorkerPool {
  return {
    name: payload.name,
    running: payload.running,
    concurrency: payload.concurrency,
    state: payload.state,
  };
}

function toDependency(payload: HealthWire["dependencies"][number]): DependencyHealth {
  return {
    name: payload.name,
    state: payload.state,
    required: payload.required,
    detail: payload.detail,
  };
}

function toExternalService(
  payload: HealthWire["external_services"][number],
): ExternalService {
  return {
    name: payload.name,
    enabled: payload.enabled,
    configured: payload.configured,
    state: payload.state,
    detail: payload.detail,
  };
}

/** Map the health report. */
export function toHealthReport(payload: HealthWire): HealthReport {
  return {
    state: payload.state,
    checkedAt: payload.checked_at,
    system: {
      startedAt: payload.system.started_at,
      uptimeSeconds: payload.system.uptime_seconds,
      uptime: payload.system.uptime,
      processId: payload.system.process_id,
      threadCount: payload.system.thread_count,
      pythonVersion: payload.system.python_version,
      platform: payload.system.platform,
      environment: payload.system.environment,
      version: payload.system.version,
      projectName: payload.system.project_name,
    },
    dependencies: payload.dependencies.map(toDependency),
    externalServices: payload.external_services.map(toExternalService),
    workers: payload.workers.map(toWorker),
  };
}

function toLatency(payload: PerformanceWire["latencies"][number]): Latency {
  return {
    name: payload.name,
    count: payload.count,
    averageMs: payload.average_ms,
    p50Ms: payload.p50_ms,
    p95Ms: payload.p95_ms,
    p99Ms: payload.p99_ms,
    maxMs: payload.max_ms,
  };
}

/** Map the performance report. */
export function toPerformance(payload: PerformanceWire): Performance {
  return {
    since: payload.since,
    requestsTotal: payload.requests_total,
    requestsByStatus: payload.requests_by_status,
    errorRate: payload.error_rate,
    latencies: payload.latencies.map(toLatency),
    slowestRoutes: payload.slowest_routes.map((entry) => ({
      route: entry.route,
      averageMs: entry.average_ms,
    })),
  };
}

function toQueue(payload: JobsWire["queues"][number]): JobQueue {
  return {
    name: payload.name,
    pending: payload.pending,
    processing: payload.processing,
    depth: payload.depth,
    completed: payload.completed,
    failed: payload.failed,
  };
}

/** Map the background-jobs report. */
export function toJobs(payload: JobsWire): JobsReport {
  return {
    queues: payload.queues.map(toQueue),
    workers: payload.workers.map(toWorker),
    totalDepth: payload.total_depth,
    depthsUnavailable: payload.depths_unavailable,
  };
}

function toTrackedError(payload: ErrorsWire["groups"][number]): TrackedError {
  return {
    fingerprint: payload.fingerprint,
    category: payload.category,
    component: payload.component,
    exceptionType: payload.exception_type,
    location: payload.location,
    operation: payload.operation,
    sampleMessage: payload.sample_message,
    occurrences: payload.occurrences,
    firstSeen: payload.first_seen,
    lastSeen: payload.last_seen,
    statusCode: payload.status_code,
    lastTraceId: payload.last_trace_id,
  };
}

/** Map the error report. */
export function toErrors(payload: ErrorsWire): ErrorsReport {
  return {
    since: payload.since,
    totalErrors: payload.total_errors,
    distinctErrors: payload.distinct_errors,
    errorsByCategory: payload.errors_by_category,
    errorsByComponent: payload.errors_by_component,
    evictedGroups: payload.evicted_groups,
    groups: payload.groups.map(toTrackedError),
  };
}

function toSecurityEvent(payload: SecurityWire["recent"][number]): SecurityEvent {
  return {
    occurredAt: payload.occurred_at,
    event: payload.event,
    severity: payload.severity,
    role: payload.role,
    reason: payload.reason,
    source: payload.source,
    traceId: payload.trace_id,
  };
}

/** Map the security report. */
export function toSecurity(payload: SecurityWire): SecurityReport {
  return {
    since: payload.since,
    totalEvents: payload.total_events,
    eventsByType: payload.events_by_type,
    eventsBySeverity: payload.events_by_severity,
    recentRates: payload.recent_rates,
    distinctSources: payload.distinct_sources,
    sourcesCapped: payload.sources_capped,
    loginAttempts: payload.login_attempts,
    failedLogins: payload.failed_logins,
    loginFailureRate: payload.login_failure_rate,
    recent: payload.recent.map(toSecurityEvent),
  };
}

function toSpan(payload: TracesWire["traces"][number]["spans"][number]): Span {
  return {
    name: payload.name,
    component: payload.component,
    kind: payload.kind,
    spanId: payload.span_id,
    parentSpanId: payload.parent_span_id,
    startedAt: payload.started_at,
    durationMs: payload.duration_ms,
    status: payload.status,
    errorType: payload.error_type,
    errorMessage: payload.error_message,
  };
}

function toTrace(payload: TracesWire["traces"][number]): TracedRequest {
  return {
    traceId: payload.trace_id,
    name: payload.name,
    component: payload.component,
    startedAt: payload.started_at,
    durationMs: payload.duration_ms,
    status: payload.status,
    failed: payload.failed,
    remoteParent: payload.remote_parent,
    droppedSpans: payload.dropped_spans,
    spans: payload.spans.map(toSpan),
  };
}

/** Map the trace report. */
export function toTraces(payload: TracesWire): TracesReport {
  return {
    since: payload.since,
    tracesStarted: payload.traces_started,
    tracesRecorded: payload.traces_recorded,
    spansStarted: payload.spans_started,
    spansDropped: payload.spans_dropped,
    failedTraces: payload.failed_traces,
    traces: payload.traces.map(toTrace),
  };
}

function toAlert(payload: AlertsWire["alerts"][number]): Alert {
  return {
    key: payload.key,
    severity: payload.severity,
    component: payload.component,
    summary: payload.summary,
    firing: payload.firing,
    value: payload.value,
    threshold: payload.threshold,
    detail: payload.detail,
  };
}

/** Map the alert report. */
export function toAlerts(payload: AlertsWire): AlertsReport {
  return {
    evaluatedAt: payload.evaluated_at,
    firing: payload.firing,
    alerts: payload.alerts.map(toAlert),
  };
}

/** Map the aggregate. */
function toOverview(payload: OverviewWire): MonitoringOverview {
  return {
    generatedAt: payload.generated_at,
    state: payload.state,
    health: toHealthReport(payload.health),
    performance: toPerformance(payload.performance),
    jobs: toJobs(payload.jobs),
    errors: toErrors(payload.errors),
    security: toSecurity(payload.security),
    traces: toTraces(payload.traces),
    alerts: toAlerts(payload.alerts),
    unavailable: payload.unavailable,
  };
}

// --------------------------------------------------------------------------- //
// Calls
// --------------------------------------------------------------------------- //

/** Read the platform's operational state in one request. */
export async function fetchMonitoringOverview(
  signal?: AbortSignal,
): Promise<MonitoringOverview> {
  const payload = await apiRequest<unknown>(MONITORING_ENDPOINTS.overview, { signal });
  return toOverview(monitoringOverviewSchema.parse(payload));
}

/** Read dependency, external-service, and worker health on its own. */
export async function fetchMonitoringHealth(signal?: AbortSignal): Promise<HealthReport> {
  const payload = await apiRequest<unknown>(MONITORING_ENDPOINTS.health, { signal });
  return toHealthReport(healthReportSchema.parse(payload));
}

/** Read background queue depths and worker liveness on their own. */
export async function fetchMonitoringJobs(signal?: AbortSignal): Promise<JobsReport> {
  const payload = await apiRequest<unknown>(MONITORING_ENDPOINTS.jobs, { signal });
  return toJobs(jobsSchema.parse(payload));
}

/** Read the evaluated alert conditions on their own. */
export async function fetchMonitoringAlerts(signal?: AbortSignal): Promise<AlertsReport> {
  const payload = await apiRequest<unknown>(MONITORING_ENDPOINTS.alerts, { signal });
  return toAlerts(alertsSchema.parse(payload));
}
