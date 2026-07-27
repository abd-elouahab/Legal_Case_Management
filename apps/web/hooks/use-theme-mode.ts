"use client";

import { useTheme } from "next-themes";

export type ThemeMode = "light" | "dark";

interface ThemeModeControls {
  /** The resolved theme actually applied to the document. */
  theme: ThemeMode;
  /** Whether the app currently forces a single theme (dark-only for now). */
  isForced: boolean;
  /** Set the theme. A no-op while the theme is forced. */
  setTheme: (mode: ThemeMode) => void;
}

/**
 * Theme state hook.
 *
 * Thin wrapper over next-themes that centralizes theme access for the shell.
 * The platform is currently dark-only (see `00-design-system.md`): the root
 * layout applies `forcedTheme="dark"`, so `isForced` is `true` and `setTheme`
 * has no effect. The wrapper keeps consumers stable for when a light theme is
 * reintroduced — only this hook and the provider change.
 */
export function useThemeMode(): ThemeModeControls {
  const { resolvedTheme, setTheme, forcedTheme } = useTheme();
  const isForced = Boolean(forcedTheme);
  const theme: ThemeMode = resolvedTheme === "light" ? "light" : "dark";

  return {
    theme,
    isForced,
    setTheme: (mode) => {
      if (!isForced) setTheme(mode);
    },
  };
}
