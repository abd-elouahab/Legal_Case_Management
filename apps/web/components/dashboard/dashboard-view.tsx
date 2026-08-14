"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { TriangleAlert } from "lucide-react";

import { ErrorState } from "@/components/shared/error-state";
import { DashboardFilters } from "@/components/dashboard/dashboard-filters";
import { QuickActions } from "@/components/dashboard/quick-actions";
import { WidgetCard, WidgetCardSkeleton } from "@/components/dashboard/widget-card";
import { WidgetContent } from "@/components/dashboard/widget-content";
import {
  useDashboard,
  useDashboardErrorMessage,
  useDashboardRealtime,
  useRefreshWidget,
} from "@/hooks/use-dashboard";
import { cn } from "@/lib/utils";
import {
  DEFAULT_DASHBOARD_QUERY,
  WIDGET_GROUPS,
  type DashboardQuery,
  type DashboardWidget,
  type WidgetGroup,
} from "@/types/dashboard";

/**
 * The dashboard.
 *
 * A **Client Component** because everything on it is interactive: the time
 * filter, the per-widget refresh, and the live updates. The page around it stays
 * a Server Component, per the standard that pages remain lightweight.
 *
 * **It renders whatever the server sent, in the order it sent it.** There is no
 * layout table here, no role branch, and no list of widgets — the API returns the
 * caller's own layout, already filtered to what they may see, and this walks it.
 * That is what makes a widget added on the server appear here without a change,
 * and it is why a court representative's dashboard and an administrator's are the
 * same component.
 *
 * Three things it does own, and each is a spec requirement about *presentation*:
 *
 * * **Loading placeholders** — cards, not a spinner, so the page does not reflow
 *   when data lands;
 * * **Partial failure** — one banner when some widgets could not be produced, and
 *   each of those cards explains itself and offers a retry. The page is never an
 *   error page because of a widget;
 * * **Responsive layout** — one column on a phone, two from `md`, three from
 *   `xl`. A widget that wants the full width says so below rather than every card
 *   guessing.
 */

/** Widgets that read better across the grid than inside one column. */
const WIDE_WIDGETS = new Set<string>([
  "case_analytics",
  "document_analytics",
  "ai_analytics",
  "hearing_calendar",
  "storage_usage",
  "active_users",
  "processing_queues",
]);

/**
 * Which widgets have a sentence of their own for "loaded, and empty".
 *
 * A set rather than a map of sentences: the words are `dashboard.empty.<key>` in
 * the catalogues, and a widget not listed here falls back to the shared
 * `common.states.emptyTitle` inside the card. Keeping the *membership* here rather
 * than inferring it from the catalogue means a translator cannot change which
 * cards have bespoke copy by leaving a key out.
 */
const WIDGETS_WITH_EMPTY_COPY = new Set<string>([
  "notifications",
  "recent_activity",
  "my_cases",
  "recent_cases",
  "case_status_overview",
  "upcoming_hearings",
  "recent_documents",
  "ocr_status",
  "ai_reports",
  "recent_conversations",
  "timeline_activity",
]);

export function DashboardView() {
  const [query, setQuery] = React.useState<DashboardQuery>(DEFAULT_DASHBOARD_QUERY);
  const { data, isLoading, isError, error, refetch } = useDashboard(query);
  const refreshWidget = useRefreshWidget(query);
  const t = useTranslations("dashboard");
  const tEmpty = useTranslations("dashboard.empty");
  const errorMessage = useDashboardErrorMessage();

  // Live updates. Each widget carries the events that make it stale, so nothing
  // in this app decides what a `case.updated` means for a dashboard.
  useDashboardRealtime(data, query);

  if (isError) {
    return (
      <ErrorState
        title={t("errors.title")}
        description={errorMessage(error)}
        onRetry={() => void refetch()}
      />
    );
  }

  const widgets = data?.widgets ?? [];
  const grouped = groupWidgets(widgets);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <DashboardFilters query={query} onChange={setQuery} disabled={isLoading} />
        {data ? <QuickActions actions={data.quickActions} /> : null}
      </div>

      {data && data.failedWidgets > 0 ? (
        <p
          role="status"
          className="flex items-start gap-2 rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground"
        >
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          {/* An ICU plural rather than a ternary: English has two forms, French
              agrees on one boundary and not the other, and Arabic has six. A
              conditional here would encode English grammar in a component. */}
          <span>{t("partialFailure", { count: data.failedWidgets })}</span>
        </p>
      ) : null}

      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, index) => (
            <WidgetCardSkeleton key={index} />
          ))}
        </div>
      ) : (
        WIDGET_GROUPS.filter((group) => (grouped.get(group)?.length ?? 0) > 0).map(
          (group) => (
            <section key={group} className="flex flex-col gap-3">
              <h2 className="text-sm font-medium text-muted-foreground">
                {t(`groups.${group}`)}
              </h2>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                {(grouped.get(group) ?? []).map((widget) => (
                  <WidgetCard
                    key={widget.widget.key}
                    widget={widget}
                    onRefresh={refreshWidget}
                    emptyMessage={
                      WIDGETS_WITH_EMPTY_COPY.has(widget.widget.key)
                        ? tEmpty(widget.widget.key)
                        : undefined
                    }
                    className={cn(
                      WIDE_WIDGETS.has(widget.widget.key) && "md:col-span-2 xl:col-span-3",
                    )}
                  >
                    {widget.data ? <WidgetContent payload={widget.data} /> : null}
                  </WidgetCard>
                ))}
              </div>
            </section>
          ),
        )
      )}
    </div>
  );
}

/**
 * Bucket the server's widget list by group, **preserving its order**.
 *
 * The order inside a group is the role layout's, which is a deliberate statement
 * about what that role comes here for. Sorting it again here would replace the
 * server's opinion with an arbitrary one.
 *
 * The quick-actions widget is dropped: its shortcuts render in the header, where
 * they stay reachable from any scroll position, so a card carrying nothing would
 * be an empty box at the top of every dashboard.
 */
function groupWidgets(
  widgets: readonly DashboardWidget[],
): Map<WidgetGroup, DashboardWidget[]> {
  const grouped = new Map<WidgetGroup, DashboardWidget[]>();

  for (const widget of widgets) {
    if (widget.widget.kind === "actions") continue;
    const bucket = grouped.get(widget.widget.group);
    if (bucket) bucket.push(widget);
    else grouped.set(widget.widget.group, [widget]);
  }

  return grouped;
}
