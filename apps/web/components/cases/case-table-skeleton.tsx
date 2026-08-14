"use client";

import { useTranslations } from "next-intl";

import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/**
 * Loading skeleton for the case list.
 *
 * Mirrors the real table's column layout, including which columns hide at which
 * breakpoint, so the page does not reflow when the data arrives — a skeleton
 * that is the wrong shape is worse than a spinner, because it promises a layout
 * it then changes.
 */
export function CaseTableSkeleton({ rows = 5 }: { rows?: number }) {
  const t = useTranslations("cases.table");

  return (
    <div
      className="w-full overflow-x-auto rounded-lg border border-border"
      aria-busy="true"
      aria-label={t("loading")}
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("columns.case_number")}</TableHead>
            <TableHead>{t("columns.title")}</TableHead>
            <TableHead>{t("columns.status")}</TableHead>
            <TableHead>{t("columns.priority")}</TableHead>
            <TableHead className="hidden lg:table-cell">{t("columns.court")}</TableHead>
            <TableHead className="hidden xl:table-cell">
              {t("columns.assignedLawyer")}
            </TableHead>
            <TableHead className="hidden xl:table-cell">
              {t("columns.assignedRepresentative")}
            </TableHead>
            <TableHead className="hidden lg:table-cell">
              {t("columns.filing_date")}
            </TableHead>
            <TableHead className="hidden lg:table-cell">
              {t("columns.next_hearing_date")}
            </TableHead>
            <TableHead className="hidden xl:table-cell">
              {t("columns.updated_at")}
            </TableHead>
            <TableHead className="w-12" />
          </TableRow>
        </TableHeader>

        <TableBody>
          {Array.from({ length: rows }, (_, index) => (
            <TableRow key={index}>
              <TableCell>
                <Skeleton className="h-4 w-28" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-48" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-5 w-24 rounded-full" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-5 w-16 rounded-full" />
              </TableCell>
              <TableCell className="hidden lg:table-cell">
                <Skeleton className="h-4 w-32" />
              </TableCell>
              <TableCell className="hidden xl:table-cell">
                <div className="flex items-center gap-2">
                  <Skeleton className="size-7 rounded-full" />
                  <Skeleton className="h-4 w-28" />
                </div>
              </TableCell>
              <TableCell className="hidden xl:table-cell">
                <div className="flex items-center gap-2">
                  <Skeleton className="size-7 rounded-full" />
                  <Skeleton className="h-4 w-28" />
                </div>
              </TableCell>
              <TableCell className="hidden lg:table-cell">
                <Skeleton className="h-4 w-24" />
              </TableCell>
              <TableCell className="hidden lg:table-cell">
                <Skeleton className="h-4 w-24" />
              </TableCell>
              <TableCell className="hidden xl:table-cell">
                <Skeleton className="h-4 w-28" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-8 w-8 rounded-md" />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
