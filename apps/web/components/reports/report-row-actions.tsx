"use client";

import { useTranslations } from "next-intl";
import { Eye, MoreHorizontal, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Protected } from "@/components/auth/protected";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useRegenerateReport, useReportErrorMessage } from "@/hooks/use-reports";
import { PERMISSION } from "@/types/authorization";
import type { Report } from "@/types/report";

/**
 * Per-row actions for one report.
 *
 * **A user is never offered an action the API would refuse.** Regenerate appears
 * only on a finished run — the API answers 409 for one already in flight — and
 * only for a caller holding both permissions a generation needs, which is the
 * same pair the endpoint requires. Export is deliberately *not* here: it belongs
 * beside the report itself, where the format choice has room and the download can
 * report progress and failure.
 */
export function ReportRowActions({
  report,
  onView,
  onDelete,
}: {
  report: Report;
  onView: (report: Report) => void;
  onDelete: (report: Report) => void;
}) {
  const regenerate = useRegenerateReport();
  const t = useTranslations("reports.actions");
  const tActions = useTranslations("common.actions");
  const errorMessage = useReportErrorMessage();

  async function onRegenerate() {
    try {
      await regenerate.mutateAsync(report.id);
      toast.success(t("queuedAgain"));
    } catch (error) {
      toast.error(errorMessage(error));
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t("menuFor", { title: report.title })}
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={() => onView(report)}>
          <Eye className="h-4 w-4" aria-hidden="true" />
          {t("open")}
        </DropdownMenuItem>

        {report.isTerminal ? (
          <Protected allOf={[PERMISSION.reportsGenerate, PERMISSION.aiGenerateReport]}>
            <DropdownMenuItem
              onSelect={() => void onRegenerate()}
              disabled={regenerate.isPending}
            >
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              {t("regenerate")}
            </DropdownMenuItem>
          </Protected>
        ) : null}

        <DropdownMenuSeparator />

        <DropdownMenuItem variant="destructive" onSelect={() => onDelete(report)}>
          <Trash2 className="h-4 w-4" aria-hidden="true" />
          {tActions("delete")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
