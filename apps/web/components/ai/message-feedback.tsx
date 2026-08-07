"use client";

import * as React from "react";
import { Check, Copy, ThumbsDown, ThumbsUp } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useSubmitFeedback, useWithdrawFeedback } from "@/hooks/use-assistant";
import type { ConversationMessage, FeedbackRating } from "@/types/assistant";

/**
 * Rate an answer, and copy it.
 *
 * The spec's "Feedback" section requires *helpful* and *not helpful* at minimum,
 * and its "User Experience" section requires a copy control. Both sit on the same
 * row because they are the same gesture — what a reader does *with* an answer
 * once they have read it.
 *
 * **Pressing the rating you already gave withdraws it.** A toggle rather than a
 * one-way switch, because a rating is an opinion its author is entitled to take
 * back, and the alternative — a rating that can be changed but never removed —
 * leaves a mis-click permanently in the evaluation data.
 *
 * **Rating never alters the answer.** It is stored separately from the transcript
 * server-side, and this component reads `message.feedback` rather than holding
 * its own idea of the state, so what is shown is always what was actually
 * recorded.
 *
 * Both buttons carry a label as well as an icon (`aria-pressed`, `aria-label`):
 * a thumb glyph alone is not an accessible control, and the pressed state must
 * not be communicated by colour alone.
 */
export function MessageFeedbackControls({
  conversationId,
  message,
}: {
  conversationId: string;
  message: ConversationMessage;
}) {
  const submit = useSubmitFeedback();
  const withdraw = useWithdrawFeedback();
  const [copied, setCopied] = React.useState(false);

  const current = message.feedback?.rating ?? null;
  const busy = submit.isPending || withdraw.isPending;

  function rate(rating: FeedbackRating) {
    if (busy) return;

    if (current === rating) {
      withdraw.mutate({ conversationId, messageId: message.id });
      return;
    }
    submit.mutate({ conversationId, messageId: message.id, rating });
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      // Reverts on its own: a "Copied" label that stayed forever would stop
      // meaning "this just happened", which is the only thing it says.
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // A clipboard permission refusal is not worth an error banner over an
      // answer the user can still select and copy by hand.
      setCopied(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-1">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2 text-xs"
        onClick={() => rate("helpful")}
        disabled={busy}
        aria-pressed={current === "helpful"}
        aria-label={current === "helpful" ? "Remove helpful rating" : "Rate this answer helpful"}
      >
        <ThumbsUp
          className={`h-4 w-4 ${current === "helpful" ? "text-[var(--state-success)]" : ""}`}
          aria-hidden="true"
        />
        Helpful
      </Button>

      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2 text-xs"
        onClick={() => rate("not_helpful")}
        disabled={busy}
        aria-pressed={current === "not_helpful"}
        aria-label={
          current === "not_helpful"
            ? "Remove not helpful rating"
            : "Rate this answer not helpful"
        }
      >
        <ThumbsDown
          className={`h-4 w-4 ${current === "not_helpful" ? "text-[var(--state-warning)]" : ""}`}
          aria-hidden="true"
        />
        Not helpful
      </Button>

      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-7 gap-1 px-2 text-xs"
        onClick={() => void copy()}
        aria-label="Copy this answer"
      >
        {copied ? (
          <Check className="h-4 w-4 text-[var(--state-success)]" aria-hidden="true" />
        ) : (
          <Copy className="h-4 w-4" aria-hidden="true" />
        )}
        {copied ? "Copied" : "Copy"}
      </Button>

      {/* Announced rather than only drawn, so the outcome of the click reaches a
          screen reader as well as an eye. */}
      <span aria-live="polite" className="sr-only">
        {current === "helpful"
          ? "Rated helpful."
          : current === "not_helpful"
            ? "Rated not helpful."
            : ""}
      </span>
    </div>
  );
}
