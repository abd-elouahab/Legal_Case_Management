"use client";

import { Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Suggested next questions.
 *
 * The spec's "Suggested Follow-Up Questions": *"After a successful response,
 * generate a small number of suggested follow-up questions"*, relevant to the
 * retrieved context and the conversation topic, and never inventing unsupported
 * facts.
 *
 * Three decisions worth stating, and none is cosmetic:
 *
 * * **choosing one fills the box, it does not send.** A suggestion is a starting
 *   point a professional may want to narrow before asking — and one click that
 *   silently spends a model call, on a metered key, is not a shortcut anybody
 *   asked for.
 * * **they appear only under the last answer.** Suggestions belong to the end of
 *   a thread; drawn under every answer in a long transcript they would be a
 *   column of stale prompts about questions that were already followed up.
 * * **nothing renders when the list is empty**, which is what happens when an
 *   answer was not grounded, when the deployment has suggestions turned off, and
 *   when the suggestion call failed. All three are the same to a reader — there
 *   is nothing worth asking next — and a "no suggestions" placeholder would be a
 *   report about the platform's internals.
 */
export function FollowUpSuggestions({
  suggestions,
  onSelect,
  disabled = false,
}: {
  suggestions: string[];
  onSelect: (question: string) => void;
  disabled?: boolean;
}) {
  if (suggestions.length === 0) return null;

  return (
    <section className="flex flex-col gap-2" aria-label="Suggested follow-up questions">
      <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Sparkles className="h-4 w-4" aria-hidden="true" />
        Ask next
      </p>
      <ul className="flex flex-wrap gap-2">
        {suggestions.map((suggestion) => (
          <li key={suggestion}>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={disabled}
              onClick={() => onSelect(suggestion)}
              className="h-auto whitespace-normal py-1.5 text-left text-xs"
            >
              <span dir="auto">{suggestion}</span>
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}
