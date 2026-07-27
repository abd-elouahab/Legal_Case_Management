import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Standard content wrapper for protected pages.
 *
 * Applies the shared max-width, horizontal centering, and page padding so every
 * feature page aligns consistently inside the app shell. Server Component —
 * purely presentational.
 */
export function PageContainer({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "mx-auto flex w-full max-w-7xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8",
        className,
      )}
    >
      {children}
    </div>
  );
}
