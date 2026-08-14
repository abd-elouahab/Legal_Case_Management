"use client";

import { useTranslations } from "next-intl";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Pagination controls for the report history.
 *
 * Shows the record range as well as the page number, because "showing 21–40 of
 * 137" answers the question a user actually has — how much of the material they
 * are looking at — which "page 2 of 7" does not.
 *
 * The live region announces the range as it changes, so a screen-reader user
 * learns the result of pressing Next without hunting for it.
 */
export function ReportPagination({
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
  const t = useTranslations("reports.pagination");
  const tCommon = useTranslations("common.pagination");
  const tActions = useTranslations("common.actions");

  const first = totalRecords === 0 ? 0 : (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, totalRecords);

  return (
    <div className="flex flex-col items-center justify-between gap-3 sm:flex-row">
      <p className="text-sm text-muted-foreground" aria-live="polite">
        {t("summary", { from: first, to: last, count: totalRecords })}
      </p>

      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">
          {tCommon("pageOf", { page, total: totalPages })}
        </span>

        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onPageChange(page - 1)}
          disabled={disabled || page <= 1}
        >
          <ChevronLeft data-flip-rtl className="h-4 w-4" />
          {tActions("previous")}
        </Button>

        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onPageChange(page + 1)}
          disabled={disabled || page >= totalPages}
        >
          {tActions("next")}
          <ChevronRight data-flip-rtl className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
