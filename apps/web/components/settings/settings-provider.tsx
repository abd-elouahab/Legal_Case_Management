"use client";

import * as React from "react";
import { useTheme } from "next-themes";

import { DEFAULT_LOCALE } from "@/lib/i18n/config";
import { useSession } from "@/hooks/use-session";
import { useSettings } from "@/hooks/use-settings";
import { SETTING_KEY, type ResolvedPreferences, type Setting } from "@/types/settings";

/**
 * The caller's presentation preferences, available to the whole application.
 *
 * `20-settings.md` asks for settings that *"survive logout, login, browser
 * refresh, and device changes"*, and this provider is what makes the last of
 * those true for the ones the shell applies. It loads the caller's settings once
 * a session exists and hands them to every surface that renders something a
 * preference decides.
 *
 * **The theme is stored twice on purpose, and the two copies answer different
 * questions.** `next-themes` keeps its own in `localStorage`, which is what
 * paints the correct palette *before React hydrates* — a server that had to be
 * asked would flash the wrong theme on every page load. The Settings API is the
 * durable copy, which is what makes the choice follow somebody to a new laptop.
 * This provider reconciles them in one direction only: **the server's answer wins
 * once it arrives**, because it is the one somebody chose deliberately and the
 * local copy is a cache of it.
 *
 * The reconciliation is deliberately one-way. Writing `localStorage` back to the
 * API on load would turn a device that has never been to the settings page into a
 * source of truth, and two tabs disagreeing would each keep overwriting the
 * other.
 *
 * **Everything else here is read, never applied by this component.** The language
 * is handed to whatever renders localizable server text (a notification feed asks
 * the API to render in it); the date and time formats and the time zone are used
 * by `useDateTimeFormat`; the AI and dashboard preferences are read by those
 * features' own surfaces. A provider that reached into other features to apply
 * their settings would be the Settings module owning their behaviour, which is
 * exactly what the spec's *"each feature should own its configuration"* rules
 * out.
 *
 * **Defaults are the platform's, not this file's.** Before the query resolves —
 * and for a signed-out visitor, who has no settings — the fallbacks below are
 * used. They mirror `core/settings.py`'s built-in defaults rather than a second
 * opinion, and they are only ever visible for the moment before an answer
 * arrives.
 */

/** What the shell renders with before the caller's settings have loaded. */
const FALLBACK: ResolvedPreferences = {
  theme: "dark",
  language: DEFAULT_LOCALE,
  timezone: "UTC",
  dateFormat: "day_month_year",
  timeFormat: "hour_24",
  aiResponseLength: "balanced",
  aiStreaming: true,
  aiCitations: "list",
  dashboardRange: "last_30_days",
  dashboardWidgets: [],
  // Nobody has chosen anything yet, by definition — which is what lets the locale
  // provider treat the very first load as a "first login" and adopt the browser's
  // language. See `ResolvedPreferences.languageIsDefault`.
  languageIsDefault: true,
};

interface SettingsContextValue {
  preferences: ResolvedPreferences;
  /** Whether the caller's own settings have arrived, or these are fallbacks. */
  isLoaded: boolean;
}

const SettingsContext = React.createContext<SettingsContextValue>({
  preferences: FALLBACK,
  isLoaded: false,
});

/** Read one setting out of the collection, narrowing it to the shape expected. */
function pick<T extends ResolvedPreferences[keyof ResolvedPreferences]>(
  settings: Setting[],
  key: string,
  fallback: T,
): T {
  const found = settings.find((setting) => setting.key === key);
  if (found === undefined) return fallback;
  // A value's shape is declared by its own definition on the server, which
  // validates every write against it — so a mismatch here means the two sides
  // genuinely disagree, and falling back is better than rendering a checkbox
  // bound to a string.
  return (typeof found.value === typeof fallback ? found.value : fallback) as T;
}

function resolve(settings: Setting[] | undefined): ResolvedPreferences {
  if (!settings) return FALLBACK;

  const theme = pick(settings, SETTING_KEY.theme, FALLBACK.theme);
  return {
    theme: theme === "light" || theme === "dark" || theme === "system" ? theme : "dark",
    language: pick(settings, SETTING_KEY.language, FALLBACK.language),
    timezone: pick(settings, SETTING_KEY.timezone, FALLBACK.timezone),
    dateFormat: pick(settings, SETTING_KEY.dateFormat, FALLBACK.dateFormat),
    timeFormat: pick(settings, SETTING_KEY.timeFormat, FALLBACK.timeFormat),
    aiResponseLength: pick(
      settings,
      SETTING_KEY.aiResponseLength,
      FALLBACK.aiResponseLength,
    ),
    aiStreaming: pick(settings, SETTING_KEY.aiStreaming, FALLBACK.aiStreaming),
    aiCitations: pick(settings, SETTING_KEY.aiCitations, FALLBACK.aiCitations),
    dashboardRange: pick(settings, SETTING_KEY.dashboardRange, FALLBACK.dashboardRange),
    dashboardWidgets: (() => {
      const found = settings.find((s) => s.key === SETTING_KEY.dashboardWidgets);
      return Array.isArray(found?.value) ? found.value : FALLBACK.dashboardWidgets;
    })(),
    // `isDefault` is the server's own statement that this account has **no stored
    // row** for the setting, which is the platform's representation of "has not
    // chosen". A setting absent from the collection entirely is treated the same
    // way, because it means the same thing.
    languageIsDefault:
      settings.find((setting) => setting.key === SETTING_KEY.language)?.isDefault ??
      true,
  };
}

export function SettingsProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useSession();
  const { data } = useSettings();
  const { setTheme } = useTheme();

  // A signed-out visitor has no settings to load, and asking for them would be a
  // 401 on the login page. The query is still declared unconditionally — hooks
  // cannot be conditional — and simply has nothing to report until a session
  // exists, because `apiRequest` needs a token it does not have.
  const preferences = React.useMemo(
    () => (isAuthenticated ? resolve(data?.settings) : FALLBACK),
    [isAuthenticated, data],
  );
  const isLoaded = isAuthenticated && data !== undefined;

  // The server's answer wins over whatever `localStorage` held. One-way and
  // idempotent: `setTheme` with the value already in force is a no-op, so this
  // does not fight a user toggling the theme in another tab.
  React.useEffect(() => {
    if (isLoaded) setTheme(preferences.theme);
  }, [isLoaded, preferences.theme, setTheme]);

  const value = React.useMemo(
    () => ({ preferences, isLoaded }),
    [preferences, isLoaded],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

/**
 * The caller's presentation preferences.
 *
 * Safe to call anywhere below `Providers`, including on a page a signed-out
 * visitor can reach: the fallbacks are the platform's own defaults, so a
 * component never has to branch on whether settings have loaded unless it wants
 * to.
 */
export function useUserPreferences(): SettingsContextValue {
  return React.useContext(SettingsContext);
}
