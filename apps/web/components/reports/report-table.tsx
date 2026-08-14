"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { ArrowDown, ArrowUp, ChevronsUpDown, FileText } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ReportProgress } from "@/components/reports/report-progress";
import { ReportRowActions } from "@/components/reports/report-row-actions";
import { ReportStatusBadge } from "@/components/reports/report-status-badge";
import { useDateFormat } from "@/hooks/use-date-format";
import { caseRoute } from "@/lib/routes";
import { cn } from "@/lib/utils";
import type { Report } from "@/types/report";
import type { ReportSortField, SortOrder } from "@/types/report-management";

/**
 * The report history table.
 *
 * Columns are what a report *is* and where its run got to: title and type,
 * status, progress, sources, generated date, and actions — plus the case, which
 * the global list needs and the case-scoped list hides (`showCase={false}`),
 * because repeating one case number down every row is noise.
 *
 * **The progress bar lives in the status column**, not in a column of its own. A
 * report is either running or it is not, and a permanently empty column for the
 * ninety percent of rows that have finished would be a column of blanks.
 *
 * **A failed row says why in the table**, rather than only inside the report. A
 * history where three rows read "Failed" and nothing more is a history that makes
 * a user open three reports to learn they all failed for the same reason.
 *
 * Sortable columns are `<button>`s inside the header cell rather than click
 * handlers on the cell itself, so they are reachable and operable by keyboard,
 * and each carries `aria-sort` so a screen reader announces the current order.
 *
 * On small screens the table scrolls horizontally inside its own container, and
 * the columns hide progressively — the sources and date below `xl`, the case
 * below `lg` — so a phone still shows what identifies a report.
 */

/** A sortable column heading, translating its own label from the field. */
function SortButton({
  field,
  sortBy,
  sortOrder,
  onToggle,
}: {
  field: ReportSortField;
  sortBy: ReportSortField;
  sortOrder: SortOrder;
  onToggle: (field: ReportSortField) => void;
}) {
  const t = useTranslations("reports.table");
  const tSort = useTranslations("common.sort");

  const isActive = sortBy === field;
  const Icon = !isActive ? ChevronsUpDown : sortOrder === "asc" ? ArrowUp : ArrowDown;
  const label = t(`columns.${field}`);

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => onToggle(field)}
      className={cn(
        "-ms-2 h-8 gap-1 px-2 font-medium",
        isActive ? "text-foreground" : "text-muted-foreground",
      )}
    >
      {label}
      <Icon className="h-4 w-4" aria-hidden="true" />
      <span className="sr-only">
        {isActive
          ? tSort(sortOrder === "asc" ? "sortedAscending" : "sortedDescending")
          : tSort("sortBy", { column: label })}
      </span>
    </Button>
  );
}

function ariaSort(
  field: ReportSortField,
  sortBy: ReportSortField,
  sortOrder: SortOrder,
): "ascending" | "descending" | "none" {
  if (sortBy !== field) return "none";
  return sortOrder === "asc" ? "ascending" : "descending";
}

export function ReportTable({
  reports,
  sortBy,
  sortOrder,
  onToggleSort,
  onView,
  onDelete,
  showCase = true,
}: {
  reports: Report[];
  sortBy: ReportSortField;
  sortOrder: SortOrder;
  onToggleSort: (field: ReportSortField) => void;
  onView: (report: Report) => void;
  onDelete: (report: Report) => void;
  /** Hidden inside a case workspace, where every row is that case. */
  showCase?: boolean;
}) {
  const { formatDateTime } = useDateFormat();
  const t = useTranslations("reports.table");
  const tTypes = useTranslations("reports.types");
  const tFailures = useTranslations("reports.failures");
  const tActions = useTranslations("common.actions");

  return (
    <div className="w-full overflow-x-auto rounded-lg border border-border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead aria-sort={ariaSort("title", sortBy, sortOrder)}>
              <SortButton
                field="title"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            {showCase ? (
              <TableHead className="hidden lg:table-cell">{t("columns.case")}</TableHead>
            ) : null}

            <TableHead aria-sort={ariaSort("status", sortBy, sortOrder)}>
              <SortButton
                field="status"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead className="hidden xl:table-cell">{t("columns.sections")}</TableHead>

            <TableHead
              className="hidden xl:table-cell"
              aria-sort={ariaSort("created_at", sortBy, sortOrder)}
            >
              <SortButton
                field="created_at"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead className="w-12">
              <span className="sr-only">{tActions("openMenu")}</span>
            </TableHead>
          </TableRow>
        </TableHeader>

        <TableBody>
          {reports.map((report) => (
            <TableRow key={report.id}>
              <TableCell>
                <div className="flex items-start gap-2">
                  <FileText
                    className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
                    aria-hidden="true"
                  />
                  <div className="flex flex-col">
                    <button
                      type="button"
                      onClick={() => onView(report)}
                      className="text-start text-sm font-medium text-foreground hover:underline"
                      dir="auto"
                    >
                      {report.title}
                    </button>
                    <span className="text-xs text-muted-foreground">
                      {tTypes(report.reportType)}
                    </span>
                  </div>
                </div>
              </TableCell>

              {showCase ? (
                <TableCell className="hidden lg:table-cell">
                  <Link
                    href={caseRoute(report.caseId)}
                    className="text-sm text-accent hover:underline"
                  >
                    {t("openCase")}
                  </Link>
                </TableCell>
              ) : null}

              <TableCell>
                <div className="flex min-w-40 flex-col gap-1.5">
                  <ReportStatusBadge status={report.status} />
                  {report.isActive ? <ReportProgress report={report} /> : null}
                  {report.status === "failed" ? (
                    <span className="text-xs text-muted-foreground">
                      {tFailures(report.errorCode ?? "unknown")}
                    </span>
                  ) : null}
                </div>
              </TableCell>

              <TableCell className="hidden text-sm text-muted-foreground xl:table-cell">
                {report.status === "completed"
                  ? t("groundedOf", {
                      grounded: report.groundedSections ?? 0,
                      total: report.sectionsTotal ?? 0,
                    })
                  : "—"}
              </TableCell>

              <TableCell className="hidden text-sm text-muted-foreground xl:table-cell">
                {formatDateTime(report.createdAt)}
              </TableCell>

              <TableCell>
                <ReportRowActions report={report} onView={onView} onDelete={onDelete} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
