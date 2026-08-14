"use client";

import { useTheme } from "next-themes";

/** The three choices `20-settings.md`'s Appearance section names. */
export type ThemePreference = "light" | "dark" | "system";

/** The theme actually painted on the document. `system` resolves to one of these. */
export type ResolvedTheme = "light" | "dark";

interface ThemeModeControls {
  /** What the user chose — including `system`, which is a real choice. */
  preference: ThemePreference;
  /** The theme actually applied to the document. */
  theme: ResolvedTheme;
  /** Whether the provider has read the stored preference yet. */
  isReady: boolean;
  /** Set the theme. */
  setTheme: (mode: ThemePreference) => void;
}

/**
 * Theme state hook.
 *
 * Thin wrapper over next-themes that centralizes theme access for the shell.
 *
 * **`preference` and `theme` are deliberately two values.** A settings page has
 * to show which of the three buttons is selected, and `system` is a selection —
 * while everything that needs to *draw* something (an icon, a chart colour) needs
 * to know which palette is actually on screen. Collapsing them would make
 * "System" impossible to render as chosen.
 *
 * **`isReady` exists because the first render has no answer.** The theme lives in
 * `localStorage` and is unreadable on the server, so next-themes reports
 * `undefined` until it has mounted. Rendering a settings control from that would
 * show "Light" selected for one frame on a dark-mode account — so callers gate on
 * this rather than defaulting.
 *
 * The *durable* copy of this preference is the Settings API, not `localStorage`:
 * see `components/settings/settings-provider.tsx` for how the two are reconciled
 * and why both exist.
 */
export function useThemeMode(): ThemeModeControls {
  const { theme, resolvedTheme, setTheme } = useTheme();

  return {
    preference: theme === "light" || theme === "dark" ? theme : "system",
    theme: resolvedTheme === "light" ? "light" : "dark",
    // `theme` is `undefined` until next-themes has read storage on the client.
    isReady: theme !== undefined,
    setTheme,
  };
}
