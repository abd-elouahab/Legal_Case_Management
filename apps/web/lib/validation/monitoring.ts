/**
 * Zod schemas for the monitoring API.
 *
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/monitoring.py`; where they must agree, the API is the
 * authority.
 *
 * **Almost everything here is deliberately loose about vocabularies**, and the
 * reason is specific to this feature. A component, an error category, a security
 * event type, a metric name, and an alert key are all **open sets on the
 * server**: `22-monitoring.md` asks the platform to *"support future metrics
 * without redesign"*, and a strict enum in this file would turn "the backend
 * added a component" into a monitoring page that will not load — which is
 * precisely the page somebody needs when a backend has just changed.
 *
 * `health_state` and the two severities are the exception and are strict, for the
 * opposite reason: this app draws a *colour* from each of them, so an
 * unrecognised value is a genuine contract break rather than a newer backend
 * being ahead of this build. That is the same distinction
 * `lib/validation/dashboard.ts` draws between a widget key and an event name.
 */

import { z } from "zod";

import { HEALTH_STATES } from "@/types/monitoring";

const healthStateSchema = z.enum(HEALTH_STATES);
const severitySchema = z.enum(["info", "warning", "critical"]);

const systemSchema = z.object({
  started_at: z.string(),
  uptime_seconds: z.number(),
  uptime: z.string(),
  process_id: z.number(),
  thread_count: z.number(),
  python_version: z.string(),
  platform: z.string(),
  environment: z.string(),
  version: z.string(),
  project_name: z.string(),
});

const dependencySchema = z.object({
  name: z.string(),
  state: healthStateSchema,
  required: z.boolean(),
  detail: z.string().nullable().default(null),
});

const externalServiceSchema = z.object({
  name: z.string(),
  enabled: z.boolean(),
  configured: z.boolean(),
  state: healthStateSchema,
  detail: z.string().nullable().default(null),
});

const workerSchema = z.object({
  name: z.string(),
  running: z.boolean(),
  concurrency: z.number(),
  state: healthStateSchema,
});

export const healthReportSchema = z.object({
  state: healthStateSchema,
  checked_at: z.string(),
  system: systemSchema,
  dependencies: z.array(dependencySchema),
  external_services: z.array(externalServiceSchema),
  workers: z.array(workerSchema),
});

const latencySchema = z.object({
  name: z.string(),
  count: z.number(),
  average_ms: z.number().nullable().default(null),
  p50_ms: z.number().nullable().default(null),
  p95_ms: z.number().nullable().default(null),
  p99_ms: z.number().nullable().default(null),
  max_ms: z.number().nullable().default(null),
});

export const performanceSchema = z.object({
  since: z.string(),
  requests_total: z.number(),
  requests_by_status: z.record(z.string(), z.number()),
  error_rate: z.number(),
  latencies: z.array(latencySchema),
  slowest_routes: z.array(z.object({ route: z.string(), average_ms: z.number() })),
});

const queueSchema = z.object({
  name: z.string(),
  pending: z.number(),
  processing: z.number(),
  depth: z.number(),
  completed: z.number().nullable().default(null),
  failed: z.number().nullable().default(null),
});

export const jobsSchema = z.object({
  queues: z.array(queueSchema),
  workers: z.array(workerSchema),
  total_depth: z.number(),
  depths_unavailable: z.boolean(),
});

const trackedErrorSchema = z.object({
  fingerprint: z.string(),
  category: z.string(),
  component: z.string(),
  exception_type: z.string(),
  location: z.string().nullable().default(null),
  operation: z.string().nullable().default(null),
  sample_message: z.string().nullable().default(null),
  occurrences: z.number(),
  first_seen: z.string(),
  last_seen: z.string(),
  status_code: z.number().nullable().default(null),
  last_trace_id: z.string().nullable().default(null),
});

export const errorsSchema = z.object({
  since: z.string(),
  total_errors: z.number(),
  distinct_errors: z.number(),
  errors_by_category: z.record(z.string(), z.number()),
  errors_by_component: z.record(z.string(), z.number()),
  evicted_groups: z.number(),
  groups: z.array(trackedErrorSchema),
});

const securityEventSchema = z.object({
  occurred_at: z.string(),
  event: z.string(),
  severity: severitySchema,
  role: z.string().nullable().default(null),
  reason: z.string().nullable().default(null),
  source: z.string().nullable().default(null),
  trace_id: z.string().nullable().default(null),
});

export const securitySchema = z.object({
  since: z.string(),
  total_events: z.number(),
  events_by_type: z.record(z.string(), z.number()),
  events_by_severity: z.record(z.string(), z.number()),
  recent_rates: z.record(z.string(), z.record(z.string(), z.number())),
  distinct_sources: z.number(),
  sources_capped: z.boolean(),
  login_attempts: z.number(),
  failed_logins: z.number(),
  login_failure_rate: z.number(),
  recent: z.array(securityEventSchema),
});

const spanSchema = z.object({
  name: z.string(),
  component: z.string(),
  kind: z.string(),
  span_id: z.string(),
  parent_span_id: z.string().nullable().default(null),
  started_at: z.string(),
  duration_ms: z.number(),
  status: z.string(),
  error_type: z.string().nullable().default(null),
  error_message: z.string().nullable().default(null),
});

const tracedRequestSchema = z.object({
  trace_id: z.string(),
  name: z.string(),
  component: z.string(),
  started_at: z.string(),
  duration_ms: z.number(),
  status: z.string(),
  failed: z.boolean(),
  remote_parent: z.boolean(),
  dropped_spans: z.number(),
  spans: z.array(spanSchema),
});

export const tracesSchema = z.object({
  since: z.string(),
  traces_started: z.number(),
  traces_recorded: z.number(),
  spans_started: z.number(),
  spans_dropped: z.number(),
  failed_traces: z.number(),
  traces: z.array(tracedRequestSchema),
});

const alertSchema = z.object({
  key: z.string(),
  severity: severitySchema,
  component: z.string(),
  summary: z.string(),
  firing: z.boolean(),
  value: z.number().nullable().default(null),
  threshold: z.number().nullable().default(null),
  detail: z.string().nullable().default(null),
});

export const alertsSchema = z.object({
  evaluated_at: z.string(),
  firing: z.number(),
  alerts: z.array(alertSchema),
});

export const monitoringOverviewSchema = z.object({
  generated_at: z.string(),
  state: healthStateSchema,
  health: healthReportSchema,
  performance: performanceSchema,
  jobs: jobsSchema,
  errors: errorsSchema,
  security: securitySchema,
  traces: tracesSchema,
  alerts: alertsSchema,
  unavailable: z.array(z.string()).default([]),
});
