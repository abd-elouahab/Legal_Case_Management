"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { login as loginRequest } from "@/lib/api/auth";
import { useErrorMessage, type ErrorCodeMap } from "@/hooks/use-error-message";
import { DEFAULT_AUTHENTICATED_ROUTE, safeRedirectTarget } from "@/lib/routes";
import { useSessionStore } from "@/stores/session-store";
import type { LoginCredentials } from "@/types/auth";

/**
 * Login mutation with loading and error state.
 *
 * Maps API error codes onto user-facing messages here (rather than in the form)
 * so the component stays presentational and the mapping is testable in isolation.
 */

/**
 * Translate a failure into a message safe to show a user, in their language.
 *
 * Invalid credentials deliberately stay vague — the API does not distinguish an
 * unknown email from a wrong password, and neither should the UI.
 *
 * **The lockout message used to be the server's, verbatim**, on the grounds that
 * only the server knows how long the wait is. `21-localization.md` ends that: the
 * sign-in screen is the one screen somebody reaches *before* the platform knows
 * who they are, so it renders in the language stored on the device — and an
 * English lockout sentence there is the first thing an Arabic reader would see.
 * The remaining wait is in the `Retry-After` header and in the log; the sentence
 * is the platform's.
 */
const LOGIN_ERRORS: ErrorCodeMap = {
  invalid_credentials: "invalidCredentials",
  account_disabled: "accountDisabled",
  too_many_login_attempts: "tooManyAttempts",
};

function useLoginErrorMessage(): (error: unknown) => string {
  return useErrorMessage("auth.errors", LOGIN_ERRORS);
}

/**
 * Read the `?next=` redirect target from the current URL.
 *
 * Deliberately reads `window.location` at submit time instead of using
 * `useSearchParams()`: that hook is a dynamic API, and calling it here would opt
 * the login form out of prerendering, so the form's inputs would be missing from
 * the served HTML until JavaScript hydrated. The value is only needed once, at
 * submit, so there is nothing to gain from making it reactive.
 */
function readRedirectTarget(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("next");
}

export function useLogin(): {
  submit: (credentials: LoginCredentials) => Promise<void>;
  isPending: boolean;
  error: string | null;
  reset: () => void;
} {
  const router = useRouter();
  const setSession = useSessionStore((state) => state.setSession);
  const errorMessage = useLoginErrorMessage();
  const [isPending, setIsPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const submit = React.useCallback(
    async (credentials: LoginCredentials) => {
      setIsPending(true);
      setError(null);

      try {
        const session = await loginRequest(credentials);
        setSession(session.user);
        // Return the user to wherever the proxy intercepted them, falling back to
        // the dashboard. `replace` (not `push`) so Back does not return here.
        const target = safeRedirectTarget(readRedirectTarget());
        router.replace(target ?? DEFAULT_AUTHENTICATED_ROUTE);
      } catch (cause) {
        setError(errorMessage(cause));
        setIsPending(false);
      }
      // On success `isPending` stays true through the navigation, keeping the
      // button disabled so the form cannot be submitted twice.
    },
    [errorMessage, router, setSession],
  );

  const reset = React.useCallback(() => setError(null), []);

  return { submit, isPending, error, reset };
}

export { useLoginErrorMessage };
