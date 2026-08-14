"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

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
import { useAssistantErrorMessage, useDeleteConversation } from "@/hooks/use-assistant";
import type { Conversation } from "@/types/assistant";

/**
 * Delete a conversation.
 *
 * `AlertDialog` rather than `Dialog` because this is destructive from the user's
 * point of view — the same reason archiving a case uses one.
 *
 * **The copy says what actually happens, and what does not.** The conversation
 * stops being readable immediately and cannot be restored from the interface;
 * server-side the transcript is withdrawn rather than destroyed, because it
 * carries the citations of advice that may have been acted on. Saying "deleted
 * permanently" would be false, and saying "kept" would sound like it can be
 * recovered — so the sentence states the effect the user experiences and the fact
 * that archiving is the reversible alternative.
 */
export function DeleteConversationDialog({
  conversation,
  open,
  onOpenChange,
  onDeleted,
}: {
  conversation: Conversation | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the deleted identifier, so a screen showing it can move on. */
  onDeleted?: (id: string) => void;
}) {
  const remove = useDeleteConversation();
  const t = useTranslations("assistant.deleteConversation");
  const tActions = useTranslations("common.actions");
  const errorMessage = useAssistantErrorMessage();
  const [error, setError] = React.useState<string | null>(null);

  // Cleared as the dialog opens or closes rather than from an effect: an effect
  // that calls setState on a prop change causes a second render for something
  // that is already an event, and React's own guidance is to do it in the
  // handler. Reopening after a failure therefore starts clean.
  function setOpen(next: boolean) {
    setError(null);
    onOpenChange(next);
  }

  function confirm() {
    if (!conversation) return;

    remove.mutate(conversation.id, {
      onSuccess: () => {
        onDeleted?.(conversation.id);
        setOpen(false);
      },
      onError: (failure) => setError(errorMessage(failure)),
    });
  }

  return (
    <AlertDialog open={open} onOpenChange={remove.isPending ? undefined : setOpen}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {conversation ? t("title", { title: conversation.title }) : t("titleGeneric")}
          </AlertDialogTitle>
          <AlertDialogDescription>{t("description")}</AlertDialogDescription>
        </AlertDialogHeader>

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        <AlertDialogFooter>
          <AlertDialogCancel disabled={remove.isPending} onClick={() => setError(null)}>
            {tActions("cancel")}
          </AlertDialogCancel>
          <Button variant="destructive" onClick={confirm} disabled={remove.isPending}>
            {remove.isPending ? (
              <>
                <Spinner className="h-4 w-4 text-current" />
                {t("deleting")}
              </>
            ) : (
              tActions("delete")
            )}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
