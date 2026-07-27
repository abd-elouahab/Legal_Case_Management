"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { login as loginRequest } from "@/lib/api/auth";
import { ApiError, NetworkError } from "@/lib/api/errors";
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
 * Translate a failure into a message safe to show a user.
 *
 * Invalid credentials deliberately stay vague — the API does not distinguish an
 * unknown email from a wrong password, and neither should the UI.
 */
function toErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "invalid_credentials":
        return "Incorrect email or password.";
      case "account_disabled":
        return "This account has been disabled. Contact an administrator.";
      case "too_many_login_attempts":
        // The server's message already states how long to wait, and it is the
        // only place that knows the remaining lockout.
        return error.message;
      case "validation_error":
        return error.details[0]?.message ?? "Check the details you entered.";
      default:
        return error.message || "Unable to sign in. Please try again.";
    }
  }

  return "Unable to sign in. Please try again.";
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
        setError(toErrorMessage(cause));
        setIsPending(false);
      }
      // On success `isPending` stays true through the navigation, keeping the
      // button disabled so the form cannot be submitted twice.
    },
    [router, setSession],
  );

  const reset = React.useCallback(() => setError(null), []);

  return { submit, isPending, error, reset };
}

export { toErrorMessage as loginErrorMessage };
