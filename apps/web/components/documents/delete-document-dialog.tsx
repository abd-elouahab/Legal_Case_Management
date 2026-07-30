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
import { documentErrorMessage, useDeleteDocument } from "@/hooks/use-documents";
import type { LegalDocument } from "@/types/document";

/**
 * Delete confirmation.
 *
 * The copy says exactly what happens, because "delete" is a misleading word for
 * this action: the file is **not** destroyed. The document is withdrawn from the
 * case and disappears from every list, but the record and every stored version
 * are retained — permanently destroying a legal document is forbidden, and an
 * administrator who expects an irreversible deletion should learn that here
 * rather than afterwards.
 *
 * Uses `AlertDialog` rather than `Dialog` because this is a destructive
 * confirmation: it takes an explicit choice to dismiss, and cannot be closed by
 * clicking away.
 */
export function DeleteDocumentDialog({
  document,
  open,
  onOpenChange,
}: {
  document: LegalDocument | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const remove = useDeleteDocument();
  const [error, setError] = React.useState<string | null>(null);

  // Clear a stale error from a previous attempt when the dialog reopens.
  // Adjusted during render rather than in an effect, so the error never flashes
  // back on screen (https://react.dev/learn/you-might-not-need-an-effect).
  const [wasOpen, setWasOpen] = React.useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setError(null);
  }

  async function confirm() {
    if (!document) return;
    setError(null);

    try {
      await remove.mutateAsync(document.id);
      toast.success(`${document.originalFilename} was deleted.`);
      onOpenChange(false);
    } catch (cause) {
      setError(documentErrorMessage(cause));
    }
  }

  const isPending = remove.isPending;

  return (
    <AlertDialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            Delete {document ? document.originalFilename : "this document"}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            The document is withdrawn from the case and will no longer appear in
            lists, searches, or downloads. It is kept — not destroyed — along with
            every stored version, because a legal document is never permanently
            removed without authorization.
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
                Deleting…
              </>
            ) : (
              "Delete"
            )}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
