"use client";

import Link from "next/link";
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
import { formatDateTime } from "@/lib/format";
import { caseRoute } from "@/lib/routes";
import { cn } from "@/lib/utils";
import { reportFailureLabel, reportTypeLabel, type Report } from "@/types/report";
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

interface SortableColumn {
  field: ReportSortField;
  label: string;
}

function SortButton({
  column,
  sortBy,
  sortOrder,
  onToggle,
}: {
  column: SortableColumn;
  sortBy: ReportSortField;
  sortOrder: SortOrder;
  onToggle: (field: ReportSortField) => void;
}) {
  const isActive = sortBy === column.field;
  const Icon = !isActive ? ChevronsUpDown : sortOrder === "asc" ? ArrowUp : ArrowDown;

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => onToggle(column.field)}
      className={cn(
        "-ml-2 h-8 gap-1 px-2 font-medium",
        isActive ? "text-foreground" : "text-muted-foreground",
      )}
    >
      {column.label}
      <Icon className="h-4 w-4" aria-hidden="true" />
      <span className="sr-only">
        {isActive
          ? `Sorted ${sortOrder === "asc" ? "ascending" : "descending"}. Activate to reverse.`
          : `Sort by ${column.label}`}
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
  return (
    <div className="w-full overflow-x-auto rounded-lg border border-border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead aria-sort={ariaSort("title", sortBy, sortOrder)}>
              <SortButton
                column={{ field: "title", label: "Report" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            {showCase ? <TableHead className="hidden lg:table-cell">Case</TableHead> : null}

            <TableHead aria-sort={ariaSort("status", sortBy, sortOrder)}>
              <SortButton
                column={{ field: "status", label: "Status" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead className="hidden xl:table-cell">Sections</TableHead>

            <TableHead
              className="hidden xl:table-cell"
              aria-sort={ariaSort("created_at", sortBy, sortOrder)}
            >
              <SortButton
                column={{ field: "created_at", label: "Requested" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead className="w-12">
              <span className="sr-only">Actions</span>
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
                      className="text-left text-sm font-medium text-foreground hover:underline"
                      dir="auto"
                    >
                      {report.title}
                    </button>
                    <span className="text-xs text-muted-foreground">
                      {reportTypeLabel(report.reportType)}
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
                    Open case
                  </Link>
                </TableCell>
              ) : null}

              <TableCell>
                <div className="flex min-w-40 flex-col gap-1.5">
                  <ReportStatusBadge status={report.status} />
                  {report.isActive ? <ReportProgress report={report} /> : null}
                  {report.status === "failed" ? (
                    <span className="text-xs text-muted-foreground">
                      {reportFailureLabel(report.errorCode)}
                    </span>
                  ) : null}
                </div>
              </TableCell>

              <TableCell className="hidden text-sm text-muted-foreground xl:table-cell">
                {report.status === "completed"
                  ? `${report.groundedSections ?? 0}/${report.sectionsTotal ?? 0} grounded`
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
