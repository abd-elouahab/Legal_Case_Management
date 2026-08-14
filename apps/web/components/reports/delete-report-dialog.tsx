"use client";

import { useTranslations } from "next-intl";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useDeleteReport, useReportErrorMessage } from "@/hooks/use-reports";
import type { Report } from "@/types/report";

/**
 * Destructive confirmation for withdrawing a report.
 *
 * The copy states plainly what actually happens, because "delete" usually implies
 * something stronger than what the platform does: the report leaves the user's
 * history immediately and can no longer be opened or exported, while the record
 * itself is **kept** — it carries the citations of an analysis a lawyer may have
 * acted on, and destroying that would destroy the record of advice that was
 * given.
 *
 * It also says the case is untouched, which is the question anyone hesitating
 * over this button is actually asking.
 */
export function DeleteReportDialog({
  report,
  open,
  onOpenChange,
  onDeleted,
}: {
  report: Report | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDeleted?: () => void;
}) {
  const remove = useDeleteReport();
  const t = useTranslations("reports.deleteDialog");
  const tActions = useTranslations("common.actions");
  const errorMessage = useReportErrorMessage();

  async function onConfirm() {
    if (!report) return;

    try {
      await remove.mutateAsync(report.id);
      toast.success(t("deleted"));
      onOpenChange(false);
      onDeleted?.();
    } catch (error) {
      toast.error(errorMessage(error));
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={remove.isPending ? undefined : onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("title")}</AlertDialogTitle>
          <AlertDialogDescription>
            {report ? (
              <>
                {/* The title is a row's own data, so it stays a `dir="auto"`
                    element of its own rather than an interpolation — an Arabic
                    report title inside a French sentence needs its own direction. */}
                <span dir="auto">{report.title}</span> {t("description")}
              </>
            ) : null}
          </AlertDialogDescription>
        </AlertDialogHeader>

        <AlertDialogFooter>
          <AlertDialogCancel disabled={remove.isPending}>{tActions("cancel")}</AlertDialogCancel>
          <AlertDialogAction
            onClick={(event) => {
              // The action closes the dialog by default; the mutation decides
              // when it closes, so a failure keeps the confirmation on screen
              // with its message rather than dismissing it silently.
              event.preventDefault();
              void onConfirm();
            }}
            disabled={remove.isPending}
          >
            {remove.isPending ? t("deleting") : t("confirm")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
