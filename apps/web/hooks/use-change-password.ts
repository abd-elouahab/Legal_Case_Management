"use client";

import * as React from "react";

import { changePassword as changePasswordRequest } from "@/lib/api/auth";
import { ApiError, NetworkError } from "@/lib/api/errors";
import type { ChangePasswordPayload } from "@/types/auth";

/**
 * Change the current user's password.
 *
 * A successful change revokes every session for the user. The replacement access
 * token is swapped in by `lib/api/auth.ts`, so this device stays signed in and no
 * further action is needed here — but the caller should tell the user that their
 * other devices have been signed out.
 */

/** Translate a failure into a message safe to show a user. */
function toErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "invalid_password":
        return "Your current password is incorrect.";
      case "validation_error":
        return error.details[0]?.message ?? "Check the details you entered.";
      case "invalid_token":
      case "token_expired":
      case "missing_token":
        return "Your session has expired. Sign in again to change your password.";
      default:
        return error.message || "Unable to change your password. Please try again.";
    }
  }

  return "Unable to change your password. Please try again.";
}

export function useChangePassword(): {
  submit: (payload: ChangePasswordPayload) => Promise<boolean>;
  isPending: boolean;
  error: string | null;
  successMessage: string | null;
  reset: () => void;
} {
  const [isPending, setIsPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [successMessage, setSuccessMessage] = React.useState<string | null>(null);

  const submit = React.useCallback(async (payload: ChangePasswordPayload) => {
    setIsPending(true);
    setError(null);
    setSuccessMessage(null);

    try {
      const result = await changePasswordRequest(payload);
      setSuccessMessage(result.message);
      return true;
    } catch (cause) {
      setError(toErrorMessage(cause));
      return false;
    } finally {
      setIsPending(false);
    }
  }, []);

  const reset = React.useCallback(() => {
    setError(null);
    setSuccessMessage(null);
  }, []);

  return { submit, isPending, error, successMessage, reset };
}

export { toErrorMessage as changePasswordErrorMessage };
