"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { ApiError, NetworkError } from "@/lib/api/errors";

/**
 * Turning a failed request into a sentence the reader can read.
 *
 * **One hook rather than one function per module**, and the reason is
 * `21-localization.md`'s *"no user-facing text should be hardcoded"* meeting an
 * awkward fact: the API returns a `message` alongside its `code`, that message is
 * written in English by a Python process that has never heard of the caller's
 * language preference, and until now every `*ErrorMessage` helper on the frontend
 * either returned an English literal of its own or passed the server's through.
 * Both are the same defect — a screen that is Arabic everywhere except when
 * something goes wrong.
 *
 * **A code is translated; a message is never displayed.** The platform already
 * decided that clients branch on `error.code` rather than on prose
 * (`lib/api/errors.ts` says so in its own docstring), so this is that rule carried
 * one step further: the code selects a key, the key selects a sentence, and the
 * server's `message` reaches the console and the log and nothing else. A code with
 * no key of its own falls through to {@link SHARED_CODES} and then to a generic
 * sentence, which is the spec's fallback strategy — *"use the default language;
 * if still unavailable, display a meaningful fallback"* — applied to error copy.
 *
 * A module passes the codes that are **its own** and inherits the rest:
 *
 * ```ts
 * const CASE_ERRORS = { case_not_found: "notFound", case_number_taken: "numberTaken" };
 * const errorMessage = useErrorMessage("cases.errors", CASE_ERRORS);
 * ```
 *
 * The map must be a module-level constant, not an object literal in the render:
 * the returned callback is memoized on it, and a fresh object each render would
 * make it a new function each render.
 */

/** API error code → key inside the module's own `errors` namespace. */
export type ErrorCodeMap = Readonly<Record<string, string>>;

/**
 * Codes any endpoint on the platform can return, and their shared sentences.
 *
 * These live in `errors.*` rather than in a feature's namespace because the
 * sentence is the same wherever it appears: a caller refused a case and a caller
 * refused a report are being told the same thing about their permissions, and
 * writing it twice is how the two start to differ.
 */
const SHARED_CODES: ErrorCodeMap = {
  forbidden: "forbidden",
  not_found: "notFound",
  unauthorized: "unauthorized",
  token_expired: "sessionExpired",
  invalid_token: "sessionExpired",
  validation_error: "validation",
  rate_limited: "rateLimited",
  service_unavailable: "unavailable",
  unexpected_error: "generic",
};

/**
 * A function that renders any thrown value as one localized sentence.
 *
 * Stable across renders as long as `codes` is, so it is safe in a dependency
 * array and safe to call during render.
 */
export function useErrorMessage(
  namespace: string,
  codes: ErrorCodeMap = {},
): (error: unknown) => string {
  const t = useTranslations(namespace);
  const tShared = useTranslations("errors");

  return React.useCallback(
    (error: unknown): string => {
      // Not an API failure at all: the request never left the browser. Its own
      // message is a literal from `lib/api/errors.ts`, so it is as untranslated as
      // the server's would be.
      if (error instanceof NetworkError) return tShared("network");

      if (error instanceof ApiError) {
        const own = codes[error.code];
        if (own) return t(own);

        const shared = SHARED_CODES[error.code];
        if (shared) return tShared(shared);
      }

      return tShared("generic");
    },
    [codes, t, tShared],
  );
}

/**
 * The same thing for a bare code, with no `Error` around it.
 *
 * Widgets, delivery rows, OCR runs, indexing runs, and report runs each persist a
 * failure **reason** rather than raising one, so their codes arrive as a plain
 * string on a successful response. Same translation, same fallback, no `Error` to
 * unwrap.
 */
export function useCodeMessage(
  namespace: string,
  codes: ErrorCodeMap = {},
): (code: string | null | undefined) => string {
  const t = useTranslations(namespace);
  const tShared = useTranslations("errors");

  return React.useCallback(
    (code: string | null | undefined): string => {
      if (code) {
        const own = codes[code];
        if (own) return t(own);

        const shared = SHARED_CODES[code];
        if (shared) return tShared(shared);
      }
      return tShared("generic");
    },
    [codes, t, tShared],
  );
}
