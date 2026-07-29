"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Pagination controls for the case list.
 *
 * Shows the record range as well as the page number, because "showing 21–40 of
 * 137" answers the question a user actually has — how much of the caseload they
 * are looking at — which "page 2 of 7" does not.
 *
 * The live region announces the range as it changes, so a screen-reader user
 * learns the result of pressing Next without hunting for it.
 */
export function CasePagination({
  page,
  pageSize,
  totalPages,
  totalRecords,
  onPageChange,
  disabled = false,
}: {
  page: number;
  pageSize: number;
  totalPages: number;
  totalRecords: number;
  onPageChange: (page: number) => void;
  disabled?: boolean;
}) {
  const first = totalRecords === 0 ? 0 : (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, totalRecords);

  return (
    <div className="flex flex-col items-center justify-between gap-3 sm:flex-row">
      <p className="text-sm text-muted-foreground" aria-live="polite">
        {totalRecords === 0
          ? "No cases"
          : `Showing ${first}–${last} of ${totalRecords} case${totalRecords === 1 ? "" : "s"}`}
      </p>

      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">
          Page {page} of {totalPages}
        </span>

        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onPageChange(page - 1)}
          disabled={disabled || page <= 1}
        >
          <ChevronLeft className="h-4 w-4" />
          Previous
        </Button>

        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onPageChange(page + 1)}
          disabled={disabled || page >= totalPages}
        >
          Next
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
