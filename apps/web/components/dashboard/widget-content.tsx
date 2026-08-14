"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { CalendarClock, FileText, MessageSquare } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { NotificationIcon } from "@/components/notifications/notification-icon";
import { share, useMetricFormat } from "@/components/dashboard/format";
import { useDateFormat } from "@/hooks/use-date-format";
import { ROUTES, caseRoute } from "@/lib/routes";
import type { WidgetPayload } from "@/types/dashboard";

/**
 * The nine widget renderers.
 *
 * **Nine, for nineteen widgets, and that ratio is the design.** A widget declares
 * a payload *kind* rather than a bespoke response shape, so `my_cases`,
 * `recent_cases`, and `upcoming_hearings` are the same renderer with different
 * rows — and the twentieth widget the platform grows is free here as long as it
 * declares one of these kinds. The alternative, a component per widget, is the
 * "redesign" `19-dashboard-analytics.md` says adding a widget must not require.
 *
 * Everything here is presentational: no data fetching, no permission checks, no
 * business rules. What may be shown was decided by the server, and by the time a
 * payload reaches one of these it has been authorized, scoped, and validated.
 *
 * **Every list links back to the module that owns the row.** A dashboard card
 * shows an index — a case number, a filename, a title — and the resource itself
 * is opened where it lives, under that module's own authorization. That is why
 * nothing below renders a description, an extracted page, a report section, or a
 * message preview.
 */

export function WidgetContent({ payload }: { payload: WidgetPayload }) {
  switch (payload.kind) {
    case "metrics":
      return <MetricsGrid payload={payload} />;
    case "breakdown":
      return <Breakdown payload={payload} />;
    case "cases":
      return <CaseList payload={payload} />;
    case "documents":
      return <DocumentList payload={payload} />;
    case "reports":
      return <ReportList payload={payload} />;
    case "conversations":
      return <ConversationList payload={payload} />;
    case "activity":
      return <ActivityList payload={payload} />;
    case "notifications":
      return <NotificationList payload={payload} />;
    case "actions":
      // The quick actions render in the page header, where they are reachable
      // from every scroll position — see `quick-actions.tsx`.
      return null;
  }
}

// --------------------------------------------------------------------------- //
// Metrics
// --------------------------------------------------------------------------- //

/**
 * Named figures in a responsive grid.
 *
 * Two columns on a phone and four from `sm` up, so the same widget is legible on
 * the screen sizes the spec asks it to remain usable on without a second layout.
 */
function MetricsGrid({ payload }: { payload: Extract<WidgetPayload, { kind: "metrics" }> }) {
  // A metric key the catalogues do not name resolves through the provider's
  // `getMessageFallback` to a humanized form of itself, so a figure the server
  // started returning is legible here before anyone ships a label for it.
  const t = useTranslations("dashboard.metrics");
  const { formatMetric } = useMetricFormat();

  return (
    <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
      {payload.metrics.map((metric) => (
        <div key={metric.key} className="flex flex-col gap-1">
          <dt className="text-xs text-muted-foreground">{t(metric.key)}</dt>
          <dd className="text-lg font-medium tabular-nums text-foreground">
            {formatMetric(metric)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

// --------------------------------------------------------------------------- //
// Breakdown
// --------------------------------------------------------------------------- //

/**
 * One total split into labelled bars.
 *
 * Bars rather than a pie: a segment's length is comparable at a glance and a
 * pie's angle is not, and the accessible fallback (the number beside every label)
 * is readable either way. The bar is decorative — the figures are in the text, so
 * nothing here depends on colour alone.
 *
 * **Zero-count slices are kept**, not filtered. The server returns every status,
 * including the empty ones, and hiding them would make the chart's shape change
 * as work moved rather than as volumes did.
 */
function Breakdown({
  payload,
}: {
  payload: Extract<WidgetPayload, { kind: "breakdown" }>;
}) {
  const t = useTranslations("dashboard.buckets");
  const { formatNumber } = useMetricFormat();

  return (
    <div className="flex flex-col gap-3">
      <p className="text-2xl font-medium tabular-nums text-foreground">
        {formatNumber(payload.total)}
      </p>
      <ul className="flex flex-col gap-2">
        {payload.buckets.map((bucket) => (
          <li key={bucket.key} className="flex flex-col gap-1">
            <div className="flex items-baseline justify-between gap-2 text-sm">
              <span className="text-muted-foreground">{t(bucket.key)}</span>
              <span className="tabular-nums text-foreground">
                {formatNumber(bucket.count)}
              </span>
            </div>
            <div
              className="h-1.5 overflow-hidden rounded-full bg-muted"
              aria-hidden="true"
            >
              <div
                className="h-full rounded-full bg-primary"
                style={{ width: `${share(bucket.count, payload.total)}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Lists
// --------------------------------------------------------------------------- //

function CaseList({ payload }: { payload: Extract<WidgetPayload, { kind: "cases" }> }) {
  const { formatDate } = useDateFormat();
  // The case module's own status vocabulary rather than the dashboard's, so a
  // badge here and a badge on the cases table can never disagree about a word.
  const tStatus = useTranslations("cases.statuses");
  return (
    <ul className="flex flex-col divide-y divide-border">
      {payload.cases.map((item) => (
        <li key={item.id} className="py-2 first:pt-0 last:pb-0">
          <Link
            href={caseRoute(item.id)}
            className="flex flex-col gap-1 rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            <span className="flex items-center gap-2">
              <span className="truncate text-sm font-medium text-foreground">
                {item.title}
              </span>
              <Badge variant="outline" className="shrink-0">
                {tStatus(item.status)}
              </Badge>
            </span>
            <span className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="tabular-nums">{item.caseNumber}</span>
              {item.nextHearingDate ? (
                <span className="flex items-center gap-1">
                  <CalendarClock className="h-3 w-3" aria-hidden="true" />
                  {formatDate(item.nextHearingDate)}
                </span>
              ) : null}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function DocumentList({
  payload,
}: {
  payload: Extract<WidgetPayload, { kind: "documents" }>;
}) {
  const { formatDate } = useDateFormat();
  const { formatBytes } = useMetricFormat();
  return (
    <ul className="flex flex-col divide-y divide-border">
      {payload.documents.map((item) => (
        <li key={item.id} className="flex items-center gap-2 py-2 first:pt-0 last:pb-0">
          <FileText
            className="h-4 w-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
          <Link
            href={caseRoute(item.caseId)}
            className="flex min-w-0 flex-1 flex-col rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            <span className="truncate text-sm text-foreground">
              {item.originalFilename}
            </span>
            <span className="text-xs text-muted-foreground">
              {formatBytes(item.fileSize)} · {formatDate(item.createdAt)}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function ReportList({
  payload,
}: {
  payload: Extract<WidgetPayload, { kind: "reports" }>;
}) {
  const { formatDateTime } = useDateFormat();
  const t = useTranslations("dashboard");
  const tStatus = useTranslations("reports.statuses");
  return (
    <ul className="flex flex-col divide-y divide-border">
      {payload.reports.map((item) => (
        <li key={item.id} className="py-2 first:pt-0 last:pb-0">
          <Link
            href={ROUTES.reports}
            className="flex flex-col gap-1 rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            <span className="flex items-center gap-2">
              <span className="truncate text-sm text-foreground">{item.title}</span>
              <Badge
                variant={item.status === "failed" ? "destructive" : "outline"}
                className="shrink-0"
              >
                {tStatus(item.status)}
              </Badge>
            </span>
            <span className="text-xs text-muted-foreground">
              {/* Progress, when a run is still writing. The dashboard shows how
                  far it got and never a section — the report itself is read where
                  it lives. */}
              {item.sectionsTotal
                ? `${t("sectionProgress", {
                    completed: item.sectionsCompleted,
                    total: item.sectionsTotal,
                  })} · `
                : ""}
              {formatDateTime(item.createdAt)}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function ConversationList({
  payload,
}: {
  payload: Extract<WidgetPayload, { kind: "conversations" }>;
}) {
  const { formatDateTime } = useDateFormat();
  const t = useTranslations("dashboard");
  return (
    <ul className="flex flex-col divide-y divide-border">
      {payload.conversations.map((item) => (
        <li key={item.id} className="flex items-center gap-2 py-2 first:pt-0 last:pb-0">
          <MessageSquare
            className="h-4 w-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
          <Link
            href={ROUTES.aiAssistant}
            className="flex min-w-0 flex-1 flex-col rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            <span className="truncate text-sm text-foreground">{item.title}</span>
            <span className="text-xs text-muted-foreground">
              {t("messageCount", { count: item.messageCount })} ·{" "}
              {formatDateTime(item.lastMessageAt)}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function ActivityList({
  payload,
}: {
  payload: Extract<WidgetPayload, { kind: "activity" }>;
}) {
  const { formatDateTime } = useDateFormat();
  return (
    <ul className="flex flex-col divide-y divide-border">
      {payload.activity.map((item) => (
        <li key={item.id} className="py-2 first:pt-0 last:pb-0">
          <Link
            href={caseRoute(item.caseId)}
            className="flex flex-col gap-0.5 rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            <span className="truncate text-sm text-foreground">{item.title}</span>
            <span className="text-xs text-muted-foreground">
              {item.actorName ? `${item.actorName} · ` : ""}
              {formatDateTime(item.createdAt)}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function NotificationList({
  payload,
}: {
  payload: Extract<WidgetPayload, { kind: "notifications" }>;
}) {
  const { formatDateTime } = useDateFormat();
  return (
    <ul className="flex flex-col divide-y divide-border">
      {payload.notifications.map((item) => (
        <li key={item.id} className="flex items-start gap-2 py-2 first:pt-0 last:pb-0">
          {/* Reuses the notification module's own icon component, so an alert
              looks the same on the dashboard as it does in the bell. */}
          <NotificationIcon
            category={item.category}
            notificationType={item.notificationType}
            className="mt-0.5 h-7 w-7"
          />
          <Link
            href={ROUTES.notifications}
            className="flex min-w-0 flex-1 flex-col rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            <span
              className={
                item.isRead
                  ? "truncate text-sm text-muted-foreground"
                  : "truncate text-sm font-medium text-foreground"
              }
            >
              {item.title}
            </span>
            <span className="text-xs text-muted-foreground">
              {formatDateTime(item.createdAt)}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
