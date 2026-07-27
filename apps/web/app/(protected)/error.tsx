"use client";

import * as React from "react";

import { ErrorState } from "@/components/shared/error-state";
import { PageContainer } from "@/components/layout/page-container";

/**
 * Error boundary for protected pages. Renders the shared {@link ErrorState}
 * inside the app shell with a reset action. Never surfaces raw error details.
 */
export default function ProtectedError({
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
    <PageContainer>
      <ErrorState onRetry={reset} />
    </PageContainer>
  );
}
