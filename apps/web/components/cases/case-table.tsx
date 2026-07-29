"use client";

import Link from "next/link";
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
import { formatDate, formatDateTime } from "@/lib/format";
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

interface SortableColumn {
  field: CaseSortField;
  label: string;
}

function SortButton({
  column,
  sortBy,
  sortOrder,
  onToggle,
}: {
  column: SortableColumn;
  sortBy: CaseSortField;
  sortOrder: SortOrder;
  onToggle: (field: CaseSortField) => void;
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
                column={{ field: "case_number", label: "Case number" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead>Title</TableHead>
            <TableHead>Status</TableHead>
            <TableHead aria-sort={ariaSort("priority", sortBy, sortOrder)}>
              <SortButton
                column={{ field: "priority", label: "Priority" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead className="hidden lg:table-cell">Court</TableHead>
            <TableHead className="hidden xl:table-cell">Assigned lawyer</TableHead>
            <TableHead className="hidden xl:table-cell">Assigned representative</TableHead>
            <TableHead
              className="hidden lg:table-cell"
              aria-sort={ariaSort("filing_date", sortBy, sortOrder)}
            >
              <SortButton
                column={{ field: "filing_date", label: "Filing date" }}
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
                column={{ field: "next_hearing_date", label: "Next hearing" }}
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
                column={{ field: "updated_at", label: "Last updated" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead className="w-12 text-right">
              <span className="sr-only">Actions</span>
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
                {legalCase.courtName ?? "—"}
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
                {formatDate(legalCase.nextHearingDate, "Not scheduled")}
              </TableCell>

              <TableCell className="hidden text-muted-foreground xl:table-cell">
                {formatDateTime(legalCase.updatedAt)}
              </TableCell>

              <TableCell className="text-right">
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
