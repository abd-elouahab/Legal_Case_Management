"use client";

import * as React from "react";

import { ErrorState } from "@/components/shared/error-state";

/**
 * Root error boundary.
 *
 * Catches errors thrown outside the protected shell (renders within the root
 * layout). Uses the shared {@link ErrorState}; raw error details are never
 * shown to the user.
 */
export default function RootError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    // Placeholder for real error reporting (e.g. Sentry) added later.
    console.error(error);
  }, [error]);

  return (
    <div className="flex min-h-svh items-center justify-center p-4">
      <ErrorState onRetry={reset} className="w-full max-w-md" />
    </div>
  );
}
