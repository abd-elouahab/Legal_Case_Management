"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { CaseAssignee } from "@/components/cases/case-assignee";
import { CasePriorityBadge, CaseStatusBadge } from "@/components/cases/case-badges";
import { CaseRowActions } from "@/components/cases/case-row-actions";
import { useDateFormat } from "@/hooks/use-date-format";
import { caseRoute } from "@/lib/routes";
import { cn } from "@/lib/utils";
import type { LegalCase } from "@/types/case";
import type { CaseSortField, SortOrder } from "@/types/case-management";

/**
 * The case list table.
 *
 * Sortable columns are `<button>`s inside the header cell rather than click
 * handlers on the cell itself, so they are reachable and operable by keyboard,
 * and each carries `aria-sort` so a screen reader announces the current order.
 *
 * On small screens the table scrolls horizontally inside its own container. The
 * columns hide progressively — the two assignees below `xl`, court and dates
 * below `lg` — so a phone shows the case number, title, status, and priority
 * without a horizontal scroll at all. What survives is what identifies a case
 * and what tells the reader whether it needs attention.
 */

/**
 * A sortable column heading.
 *
 * Takes the **field** and translates its own label, rather than being handed a
 * string: a caller that passed `label="Case number"` would be a caller with a
 * hardcoded sentence in it, which is the defect this whole change removes. The
 * screen-reader sentence is one interpolated message rather than three
 * concatenated fragments, because *"Sorted ascending"* is not two words in French
 * and is not two words in the same order in Arabic.
 */
function SortButton({
  field,
  sortBy,
  sortOrder,
  onToggle,
}: {
  field: CaseSortField;
  sortBy: CaseSortField;
  sortOrder: SortOrder;
  onToggle: (field: CaseSortField) => void;
}) {
  const t = useTranslations("cases.table");
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
  field: CaseSortField,
  sortBy: CaseSortField,
  sortOrder: SortOrder,
): "ascending" | "descending" | "none" {
  if (sortBy !== field) return "none";
  return sortOrder === "asc" ? "ascending" : "descending";
}

export function CaseTable({
  cases,
  sortBy,
  sortOrder,
  onToggleSort,
  onEdit,
  onArchive,
  onRestore,
  onAssign,
  isRefreshing = false,
}: {
  cases: LegalCase[];
  sortBy: CaseSortField;
  sortOrder: SortOrder;
  onToggleSort: (field: CaseSortField) => void;
  onEdit: (legalCase: LegalCase) => void;
  onArchive: (legalCase: LegalCase) => void;
  onRestore: (legalCase: LegalCase) => void;
  onAssign: (legalCase: LegalCase) => void;
  /** Dim the table while a new page or filter is loading. */
  isRefreshing?: boolean;
}) {
  const { formatDate, formatDateTime } = useDateFormat();
  const t = useTranslations("cases.table");
  const tStates = useTranslations("common.states");
  const tActions = useTranslations("common.actions");

  return (
    <div
      className={cn(
        "w-full overflow-x-auto rounded-lg border border-border transition-opacity",
        isRefreshing && "opacity-60",
      )}
      aria-busy={isRefreshing}
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead aria-sort={ariaSort("case_number", sortBy, sortOrder)}>
              <SortButton
                field="case_number"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead>{t("columns.title")}</TableHead>
            <TableHead>{t("columns.status")}</TableHead>
            <TableHead aria-sort={ariaSort("priority", sortBy, sortOrder)}>
              <SortButton
                field="priority"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead className="hidden lg:table-cell">{t("columns.court")}</TableHead>
            <TableHead className="hidden xl:table-cell">
              {t("columns.assignedLawyer")}
            </TableHead>
            <TableHead className="hidden xl:table-cell">
              {t("columns.assignedRepresentative")}
            </TableHead>
            <TableHead
              className="hidden lg:table-cell"
              aria-sort={ariaSort("filing_date", sortBy, sortOrder)}
            >
              <SortButton
                field="filing_date"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead
              className="hidden lg:table-cell"
              aria-sort={ariaSort("next_hearing_date", sortBy, sortOrder)}
            >
              <SortButton
                field="next_hearing_date"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead
              className="hidden xl:table-cell"
              aria-sort={ariaSort("updated_at", sortBy, sortOrder)}
            >
              <SortButton
                field="updated_at"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead className="w-12 text-end">
              <span className="sr-only">{tActions("openMenu")}</span>
            </TableHead>
          </TableRow>
        </TableHeader>

        <TableBody>
          {cases.map((legalCase) => (
            <TableRow key={legalCase.id}>
              <TableCell className="font-mono text-xs text-muted-foreground">
                {legalCase.caseNumber}
              </TableCell>

              <TableCell className="max-w-72">
                <Link
                  href={caseRoute(legalCase.id)}
                  className="font-medium text-foreground hover:underline"
                >
                  {legalCase.title}
                </Link>
                {legalCase.category ? (
                  <p className="truncate text-xs text-muted-foreground">{legalCase.category}</p>
                ) : null}
              </TableCell>

              <TableCell>
                <CaseStatusBadge status={legalCase.status} />
              </TableCell>

              <TableCell>
                <CasePriorityBadge priority={legalCase.priority} />
              </TableCell>

              <TableCell className="hidden text-muted-foreground lg:table-cell">
                {legalCase.courtName ?? tStates("notAvailable")}
              </TableCell>

              <TableCell className="hidden xl:table-cell">
                <CaseAssignee user={legalCase.assignedLawyer} />
              </TableCell>

              <TableCell className="hidden xl:table-cell">
                <CaseAssignee user={legalCase.assignedCourtRepresentative} />
              </TableCell>

              <TableCell className="hidden text-muted-foreground lg:table-cell">
                {formatDate(legalCase.filingDate)}
              </TableCell>

              <TableCell className="hidden text-muted-foreground lg:table-cell">
                {formatDate(legalCase.nextHearingDate, t("notScheduled"))}
              </TableCell>

              <TableCell className="hidden text-muted-foreground xl:table-cell">
                {formatDateTime(legalCase.updatedAt)}
              </TableCell>

              <TableCell className="text-end">
                <CaseRowActions
                  legalCase={legalCase}
                  onEdit={onEdit}
                  onArchive={onArchive}
                  onRestore={onRestore}
                  onAssign={onAssign}
                />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
