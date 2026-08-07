"use client";

import { Bot, Info, TriangleAlert, User as UserIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { CitationList } from "@/components/ai/citation-list";
import { MessageFeedbackControls } from "@/components/ai/message-feedback";
import { cn } from "@/lib/utils";
import type { AssistantCitation, ConversationMessage } from "@/types/assistant";

/**
 * One turn of a conversation.
 *
 * Renders both roles from one component because they are one list: a transcript
 * where the question and the answer were drawn by different components would
 * drift in spacing, alignment, and text direction the first time either changed.
 *
 * Four decisions worth stating:
 *
 * * **an ungrounded answer says so, prominently.** When the documents did not
 *   support an answer the API returns the platform's own sentence with `grounded:
 *   false` and no citations — and this draws a notice rather than letting it look
 *   like an ordinary reply. A reader must never mistake "I found nothing" for an
 *   answer that happens to be short.
 * * **a truncated answer says so too.** An answer cut off at the model's output
 *   ceiling ends mid-thought, and presenting it as complete is the one way a
 *   legal reader could be actively misled by this screen.
 * * **`dir="auto"`** on every message body, so an Arabic answer renders
 *   right-to-left beside a French question without this component detecting
 *   script.
 * * **`whitespace-pre-wrap`, and no Markdown rendering.** The answer is displayed
 *   as the model wrote it. Interpreting it as markup would mean deciding what to
 *   do with a `[1]` citation marker, a `#` from a statute reference, or an
 *   underscore in a filename — and rendering generated text as HTML in a legal
 *   platform is a much larger decision than it looks.
 */
export function ChatMessage({
  message,
  conversationId,
}: {
  message: ConversationMessage;
  conversationId: string;
}) {
  const isUser = message.role === "user";

  return (
    <article
      className={cn("flex gap-3", isUser ? "flex-row-reverse" : "flex-row")}
      aria-label={isUser ? "Your message" : "Assistant answer"}
    >
      <span
        className={cn(
          "mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full",
          isUser ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
        )}
        aria-hidden="true"
      >
        {isUser ? <UserIcon className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
      </span>

      <div className={cn("flex min-w-0 flex-col gap-2", isUser ? "items-end" : "items-start", "flex-1")}>
        <div
          className={cn(
            "max-w-full rounded-xl px-4 py-3 text-sm leading-relaxed",
            isUser
              ? "bg-primary/10 text-foreground"
              : "border border-border bg-card text-secondary-foreground",
          )}
        >
          <p dir="auto" className="whitespace-pre-wrap break-words">
            {message.content}
          </p>
        </div>

        {!isUser ? (
          <>
            {message.insufficientEvidence ? (
              <p className="flex items-start gap-2 rounded-md border border-[var(--state-warning)]/30 bg-[var(--state-warning)]/5 px-3 py-2 text-xs text-muted-foreground">
                <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  No supporting document was found for this question, so the assistant did
                  not answer from anything. Documents become answerable once their text
                  has been extracted and indexed.
                </span>
              </p>
            ) : null}

            {message.truncated ? (
              <p className="flex items-start gap-2 rounded-md border border-[var(--state-warning)]/30 bg-[var(--state-warning)]/5 px-3 py-2 text-xs text-muted-foreground">
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>
                  This answer reached the length limit and stops early. Ask a narrower
                  question to see the rest.
                </span>
              </p>
            ) : null}

            <CitationList citations={message.citations} />

            <div className="flex w-full flex-wrap items-center justify-between gap-2">
              <MessageFeedbackControls conversationId={conversationId} message={message} />
              <MessageProvenance message={message} />
            </div>
          </>
        ) : null}
      </div>
    </article>
  );
}

/**
 * How an answer was produced, in one quiet line.
 *
 * Shown because this is a *legal* assistant: which model wrote an answer, how
 * long it took, and how many passages it considered are the questions a
 * professional asks when an answer surprises them, and hiding them behind an
 * administrator's monitoring page would make the transcript less trustworthy
 * rather than tidier.
 *
 * `contextTurns` is here rather than in the metrics because it changes how the
 * answer should be *read*: it says the question was interpreted against an
 * earlier one, which is invisible from the answer alone.
 */
function MessageProvenance({ message }: { message: ConversationMessage }) {
  const parts: string[] = [];

  if (message.retrievedCount !== null) {
    parts.push(`${message.retrievedCount} passage${message.retrievedCount === 1 ? "" : "s"}`);
  }
  if (message.durationMs !== null) {
    parts.push(`${(message.durationMs / 1000).toFixed(1)}s`);
  }
  if (message.model) parts.push(message.model);

  if (parts.length === 0 && message.contextTurns === 0) return null;

  return (
    <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      {message.contextTurns > 0 ? (
        <Badge
          variant="outline"
          title="This question was read against what was asked before it"
        >
          Follow-up
        </Badge>
      ) : null}
      {parts.length > 0 ? <span>{parts.join(" · ")}</span> : null}
    </p>
  );
}

/**
 * The answer that is still being written.
 *
 * A separate component from {@link ChatMessage} on purpose: a pending answer has
 * no identifier, no citations yet, and cannot be rated or copied, so giving it
 * the stored message's props would mean a dozen optional fields on the type that
 * exists to describe a *stored* message.
 *
 * The three states it shows are the three the API actually reports, and telling
 * them apart is the whole value of streaming: **searching** (retrieval has not
 * finished), **read N passages** (it has, and the model has not started), and the
 * text itself once it begins to arrive.
 */
export function PendingMessage({
  question,
  answer,
  retrievedCount,
  streaming,
  citations,
}: {
  question: string;
  answer: string;
  retrievedCount: number | null;
  streaming: boolean;
  citations: AssistantCitation[];
}) {
  return (
    <>
      <article className="flex flex-row-reverse gap-3" aria-label="Your message">
        <span
          className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary"
          aria-hidden="true"
        >
          <UserIcon className="h-4 w-4" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col items-end">
          <div className="max-w-full rounded-xl bg-primary/10 px-4 py-3 text-sm leading-relaxed text-foreground">
            <p dir="auto" className="whitespace-pre-wrap break-words">
              {question}
            </p>
          </div>
        </div>
      </article>

      <article className="flex gap-3" aria-label="Assistant answer in progress">
        <span
          className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground"
          aria-hidden="true"
        >
          <Bot className="h-4 w-4" />
        </span>

        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="max-w-full rounded-xl border border-border bg-card px-4 py-3 text-sm leading-relaxed text-secondary-foreground">
            {answer ? (
              <p dir="auto" className="whitespace-pre-wrap break-words">
                {answer}
                {streaming ? (
                  <span
                    className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-muted-foreground align-text-bottom"
                    aria-hidden="true"
                  />
                ) : null}
              </p>
            ) : (
              <p className="flex items-center gap-2 text-muted-foreground" aria-live="polite">
                <span className="flex gap-1" aria-hidden="true">
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:-0.3s]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground [animation-delay:-0.15s]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground" />
                </span>
                {retrievedCount === null
                  ? "Searching your documents…"
                  : retrievedCount === 0
                    ? "No matching passages found…"
                    : `Read ${retrievedCount} passage${retrievedCount === 1 ? "" : "s"} — writing the answer…`}
              </p>
            )}
          </div>

          <CitationList citations={citations} />
        </div>
      </article>
    </>
  );
}
