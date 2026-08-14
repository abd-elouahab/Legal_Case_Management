"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/shared/spinner";
import { useAssistantErrorMessage, useUpdateConversation } from "@/hooks/use-assistant";
import {
  MAX_TITLE_LENGTH,
  conversationTitleFormSchema,
} from "@/lib/validation/assistant";
import { useFieldError } from "@/hooks/use-field-error";
import type { Conversation } from "@/types/assistant";

/**
 * Rename a conversation.
 *
 * The spec requires titles to be *editable by the user*, and this is that. It is
 * a dialog rather than an inline edit because a conversation title is also a list
 * row: making the row itself editable turns every mis-click in the sidebar into
 * an accidental rename.
 *
 * Renaming marks the title as the user's, permanently and server-side — automatic
 * titling never overwrites a name somebody chose, which is what makes the
 * generated first title safe to offer at all.
 */
export function RenameConversationDialog({
  conversation,
  open,
  onOpenChange,
}: {
  conversation: Conversation | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        {/* Keyed by the conversation, so the form's state is *initialized* from
            the current title rather than synchronized to it by an effect. An
            effect that called setState on a prop change would cost a second
            render and is exactly what React's own guidance says to replace with
            a key. */}
        {conversation ? (
          <RenameForm
            key={conversation.id}
            conversation={conversation}
            onDone={() => onOpenChange(false)}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function RenameForm({
  conversation,
  onDone,
}: {
  conversation: Conversation;
  onDone: () => void;
}) {
  const update = useUpdateConversation();
  const t = useTranslations("assistant.rename");
  const tActions = useTranslations("common.actions");
  const errorMessage = useAssistantErrorMessage();
  const fieldError = useFieldError();
  const [title, setTitle] = React.useState(conversation.title);
  const [error, setError] = React.useState<string | null>(null);

  function submit(event: React.FormEvent) {
    event.preventDefault();

    const parsed = conversationTitleFormSchema.safeParse({ title });
    if (!parsed.success) {
      setError(fieldError(parsed.error.issues[0]?.message) ?? t("enterName"));
      return;
    }

    update.mutate(
      { id: conversation.id, input: { title: parsed.data.title } },
      {
        onSuccess: onDone,
        onError: (failure) => setError(errorMessage(failure)),
      },
    );
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>{t("title")}</DialogTitle>
        <DialogDescription>{t("description")}</DialogDescription>
      </DialogHeader>

      <form onSubmit={submit} className="flex flex-col gap-3">
        <div className="flex flex-col gap-2">
          <Label htmlFor="conversation-title">{t("name")}</Label>
          <Input
            id="conversation-title"
            value={title}
            maxLength={MAX_TITLE_LENGTH}
            onChange={(event) => setTitle(event.target.value)}
            dir="auto"
            autoFocus
            aria-invalid={error !== null}
            aria-describedby={error ? "conversation-title-error" : undefined}
          />
          {error ? (
            <p id="conversation-title-error" role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={onDone}
            disabled={update.isPending}
          >
            {tActions("cancel")}
          </Button>
          <Button type="submit" disabled={update.isPending}>
            {update.isPending ? (
              <>
                <Spinner className="h-4 w-4 text-current" />
                {tActions("saving")}
              </>
            ) : (
              tActions("save")
            )}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
