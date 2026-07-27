"use client";

import * as React from "react";
import { ThemeProvider as NextThemesProvider } from "next-themes";

/**
 * Application theme provider.
 *
 * The platform is dark-mode-only per
 * context/feature-specs/00-design-system.md. The provider is kept as a thin
 * wrapper around next-themes so a light theme could be reintroduced later
 * without touching consumers, but the current configuration (see the root
 * layout) forces the dark theme.
 */
export function ThemeProvider({
  children,
  ...props
}: React.ComponentProps<typeof NextThemesProvider>) {
  return <NextThemesProvider {...props}>{children}</NextThemesProvider>;
}
