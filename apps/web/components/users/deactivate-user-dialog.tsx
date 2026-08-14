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
import { useDeactivateUser, useUserErrorMessage } from "@/hooks/use-users";
import type { ManagedUser } from "@/types/user";

/**
 * Deactivation confirmation.
 *
 * The copy says exactly what happens, because "delete" is a misleading word for
 * this action: the account is **not** removed. It is marked inactive, its
 * sessions end immediately, and it can be reactivated later. An administrator who
 * expects a permanent deletion and gets a soft one should learn that here, not
 * afterwards.
 *
 * Uses `AlertDialog` rather than `Dialog` because this is a destructive
 * confirmation: it takes an explicit choice to dismiss, and cannot be closed by
 * clicking away.
 */
export function DeactivateUserDialog({
  user,
  open,
  onOpenChange,
}: {
  user: ManagedUser | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const deactivate = useDeactivateUser();
  const t = useTranslations("users.deactivateDialog");
  const tActions = useTranslations("common.actions");
  const errorMessage = useUserErrorMessage();
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
    if (!user) return;
    setError(null);

    try {
      await deactivate.mutateAsync(user.id);
      toast.success(t("deactivated", { name: user.fullName }));
      onOpenChange(false);
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }

  const isPending = deactivate.isPending;

  return (
    <AlertDialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {user ? t("title", { name: user.fullName }) : t("titleGeneric")}
          </AlertDialogTitle>
          <AlertDialogDescription>
            They will be signed out of every device and will no longer be able to
            sign in. The account is kept — not deleted — so their history stays
            intact, and you can reactivate it at any time.
          </AlertDialogDescription>
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
                {t("deactivating")}
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
