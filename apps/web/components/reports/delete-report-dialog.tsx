"use client";

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
import { reportErrorMessage, useDeleteReport } from "@/hooks/use-reports";
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

  async function onConfirm() {
    if (!report) return;

    try {
      await remove.mutateAsync(report.id);
      toast.success("Report deleted.");
      onOpenChange(false);
      onDeleted?.();
    } catch (error) {
      toast.error(reportErrorMessage(error));
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={remove.isPending ? undefined : onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this report?</AlertDialogTitle>
          <AlertDialogDescription>
            {report ? (
              <>
                <span dir="auto">{report.title}</span> leaves your history and can no
                longer be opened or exported. The case, its documents, and its timeline
                are untouched. Generating the report again is always possible.
              </>
            ) : null}
          </AlertDialogDescription>
        </AlertDialogHeader>

        <AlertDialogFooter>
          <AlertDialogCancel disabled={remove.isPending}>Cancel</AlertDialogCancel>
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
            {remove.isPending ? "Deleting…" : "Delete report"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
