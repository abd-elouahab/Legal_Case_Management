"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { SendHorizontal } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Spinner } from "@/components/shared/spinner";
import { useFieldError } from "@/hooks/use-field-error";
import { MAX_MESSAGE_LENGTH, messageFormSchema } from "@/lib/validation/assistant";

/**
 * The message box.
 *
 * Four behaviours are deliberate, and each follows from what the API actually
 * costs or promises:
 *
 * * **Enter sends, Shift+Enter breaks the line.** The convention every modern
 *   assistant uses, and the right one here: a legal question is usually one
 *   sentence, and the occasional multi-line one is worth a modifier key.
 * * **the box is never disabled while an answer is in flight**, only the send
 *   button is. Someone who has thought of their next question while reading an
 *   answer must be able to type it; taking the keyboard away mid-thought is the
 *   most annoying thing a chat interface can do.
 * * **it grows with the text, to a ceiling.** A question near the 1000-character
 *   limit is unreadable in three lines, and a box that filled the screen would
 *   push the answer out of it.
 * * **validation happens here and on the server.** This spares a round trip and
 *   puts the message next to the field; it is **not** a security boundary, and a
 *   caller bypassing it reaches exactly the same refusal.
 */
export function ChatComposer({
  onSend,
  disabled = false,
  isSending = false,
  placeholder,
  /** Pre-filled text, used when a suggested follow-up is chosen. */
  value,
  onValueChange,
}: {
  onSend: (content: string) => void;
  disabled?: boolean;
  isSending?: boolean;
  placeholder?: string;
  value?: string;
  onValueChange?: (value: string) => void;
}) {
  const [internal, setInternal] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const t = useTranslations("assistant.composer");
  const fieldError = useFieldError();
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  // Controlled when the parent supplies a value (a suggestion was clicked),
  // uncontrolled otherwise. One component rather than two, because the only
  // difference is where the string lives.
  const content = value ?? internal;

  const setContent = React.useCallback(
    (next: string) => {
      if (onValueChange) onValueChange(next);
      else setInternal(next);
    },
    [onValueChange],
  );

  React.useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`;
  }, [content]);

  function submit() {
    const parsed = messageFormSchema.safeParse({ content });
    if (!parsed.success) {
      setError(fieldError(parsed.error.issues[0]?.message) ?? t("enterQuestion"));
      return;
    }

    setError(null);
    setContent("");
    onSend(parsed.data.content);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    if (!disabled && !isSending) submit();
  }

  const remaining = MAX_MESSAGE_LENGTH - content.length;

  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label htmlFor="assistant-composer" className="sr-only">
        {t("label")}
      </label>

      <div className="flex items-end gap-2 rounded-xl border border-border bg-card p-2">
        <Textarea
          id="assistant-composer"
          ref={textareaRef}
          rows={1}
          value={content}
          maxLength={MAX_MESSAGE_LENGTH}
          onChange={(event) => setContent(event.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          placeholder={placeholder ?? t("placeholder")}
          className="max-h-52 min-h-10 resize-none border-0 bg-transparent shadow-none focus-visible:ring-0"
          aria-invalid={error !== null}
          aria-describedby={error ? "assistant-composer-error" : "assistant-composer-hint"}
        />
        <Button
          type="submit"
          size="icon"
          disabled={disabled || isSending || content.trim().length === 0}
          aria-label={t("send")}
        >
          {isSending ? (
            <Spinner className="h-4 w-4" />
          ) : (
            <SendHorizontal className="h-4 w-4" aria-hidden="true" />
          )}
        </Button>
      </div>

      {error ? (
        <p id="assistant-composer-error" role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : (
        <p
          id="assistant-composer-hint"
          className="flex items-center justify-between gap-2 text-xs text-muted-foreground"
        >
          <span>{t("hint")}</span>
          {/* Shown only when it starts to matter: a counter on an empty box is
              noise, and one at 40 characters remaining is information. */}
          {remaining <= 100 ? (
            <span className="shrink-0 tabular-nums">{remaining}</span>
          ) : null}
        </p>
      )}
    </form>
  );
}
