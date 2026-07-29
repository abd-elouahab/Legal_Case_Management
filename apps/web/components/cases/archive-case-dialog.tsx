"use client";

import * as React from "react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/shared/spinner";
import { caseErrorMessage, useArchiveCase } from "@/hooks/use-cases";
import type { LegalCase } from "@/types/case";

/**
 * Archive confirmation.
 *
 * The copy says exactly what happens, because "delete" is a misleading word for
 * this action: the case is **not** removed. It is marked archived, stays
 * searchable, keeps its documents and history, and can be restored. A user who
 * expects a permanent deletion and gets a soft one should learn that here, not
 * afterwards.
 *
 * Uses `AlertDialog` rather than `Dialog` because this is a destructive
 * confirmation: it takes an explicit choice to dismiss, and cannot be closed by
 * clicking away.
 */
export function ArchiveCaseDialog({
  legalCase,
  open,
  onOpenChange,
}: {
  legalCase: LegalCase | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const archive = useArchiveCase();
  const [error, setError] = React.useState<string | null>(null);

  // Clear a stale error from a previous attempt when the dialog reopens.
  // Adjusted during render rather than in an effect: React re-renders before
  // painting, so the error never flashes back on screen the way an effect-based
  // reset would let it (https://react.dev/learn/you-might-not-need-an-effect).
  const [wasOpen, setWasOpen] = React.useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setError(null);
  }

  async function confirm() {
    if (!legalCase) return;
    setError(null);

    try {
      await archive.mutateAsync(legalCase.id);
      toast.success(`Case ${legalCase.caseNumber} was archived.`);
      onOpenChange(false);
    } catch (cause) {
      setError(caseErrorMessage(cause));
    }
  }

  const isPending = archive.isPending;

  return (
    <AlertDialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            Archive {legalCase ? legalCase.caseNumber : "this case"}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            The case leaves the active workload but is kept — not deleted — so its
            documents, timeline, and history stay intact. It remains searchable,
            and you can restore it at any time.
          </AlertDialogDescription>
        </AlertDialogHeader>

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        <AlertDialogFooter>
          <AlertDialogCancel disabled={isPending}>Cancel</AlertDialogCancel>
          <Button variant="destructive" onClick={confirm} disabled={isPending}>
            {isPending ? (
              <>
                <Spinner className="h-4 w-4 text-current" />
                Archiving…
              </>
            ) : (
              "Archive"
            )}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
