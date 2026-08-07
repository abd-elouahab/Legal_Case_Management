"use client";

import * as React from "react";
import { Bot, RotateCcw, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { ChatComposer } from "@/components/ai/chat-composer";
import { ChatMessage, PendingMessage } from "@/components/ai/chat-message";
import { FollowUpSuggestions } from "@/components/ai/follow-up-suggestions";
import {
  assistantErrorMessage,
  isAssistantUnavailable,
  lastAnswer,
  useChatSession,
  useConversation,
} from "@/hooks/use-assistant";
import { cn } from "@/lib/utils";
import type { MessageInput } from "@/types/assistant";

/**
 * One conversation, end to end.
 *
 * The transcript, the composer, the suggested follow-ups, and the error state —
 * everything a reader does inside a single thread. The list of conversations is
 * beside it rather than in it, because a case workspace embeds this without one.
 *
 * Five behaviours are deliberate, and each of them follows from what the API
 * actually promises:
 *
 * * **the question appears the instant it is sent**, before the server has
 *   confirmed anything. A composer that cleared and then showed nothing for four
 *   seconds reads as a failure, and the pending turn is discarded the moment the
 *   stored transcript arrives — so the answer is never drawn twice.
 * * **the answer streams when the platform allows it**, and falls back to
 *   arriving whole when it does not. The two paths are indistinguishable here
 *   except by how many times the text changes, which is the point.
 * * **a failure keeps the question on screen and offers a retry.** Retrying is a
 *   button rather than something automatic: a 503 means retrieval or the model is
 *   down and an immediate retry fails the same way, while a request that *did*
 *   reach the model would append a second answer.
 * * **suggestions appear under the last answer only**, and choosing one fills the
 *   box rather than sending it.
 * * **the transcript scrolls to the newest turn** when it grows, because a chat
 *   that leaves the reader at the top of a thread makes them scroll to find the
 *   thing they just asked for.
 */
export function AssistantChat({
  conversationId,
  /** Fixes every question in this thread to one case, for the case workspace. */
  caseId,
  /**
   * Whether this caller may ask new questions. `false` for someone holding
   * `ai:chat` but not `ai:ask` — they read their history, and the composer is
   * disabled rather than answering 403 on submit.
   */
  canSend = true,
  className,
}: {
  conversationId: string | null;
  caseId?: string;
  canSend?: boolean;
  className?: string;
}) {
  const conversation = useConversation(conversationId);
  const session = useChatSession(conversationId);
  const [draft, setDraft] = React.useState("");
  const bottomRef = React.useRef<HTMLDivElement>(null);

  const messages = conversation.data?.messages ?? [];
  const answer = lastAnswer(messages);

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, session.pending?.answer]);

  const send = React.useCallback(
    (content: string) => {
      const input: MessageInput = { content, ...(caseId ? { caseId } : {}) };
      session.send(input);
    },
    [caseId, session],
  );

  if (!conversationId) {
    return (
      <div className={cn("flex flex-1 items-center justify-center", className)}>
        <EmptyState
          icon={Bot}
          title="No conversation open"
          description="Start a new conversation to ask questions about the documents on your cases. Every answer cites the file and page it came from."
        />
      </div>
    );
  }

  return (
    <div className={cn("flex min-h-0 flex-1 flex-col gap-4", className)}>
      <div
        className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto pr-1"
        role="log"
        aria-label="Conversation"
        aria-live="polite"
      >
        {conversation.isLoading ? (
          <div className="flex flex-col gap-4" aria-busy="true">
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-24 rounded-xl" />
            ))}
          </div>
        ) : conversation.isError ? (
          <p
            role="alert"
            className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
          >
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <span>{assistantErrorMessage(conversation.error)}</span>
          </p>
        ) : messages.length === 0 && !session.pending ? (
          <EmptyState
            icon={Bot}
            title="Ask your first question"
            description="Answers are built only from the documents on cases you have access to, and every statement carries the file and page it came from. If the documents do not support an answer, the assistant says so rather than guessing."
          />
        ) : (
          <>
            {conversation.data?.hasMoreMessages ? (
              <p className="text-center text-xs text-muted-foreground">
                Earlier messages in this conversation are not shown.
              </p>
            ) : null}

            {messages.map((message) => (
              <ChatMessage
                key={message.id}
                message={message}
                conversationId={conversationId}
              />
            ))}

            {session.pending ? (
              <PendingMessage
                question={session.pending.question}
                answer={session.pending.answer}
                retrievedCount={session.pending.retrievedCount}
                streaming={session.pending.streaming}
                citations={session.pending.citations}
              />
            ) : null}
          </>
        )}

        <div ref={bottomRef} />
      </div>

      {session.error ? (
        <div className="flex flex-col gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3">
          <p role="alert" className="flex items-start gap-2 text-sm text-destructive">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <span>
              {assistantErrorMessage(session.error)}
              {isAssistantUnavailable(session.error)
                ? " Your documents and their text are unaffected."
                : null}
            </span>
          </p>
          <div>
            <Button type="button" variant="outline" size="sm" onClick={session.reset}>
              <RotateCcw className="h-4 w-4" aria-hidden="true" />
              Dismiss and try again
            </Button>
          </div>
        </div>
      ) : null}

      {/* Under the last answer only: suggestions belong at the end of a thread,
          and one set per answer would be a column of stale prompts. */}
      {!session.pending && answer && canSend ? (
        <FollowUpSuggestions
          suggestions={answer.suggestions}
          onSelect={setDraft}
          disabled={session.isSending}
        />
      ) : null}

      <ChatComposer
        onSend={send}
        isSending={session.isSending}
        disabled={conversation.isError || !canSend}
        value={draft}
        onValueChange={setDraft}
        placeholder={
          caseId
            ? "Ask about this case's documents…"
            : "Ask about your documents — for example, when is the rent payable?"
        }
      />
    </div>
  );
}
