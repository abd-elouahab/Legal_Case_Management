"use client";

import * as React from "react";
import { Bot, PanelLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { AccessDenied } from "@/components/shared/access-denied";
import { AssistantChat } from "@/components/ai/assistant-chat";
import { ConversationList } from "@/components/ai/conversation-list";
import { useConversations, useCreateConversation } from "@/hooks/use-assistant";
import { usePermissions } from "@/hooks/use-permissions";
import { PERMISSION } from "@/types/authorization";

/**
 * The AI Assistant page: conversations beside the open one.
 *
 * **Two permissions, checked separately**, because the API checks them
 * separately: `ai:chat` opens the workspace and reads a transcript, and sending a
 * message needs `ai:ask` as well. A user holding only the first sees their
 * conversations and is told plainly that they cannot ask new questions — which is
 * better than a composer that answers 403 on submit.
 *
 * **Which conversation is open is component state, not a route.** A conversation
 * identifier in the URL would be written to the browser's history and to the
 * `Referer` header of anything the page loads next — the same three logs the API
 * refuses to put a question into by making search and messaging POSTs. The thread
 * is a user's private working material, and its identifier is one keystroke from
 * being pasted into a support ticket.
 *
 * **On a narrow screen the list becomes a drawer**, per `ui-context.md`: the
 * sidebar collapses and the assistant is a slide-over rather than a column, so a
 * phone shows the conversation rather than a list of names.
 */
export function AssistantWorkspace({
  /** Pins the whole workspace to one case, for the case workspace embed. */
  caseId,
}: {
  caseId?: string;
} = {}) {
  const { can, isLoading } = usePermissions();
  const [chosenId, setChosenId] = React.useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = React.useState(false);
  const create = useCreateConversation();

  const conversations = useConversations(caseId ? { caseId } : {});

  // The most recent conversation is open on arrival, so the screen starts with
  // work in progress rather than with an empty panel and an instruction.
  //
  // **Derived rather than synchronized by an effect.** An effect that set the
  // selection when the list arrived would cost a second render and would fight
  // with the user's own choice on every refetch; falling back only when nothing
  // has been chosen expresses the same intent as a plain expression. It also
  // gives deletion its behaviour for free: the list clears the choice, and the
  // next-most-recent thread opens.
  const selectedId = chosenId ?? conversations.data?.items[0]?.id ?? null;

  const startConversation = React.useCallback(() => {
    create.mutate(
      { ...(caseId ? { caseId } : {}) },
      {
        onSuccess: (conversation) => {
          setChosenId(conversation.id);
          setDrawerOpen(false);
        },
      },
    );
  }, [caseId, create]);

  const select = React.useCallback((id: string) => {
    // An empty identifier is how the list reports "the open one was deleted":
    // clearing the choice falls back to the most recent remaining thread.
    setChosenId(id || null);
    setDrawerOpen(false);
  }, []);

  if (isLoading) {
    return (
      <Card>
        <CardContent className="flex min-h-96 items-center justify-center py-10">
          <span className="text-sm text-muted-foreground">Loading…</span>
        </CardContent>
      </Card>
    );
  }

  if (!can(PERMISSION.aiChat)) {
    return <AccessDenied />;
  }

  const canAsk = can(PERMISSION.aiAsk);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {!canAsk ? (
        <p className="rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground">
          You can read your conversations, but your role does not allow asking the AI
          assistant new questions.
        </p>
      ) : null}

      <div className="flex min-h-0 flex-1 gap-4">
        <aside className="hidden w-72 shrink-0 flex-col lg:flex">
          <ConversationList
            selectedId={selectedId}
            onSelect={select}
            onCreate={startConversation}
            {...(caseId ? { caseId } : {})}
            className="min-h-0"
          />
        </aside>

        <div className="flex min-h-0 flex-1 flex-col gap-3">
          <div className="lg:hidden">
            <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
              <SheetTrigger asChild>
                <Button type="button" variant="outline" size="sm">
                  <PanelLeft className="h-4 w-4" aria-hidden="true" />
                  Conversations
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-80 p-4">
                <SheetTitle className="mb-3 flex items-center gap-2 text-base">
                  <Bot className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                  Conversations
                </SheetTitle>
                <ConversationList
                  selectedId={selectedId}
                  onSelect={select}
                  onCreate={startConversation}
                  {...(caseId ? { caseId } : {})}
                />
              </SheetContent>
            </Sheet>
          </div>

          <AssistantChat
            conversationId={selectedId}
            canSend={canAsk}
            {...(caseId ? { caseId } : {})}
            className="min-h-96"
          />
        </div>
      </div>
    </div>
  );
}
