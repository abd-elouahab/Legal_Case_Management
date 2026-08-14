"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
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
import { useArchiveCase, useCaseErrorMessage } from "@/hooks/use-cases";
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
  const t = useTranslations("cases.archiveDialog");
  const tActions = useTranslations("common.actions");
  const errorMessage = useCaseErrorMessage();
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
      toast.success(t("archived", { caseNumber: legalCase.caseNumber }));
      onOpenChange(false);
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }

  const isPending = archive.isPending;

  return (
    <AlertDialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          {/* The case number is interpolated rather than concatenated, so a
              language that puts the object before the verb can say so. */}
          <AlertDialogTitle>
            {legalCase
              ? t("title", { caseNumber: legalCase.caseNumber })
              : t("titleGeneric")}
          </AlertDialogTitle>
          <AlertDialogDescription>{t("description")}</AlertDialogDescription>
        </AlertDialogHeader>

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        <AlertDialogFooter>
          <AlertDialogCancel disabled={isPending}>{tActions("cancel")}</AlertDialogCancel>
          <Button variant="destructive" onClick={confirm} disabled={isPending}>
            {isPending ? (
              <>
                <Spinner className="h-4 w-4 text-current" />
                {t("archiving")}
              </>
            ) : (
              t("confirm")
            )}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
