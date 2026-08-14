"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { decodeValidationMessage } from "@/lib/validation/messages";

/**
 * Rendering a form field's validation message in the reader's language.
 *
 * The other half of `lib/validation/messages.ts`: a Zod schema emits `vm(key)`,
 * this decodes it and looks the key up. Every form on the platform passes its
 * `errors.<field>?.message` through here on the way to the input.
 *
 * **A message it does not recognise is returned unchanged**, which matters more
 * than it looks. Three kinds of string arrive at a form field: one this platform's
 * schemas produced (translated here), one Zod produced from a built-in rule, and
 * one the *server* produced in a 422 response. The last is written in English by
 * a process with no knowledge of the reader — an honest limitation this hook
 * cannot fix, and passing it through is better than replacing a specific
 * complaint about a specific field with a generic sentence.
 *
 * Returns `undefined` for `undefined`, so it composes with the optional-chaining
 * every form already does: `error={fieldError(errors.title?.message)}`.
 */
export function useFieldError(): (message?: string | null) => string | undefined {
  // No namespace: keys from `vm()` are absolute paths into the catalogue, so a
  // schema shared by several forms does not have to know which one is rendering it.
  const t = useTranslations();

  return React.useCallback(
    (message?: string | null): string | undefined => {
      if (!message) return undefined;

      const decoded = decodeValidationMessage(message);
      if (!decoded) return message;

      return t(decoded.key, decoded.values);
    },
    [t],
  );
}
