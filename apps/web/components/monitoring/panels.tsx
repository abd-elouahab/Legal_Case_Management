"use client";

import {
  Activity,
  AlertTriangle,
  Bug,
  Database,
  Layers,
  ListTree,
  Server,
  ShieldAlert,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HealthBadge, SeverityBadge } from "@/components/monitoring/state-badge";
import {
  EMPTY,
  formatCount,
  formatDuration,
  formatPercent,
  humanize,
} from "@/components/monitoring/format";
import { useDateFormat } from "@/hooks/use-date-format";
import type {
  AlertsReport,
  ErrorsReport,
  HealthReport,
  JobsReport,
  Performance,
  SecurityReport,
  TracesReport,
} from "@/types/monitoring";

/**
 * The monitoring page's panels.
 *
 * One file rather than eight, deliberately: each of these is a table or a strip
 * of figures over a report the page has already fetched, none of them holds state
 * or fetches anything, and splitting them would produce eight files whose entire
 * content is a heading and a `map`. The page composes them; they compose nothing.
 *
 * **Every panel is a read and every label is a translation key.** Nothing here
 * takes an action, so there is no button that changes anything on this page —
 * which is what makes an operational view safe to hand somebody during an
 * incident.
 */

// --------------------------------------------------------------------------- //
// Shared pieces
// --------------------------------------------------------------------------- //

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-lg font-medium tabular-nums text-foreground">{value}</span>
    </div>
  );
}

function Panel({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon: typeof Activity;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Icon className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">{children}</CardContent>
    </Card>
  );
}

function EmptyRow({ message }: { message: string }) {
  return <p className="text-sm text-muted-foreground">{message}</p>;
}

// --------------------------------------------------------------------------- //
// Health
// --------------------------------------------------------------------------- //

export function HealthPanel({ report }: { report: HealthReport }) {
  const t = useTranslations("monitoring.health");
  const tCommon = useTranslations("monitoring");

  return (
    <Panel title={t("title")} icon={Server}>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label={t("uptime")} value={report.system.uptime} />
        <Stat label={t("environment")} value={report.system.environment} />
        <Stat label={t("version")} value={report.system.version} />
        <Stat label={t("threads")} value={formatCount(report.system.threadCount)} />
      </div>

      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {t("dependencies")}
        </h3>
        <ul className="flex flex-col gap-2">
          {report.dependencies.map((dependency) => (
            <li
              key={dependency.name}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2"
            >
              <span className="flex items-center gap-2 text-sm text-foreground">
                <Database className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                {humanize(dependency.name)}
                {dependency.required ? (
                  <span className="text-xs text-muted-foreground">({t("required")})</span>
                ) : null}
              </span>
              <HealthBadge state={dependency.state} />
            </li>
          ))}
        </ul>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {t("externalServices")}
        </h3>
        <ul className="flex flex-col gap-2">
          {report.externalServices.map((service) => (
            <li
              key={service.name}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2"
            >
              <span className="flex flex-col">
                <span className="text-sm text-foreground">{humanize(service.name)}</span>
                {service.detail ? (
                  <span className="text-xs text-muted-foreground">{service.detail}</span>
                ) : null}
              </span>
              <HealthBadge state={service.state} />
            </li>
          ))}
        </ul>
      </section>

      <p className="text-xs text-muted-foreground">{tCommon("externalNote")}</p>
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// Alerts
// --------------------------------------------------------------------------- //

export function AlertsPanel({ report }: { report: AlertsReport }) {
  const t = useTranslations("monitoring.alerts");
  const tRules = useTranslations("monitoring.rules");
  const firing = report.alerts.filter((alert) => alert.firing);

  return (
    <Panel title={t("title")} icon={AlertTriangle}>
      {firing.length === 0 ? (
        <EmptyRow message={t("none")} />
      ) : (
        <ul className="flex flex-col gap-2">
          {firing.map((alert) => (
            <li
              key={alert.key}
              className="flex flex-col gap-1 rounded-md border border-border px-3 py-2"
            >
              <span className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-medium text-foreground">
                  {tRules.has(alert.key) ? tRules(alert.key) : alert.summary}
                </span>
                <SeverityBadge severity={alert.severity} />
              </span>
              <span className="text-xs text-muted-foreground">
                {alert.value !== null
                  ? t("measured", {
                      value: alert.value.toLocaleString(),
                      threshold: alert.threshold?.toLocaleString() ?? EMPTY,
                    })
                  : (alert.detail ?? "")}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-muted-foreground">{t("deliveryNote")}</p>
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// Performance
// --------------------------------------------------------------------------- //

export function PerformancePanel({ report }: { report: Performance }) {
  const t = useTranslations("monitoring.performance");
  const tLatency = useTranslations("monitoring.latencies");

  return (
    <Panel title={t("title")} icon={Activity}>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label={t("requests")} value={formatCount(report.requestsTotal)} />
        <Stat label={t("errorRate")} value={formatPercent(report.errorRate)} />
        <Stat
          label={t("clientErrors")}
          value={formatCount(report.requestsByStatus["4xx"] ?? 0)}
        />
        <Stat
          label={t("serverErrors")}
          value={formatCount(report.requestsByStatus["5xx"] ?? 0)}
        />
      </div>

      {report.latencies.length === 0 ? (
        <EmptyRow message={t("noObservations")} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-start text-xs uppercase tracking-wide text-muted-foreground">
                <th className="py-2 text-start font-medium">{t("measurement")}</th>
                <th className="py-2 text-end font-medium">{t("count")}</th>
                <th className="py-2 text-end font-medium">{t("average")}</th>
                <th className="py-2 text-end font-medium">p95</th>
                <th className="py-2 text-end font-medium">{t("max")}</th>
              </tr>
            </thead>
            <tbody>
              {report.latencies.map((latency) => (
                <tr key={latency.name} className="border-t border-border">
                  <td className="py-2 text-start">
                    {tLatency.has(latency.name) ? tLatency(latency.name) : humanize(latency.name)}
                  </td>
                  <td className="py-2 text-end tabular-nums">{formatCount(latency.count)}</td>
                  <td className="py-2 text-end tabular-nums">
                    {formatDuration(latency.averageMs)}
                  </td>
                  <td className="py-2 text-end tabular-nums">{formatDuration(latency.p95Ms)}</td>
                  <td className="py-2 text-end tabular-nums">{formatDuration(latency.maxMs)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {report.slowestRoutes.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("slowestRoutes")}
          </h3>
          <ul className="flex flex-col gap-1">
            {report.slowestRoutes.map((entry) => (
              <li
                key={entry.route}
                className="flex items-center justify-between gap-4 text-sm"
              >
                <code className="truncate text-xs text-muted-foreground">{entry.route}</code>
                <span className="tabular-nums">{formatDuration(entry.averageMs)}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// Background jobs
// --------------------------------------------------------------------------- //

export function JobsPanel({ report }: { report: JobsReport }) {
  const t = useTranslations("monitoring.jobs");
  const tQueues = useTranslations("monitoring.queues");

  return (
    <Panel title={t("title")} icon={Layers}>
      {report.depthsUnavailable ? (
        <EmptyRow message={t("depthsUnavailable")} />
      ) : report.queues.length === 0 ? (
        <EmptyRow message={t("none")} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wide text-muted-foreground">
                <th className="py-2 text-start font-medium">{t("queue")}</th>
                <th className="py-2 text-end font-medium">{t("pending")}</th>
                <th className="py-2 text-end font-medium">{t("processing")}</th>
                <th className="py-2 text-end font-medium">{t("failed")}</th>
              </tr>
            </thead>
            <tbody>
              {report.queues.map((queue) => (
                <tr key={queue.name} className="border-t border-border">
                  <td className="py-2 text-start">
                    {tQueues.has(queue.name) ? tQueues(queue.name) : humanize(queue.name)}
                  </td>
                  <td className="py-2 text-end tabular-nums">{formatCount(queue.pending)}</td>
                  <td className="py-2 text-end tabular-nums">{formatCount(queue.processing)}</td>
                  <td className="py-2 text-end tabular-nums">{formatCount(queue.failed)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {t("workers")}
        </h3>
        <ul className="flex flex-wrap gap-2">
          {report.workers.map((worker) => (
            <li
              key={worker.name}
              className="flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm"
            >
              <span>{tQueues.has(worker.name) ? tQueues(worker.name) : humanize(worker.name)}</span>
              <HealthBadge state={worker.state} />
            </li>
          ))}
        </ul>
      </section>
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// Errors
// --------------------------------------------------------------------------- //

export function ErrorsPanel({ report }: { report: ErrorsReport }) {
  const t = useTranslations("monitoring.errors");
  const { formatDateTime } = useDateFormat();

  return (
    <Panel title={t("title")} icon={Bug}>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <Stat label={t("total")} value={formatCount(report.totalErrors)} />
        <Stat label={t("distinct")} value={formatCount(report.distinctErrors)} />
        <Stat label={t("evicted")} value={formatCount(report.evictedGroups)} />
      </div>

      {report.groups.length === 0 ? (
        <EmptyRow message={t("none")} />
      ) : (
        <ul className="flex flex-col gap-2">
          {report.groups.map((group) => (
            <li
              key={group.fingerprint}
              className="flex flex-col gap-1 rounded-md border border-border px-3 py-2"
            >
              <span className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-sm font-medium text-foreground">
                  {group.exceptionType}
                  {group.location ? (
                    <code className="ms-2 text-xs text-muted-foreground">{group.location}</code>
                  ) : null}
                </span>
                <span className="text-xs tabular-nums text-muted-foreground">
                  {t("occurrences", { count: group.occurrences })}
                </span>
              </span>
              {group.sampleMessage ? (
                <span className="truncate text-xs text-muted-foreground">
                  {group.sampleMessage}
                </span>
              ) : null}
              <span className="text-xs text-muted-foreground">
                {humanize(group.category)} · {humanize(group.component)} ·{" "}
                {formatDateTime(group.lastSeen)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// Security
// --------------------------------------------------------------------------- //

export function SecurityPanel({ report }: { report: SecurityReport }) {
  const t = useTranslations("monitoring.security");
  const tEvents = useTranslations("monitoring.events");
  const { formatDateTime } = useDateFormat();

  return (
    <Panel title={t("title")} icon={ShieldAlert}>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label={t("attempts")} value={formatCount(report.loginAttempts)} />
        <Stat label={t("failures")} value={formatCount(report.failedLogins)} />
        <Stat label={t("failureRate")} value={formatPercent(report.loginFailureRate)} />
        <Stat
          label={t("sources")}
          value={`${report.sourcesCapped ? "≥ " : ""}${formatCount(report.distinctSources)}`}
        />
      </div>

      {report.recent.length === 0 ? (
        <EmptyRow message={t("none")} />
      ) : (
        <ul className="flex flex-col gap-1">
          {report.recent.slice(0, 12).map((event, index) => (
            <li
              key={`${event.occurredAt}-${index}`}
              className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-1.5 text-sm last:border-b-0"
            >
              <span className="flex items-center gap-2">
                <SeverityBadge severity={event.severity} />
                <span>
                  {tEvents.has(event.event) ? tEvents(event.event) : humanize(event.event)}
                </span>
                {event.source ? (
                  <code className="text-xs text-muted-foreground">{event.source}</code>
                ) : null}
              </span>
              <span className="text-xs text-muted-foreground">
                {formatDateTime(event.occurredAt)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <p className="text-xs text-muted-foreground">{t("privacyNote")}</p>
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// Traces
// --------------------------------------------------------------------------- //

export function TracesPanel({ report }: { report: TracesReport }) {
  const t = useTranslations("monitoring.traces");

  return (
    <Panel title={t("title")} icon={ListTree}>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label={t("recorded")} value={formatCount(report.tracesRecorded)} />
        <Stat label={t("spans")} value={formatCount(report.spansStarted)} />
        <Stat label={t("failed")} value={formatCount(report.failedTraces)} />
        <Stat label={t("dropped")} value={formatCount(report.spansDropped)} />
      </div>

      {report.traces.length === 0 ? (
        <EmptyRow message={t("none")} />
      ) : (
        <ul className="flex flex-col gap-2">
          {report.traces.slice(0, 8).map((trace) => (
            <li
              key={trace.traceId}
              className="flex flex-col gap-1 rounded-md border border-border px-3 py-2"
            >
              <span className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="truncate text-sm text-foreground">{trace.name}</span>
                <span className="text-xs tabular-nums text-muted-foreground">
                  {formatDuration(trace.durationMs)}
                </span>
              </span>
              <span className="text-xs text-muted-foreground">
                {t("spanSummary", { count: trace.spans.length })}
                {trace.failed ? ` · ${t("failedTrace")}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
