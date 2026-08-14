"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { FileText, Sparkles } from "lucide-react";

import { Protected } from "@/components/auth/protected";
import { DeleteReportDialog } from "@/components/reports/delete-report-dialog";
import { GenerateReportDialog } from "@/components/reports/generate-report-dialog";
import { ReportDetailDialog } from "@/components/reports/report-detail-dialog";
import { ReportFilters } from "@/components/reports/report-filters";
import { ReportPagination } from "@/components/reports/report-pagination";
import { ReportTable } from "@/components/reports/report-table";
import { ReportTableSkeleton } from "@/components/reports/report-table-skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";
import { useReportErrorMessage, useReports } from "@/hooks/use-reports";
import { PERMISSION } from "@/types/authorization";
import type { Report } from "@/types/report";
import {
  DEFAULT_REPORT_LIST_QUERY,
  type ReportListQuery,
  type ReportSortField,
} from "@/types/report-management";

/**
 * The report history, with its filters, its table, and its dialogs.
 *
 * Used twice: on `/reports`, where it lists everything the caller has generated,
 * and inside a case workspace pinned to one matter (`caseId`) — where the case
 * column disappears, *Generate* pre-selects the case, and *Clear filters* does
 * **not** widen the list back to the whole platform. That is the same rule the
 * embedded document list and case search follow.
 *
 * **The list is the caller's own.** The API scopes it by requester, so there is
 * nothing to filter by here and nothing to explain: a lawyer's history contains
 * the reports they generated and the totals count only those.
 *
 * Three empty states, and none of them is an error: "no reports yet" (offering
 * *Generate*), "no results" (offering *Clear filters*), and the error state,
 * which is a fourth thing entirely.
 *
 * **Which report is open is component state, not a route.** A report identifier
 * in the URL would be written to the browser's history and to the `Referer`
 * header of anything the page loads next — see
 * {@link ReportDetailDialog} for the full reasoning.
 */
export function ReportList({
  caseId,
  title,
  description,
}: {
  /** Pins the list, the Generate dialog, and the reset to one case. */
  caseId?: string;
  /** Already translated by the caller; falls back to the page's own heading. */
  title?: string;
  description?: string;
}) {
  const t = useTranslations("reports");
  const tActions = useTranslations("common.actions");
  const errorMessage = useReportErrorMessage();

  const base = React.useMemo<ReportListQuery>(
    () => ({ ...DEFAULT_REPORT_LIST_QUERY, caseId: caseId ?? null }),
    [caseId],
  );

  const [query, setQuery] = React.useState<ReportListQuery>(base);
  const [generating, setGenerating] = React.useState(false);
  const [openReportId, setOpenReportId] = React.useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = React.useState<Report | null>(null);

  // Re-pin when the case changes — adjusted during render rather than in an
  // effect (https://react.dev/learn/you-might-not-need-an-effect).
  const [lastBase, setLastBase] = React.useState(base);
  if (base !== lastBase) {
    setLastBase(base);
    setQuery(base);
  }

  const reports = useReports(query);

  // Every filter change resets to the first page: filtering while on page 4
  // would otherwise show an empty result the user cannot explain.
  function patch(next: Partial<ReportListQuery>) {
    setQuery((current) => ({ ...current, ...next, page: 1 }));
  }

  function toggleSort(field: ReportSortField) {
    setQuery((current) => ({
      ...current,
      page: 1,
      sortBy: field,
      sortOrder:
        current.sortBy === field && current.sortOrder === "desc" ? "asc" : "desc",
    }));
  }

  const page = reports.data;
  const hasFilters =
    Boolean(query.search) || query.status !== null || query.reportType !== null;

  return (
    <section className="flex flex-col gap-4" aria-labelledby="report-list-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 id="report-list-heading" className="text-lg font-semibold text-foreground">
            {title ?? t("caseSection.title")}
          </h2>
          {description ? (
            <p className="text-sm text-muted-foreground">{description}</p>
          ) : null}
        </div>

        <Protected allOf={[PERMISSION.reportsGenerate, PERMISSION.aiGenerateReport]}>
          <Button type="button" onClick={() => setGenerating(true)}>
            <Sparkles className="h-4 w-4" aria-hidden="true" />
            {t("generateDialog.title")}
          </Button>
        </Protected>
      </div>

      <ReportFilters
        query={query}
        onChange={patch}
        onReset={() => setQuery(base)}
        disabled={reports.isLoading}
      />

      {reports.isLoading ? (
        <ReportTableSkeleton showCase={!caseId} />
      ) : reports.isError ? (
        <ErrorState
          title={t("errors.listTitle")}
          description={errorMessage(reports.error)}
          onRetry={() => void reports.refetch()}
        />
      ) : page && page.items.length > 0 ? (
        <>
          <ReportTable
            reports={page.items}
            sortBy={query.sortBy}
            sortOrder={query.sortOrder}
            onToggleSort={toggleSort}
            onView={(report) => setOpenReportId(report.id)}
            onDelete={setPendingDelete}
            showCase={!caseId}
          />
          <ReportPagination
            page={page.page}
            pageSize={page.pageSize}
            totalPages={page.totalPages}
            totalRecords={page.totalRecords}
            onPageChange={(next) => setQuery((current) => ({ ...current, page: next }))}
            disabled={reports.isFetching}
          />
        </>
      ) : hasFilters ? (
        <EmptyState
          icon={FileText}
          titleKey="reports.empty.filteredTitle"
          descriptionKey="reports.empty.filteredDescription"
          action={
            <Button type="button" variant="outline" onClick={() => setQuery(base)}>
              {tActions("clearFilters")}
            </Button>
          }
        />
      ) : (
        <EmptyState
          icon={FileText}
          titleKey="reports.empty.title"
          descriptionKey="reports.empty.description"
          action={
            <Protected allOf={[PERMISSION.reportsGenerate, PERMISSION.aiGenerateReport]}>
              <Button type="button" onClick={() => setGenerating(true)}>
                <Sparkles className="h-4 w-4" aria-hidden="true" />
                {t("generateDialog.title")}
              </Button>
            </Protected>
          }
        />
      )}

      <GenerateReportDialog
        open={generating}
        onOpenChange={setGenerating}
        caseId={caseId}
        // Opened straight away, so the user watches the progress bar move rather
        // than being left on a list wondering whether the button worked.
        onGenerated={setOpenReportId}
      />

      <ReportDetailDialog
        reportId={openReportId}
        open={openReportId !== null}
        onOpenChange={(open) => {
          if (!open) setOpenReportId(null);
        }}
      />

      <DeleteReportDialog
        report={pendingDelete}
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        onDeleted={() => setPendingDelete(null)}
      />
    </section>
  );
}
