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
 * Loading placeholder for the report history.
 *
 * Matches the real table's column layout so the page does not shift when the
 * first page arrives — a skeleton whose shape differs from what replaces it
 * causes the jump it exists to prevent.
 */
export function ReportTableSkeleton({
  rows = 5,
  showCase = true,
}: {
  rows?: number;
  showCase?: boolean;
}) {
  const t = useTranslations("reports.table");
  const tActions = useTranslations("common.actions");

  return (
    <div
      className="w-full overflow-x-auto rounded-lg border border-border"
      aria-busy="true"
      aria-label={t("loading")}
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("columns.report")}</TableHead>
            {showCase ? (
              <TableHead className="hidden lg:table-cell">{t("columns.case")}</TableHead>
            ) : null}
            <TableHead>{t("columns.status")}</TableHead>
            <TableHead className="hidden xl:table-cell">{t("columns.sections")}</TableHead>
            <TableHead className="hidden xl:table-cell">
              {t("columns.requested")}
            </TableHead>
            <TableHead className="w-12">
              <span className="sr-only">{tActions("openMenu")}</span>
            </TableHead>
          </TableRow>
        </TableHeader>

        <TableBody>
          {Array.from({ length: rows }, (_, index) => (
            <TableRow key={index}>
              <TableCell>
                <div className="flex flex-col gap-1.5">
                  <Skeleton className="h-4 w-48" />
                  <Skeleton className="h-3 w-24" />
                </div>
              </TableCell>
              {showCase ? (
                <TableCell className="hidden lg:table-cell">
                  <Skeleton className="h-4 w-20" />
                </TableCell>
              ) : null}
              <TableCell>
                <Skeleton className="h-6 w-24 rounded-full" />
              </TableCell>
              <TableCell className="hidden xl:table-cell">
                <Skeleton className="h-4 w-20" />
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
