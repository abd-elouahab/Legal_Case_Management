"use client";

import * as React from "react";

/**
 * Global error boundary.
 *
 * Only catches errors thrown by the root layout itself, so it must render its
 * own `<html>`/`<body>`. Kept intentionally dependency-free (no providers or
 * theme context are guaranteed available here). Dark background is inlined to
 * match the platform surface.
 *
 * **The one screen on the platform that is not translated, and structurally so.**
 * It is the boundary that catches a failure of the root layout — the layout that
 * mounts `LocaleProvider` — so by the time this renders there is no catalogue,
 * no locale, and no guarantee that a dynamic import would resolve. Reaching for
 * `useTranslations` here would replace a legible English sentence with a second
 * crash. `app/not-found.tsx` is inside that layout and *is* translated; the
 * difference between the two files is exactly this one.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <html lang="en" dir="ltr">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: "#0f172a",
          color: "#f8fafc",
          fontFamily: "system-ui, sans-serif",
          textAlign: "center",
          padding: "1rem",
        }}
      >
        <div style={{ maxWidth: "28rem" }}>
          <h1 style={{ fontSize: "1.25rem", fontWeight: 600 }}>
            Something went wrong
          </h1>
          <p style={{ color: "#94a3b8", fontSize: "0.875rem" }}>
            An unexpected error occurred. Please try again.
          </p>
          <button
            type="button"
            onClick={reset}
            style={{
              marginTop: "1rem",
              borderRadius: "0.375rem",
              border: "1px solid #334155",
              backgroundColor: "transparent",
              color: "#f8fafc",
              padding: "0.5rem 1rem",
              fontSize: "0.875rem",
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
