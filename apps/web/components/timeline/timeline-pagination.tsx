"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import { Spinner } from "@/components/shared/spinner";
import { Button } from "@/components/ui/button";

/**
 * Pagination controls for a case timeline.
 *
 * Shows the record range as well as the page number, because "showing 21–40 of
 * 137" answers the question a user actually has — how much of the history they
 * are looking at — which "page 2 of 7" does not.
 *
 * Unlike the document list's controls, this one shows a **spinner while the next
 * page loads**: the previous page stays on screen (see `placeholderData`), so
 * without an indicator pressing Next appears to do nothing on a slow connection.
 *
 * The live region announces the range as it changes, so a screen-reader user
 * learns the result of pressing Next without hunting for it.
 */
export function TimelinePagination({
  page,
  pageSize,
  totalPages,
  totalRecords,
  onPageChange,
  isLoading = false,
}: {
  page: number;
  pageSize: number;
  totalPages: number;
  totalRecords: number;
  onPageChange: (page: number) => void;
  isLoading?: boolean;
}) {
  const first = totalRecords === 0 ? 0 : (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, totalRecords);

  return (
    <div className="flex flex-col items-center justify-between gap-3 sm:flex-row">
      <p className="flex items-center gap-2 text-sm text-muted-foreground" aria-live="polite">
        {isLoading ? <Spinner className="h-4 w-4" /> : null}
        {totalRecords === 0
          ? "No activity"
          : `Showing ${first}–${last} of ${totalRecords} ${
              totalRecords === 1 ? "entry" : "entries"
            }`}
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
          disabled={isLoading || page <= 1}
        >
          <ChevronLeft className="h-4 w-4" />
          Previous
        </Button>

        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onPageChange(page + 1)}
          disabled={isLoading || page >= totalPages}
        >
          Next
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
