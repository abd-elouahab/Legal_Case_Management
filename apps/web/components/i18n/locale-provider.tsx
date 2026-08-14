"use client";

import * as React from "react";
import { NextIntlClientProvider } from "next-intl";

import { useUserPreferences } from "@/components/settings/settings-provider";
import { reportLocalizationProblems } from "@/lib/api/localization";
import {
  DEFAULT_LOCALE,
  browserLocale,
  directionOf,
  localeTag,
  resolveLocale,
  type Locale,
} from "@/lib/i18n/config";
import {
  getServerLocale,
  getStoredLocale,
  storeLocale,
  subscribeToLocale,
} from "@/lib/i18n/locale-store";
import {
  defaultMessages,
  humanizeKey,
  loadMessages,
  type Messages,
} from "@/lib/i18n/messages";
import { useSession } from "@/hooks/use-session";
import { useUpdateSettings } from "@/hooks/use-settings";
import { SETTING_KEY } from "@/types/settings";

/**
 * The language the whole interface is rendered in.
 *
 * `21-localization.md`'s Language Selection section states a priority and this
 * provider is the whole of it:
 *
 * 1. **the user's preference stored in Settings** — the durable answer, which is
 *    what makes a choice follow somebody to a new laptop;
 * 2. **the browser's language, on first sign-in only** — adopted *and stored*, so
 *    it becomes an ordinary preference from the second visit onwards;
 * 3. **the application default**.
 *
 * **The language is stored twice on purpose, exactly as the theme is**, and for
 * the same reason `components/settings/settings-provider.tsx` records: the
 * Settings API is the durable copy, and `localStorage` is what lets the *login
 * screen* — which has no session and therefore no settings — render in the right
 * language instead of flashing English at somebody who has been using the
 * platform in Arabic for a year. The reconciliation is one-way: **the server's
 * answer wins once it arrives.** A device that has never been to the settings
 * page must not become a source of truth, and two tabs disagreeing must not
 * overwrite each other in a loop.
 *
 * **The local copy lives in an external store rather than in this component's
 * state** (`lib/i18n/locale-store.ts`), and that is what keeps the whole
 * resolution out of `setState`-inside-an-effect: a menu selection, the
 * reconciliation below, and the one-off adoption of the browser's language are
 * all *writes to an external system*, which is what an effect is for. It also
 * makes the value survive the remount a language switch causes, and keeps two
 * tabs in step through the `storage` event.
 *
 * **Switching is instant and downloads one file.** The catalogue is a dynamic
 * import cached per locale (`lib/i18n/messages.ts`), so the first switch to
 * Arabic fetches one chunk and switching back fetches nothing. Until it arrives
 * the previous catalogue stays on screen rather than a spinner: a language switch
 * that blanked the page would look like a navigation, and the strings are about
 * to be replaced anyway.
 *
 * **RTL is applied here and nowhere else.** `dir` and `lang` are written onto the
 * document element, so every component below inherits direction from the document
 * rather than deciding it — which is what lets the rest of the application use
 * logical CSS properties and stay direction-agnostic. Writing them from an effect
 * rather than in the root layout is forced by where the answer lives: the
 * preference arrives from an authenticated request, so the server that renders
 * `<html>` does not know it.
 *
 * **A missing translation is reported, never displayed.** `onError` counts it
 * against `POST /localization/report` — batched, deduplicated, and keys only —
 * and `getMessageFallback` renders a humanized form of the key instead of the key
 * itself, which is the spec's *"the application should never expose translation
 * keys to users"*.
 */

/** How long missing keys accumulate before one report is sent, in ms. */
const REPORT_DEBOUNCE_MS = 4000;

interface LocaleContextValue {
  /** The language in force. */
  locale: Locale;
  /** `ltr` or `rtl` — the same value on the document element. */
  direction: "ltr" | "rtl";
  /** The BCP-47 tag for `Intl` work. */
  tag: string;
  /** Whether the catalogue for `locale` has finished loading. */
  isReady: boolean;
  /**
   * Switch language.
   *
   * Applies immediately and persists to Settings when there is a session to
   * persist to — so a signed-out visitor on the login page can still read it in
   * their own language, and a signed-in one does not have to choose twice.
   */
  setLocale: (next: Locale) => void;
}

const LocaleContext = React.createContext<LocaleContextValue>({
  locale: DEFAULT_LOCALE,
  direction: "ltr",
  tag: "en-GB",
  isReady: false,
  setLocale: () => undefined,
});

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useSession();
  const { preferences, isLoaded } = useUserPreferences();
  const updateSettings = useUpdateSettings();

  const [messages, setMessages] = React.useState<Messages>(
    defaultMessages as Messages,
  );
  const [loadedLocale, setLoadedLocale] = React.useState<Locale>(DEFAULT_LOCALE);

  // --- resolution ---------------------------------------------------------- //

  // The device's copy. `null` on the server and on the very first client render,
  // so the markup the server produced and the markup React hydrates agree; the
  // stored value is applied immediately afterwards, before paint.
  const stored = React.useSyncExternalStore(
    subscribeToLocale,
    getStoredLocale,
    getServerLocale,
  );

  // The store is the answer once it has one, because the effect below keeps it in
  // step with the server and a menu selection writes it directly. Until then —
  // a device that has never chosen — the caller's own setting answers, and the
  // application default is the last resort. That ordering *is*
  // `21-localization.md`'s selection chain.
  const locale = resolveLocale(stored, isLoaded ? preferences.language : null);

  // The server's answer wins once it arrives. A write to an external store rather
  // than to React state, one-way and idempotent — so it does not fight a switch
  // made in another tab, and does not cascade a render.
  React.useEffect(() => {
    if (!isLoaded) return;
    storeLocale(resolveLocale(preferences.language));
  }, [isLoaded, preferences.language]);

  // Step 2 of the selection chain: the browser's language, **on first sign-in
  // only**. `isDefault` is what makes "first" precise — an account that has never
  // chosen has no stored row at all, which is the platform's own representation
  // of "has not chosen" and the only honest signal for this. Adopting it *writes*
  // it, so from the next visit it is an ordinary preference and this branch never
  // runs again; without the write, somebody who deliberately chose the platform
  // default would have it silently overridden by their browser on every load.
  const adopted = React.useRef(false);
  React.useEffect(() => {
    if (!isAuthenticated || !isLoaded || adopted.current) return;
    if (!preferences.languageIsDefault) return;

    const fromBrowser = browserLocale();
    if (!fromBrowser || fromBrowser === preferences.language) return;

    adopted.current = true;
    storeLocale(fromBrowser);
    updateSettings.mutate([{ key: SETTING_KEY.language, value: fromBrowser }]);
  }, [
    isAuthenticated,
    isLoaded,
    preferences.languageIsDefault,
    preferences.language,
    updateSettings,
  ]);

  // --- the catalogue ------------------------------------------------------- //

  React.useEffect(() => {
    let cancelled = false;

    loadMessages(locale)
      .then((loaded) => {
        if (cancelled) return;
        setMessages(loaded);
        setLoadedLocale(locale);
      })
      .catch(() => {
        if (cancelled) return;
        // The default catalogue is already on screen and every key resolves
        // against it, so an Arabic reader sees English rather than a blank page —
        // the spec's own fallback. Reported so an operator can see that a chunk
        // is not being served; never thrown, because a page that failed to render
        // over a translation file is a far worse outcome than one in the wrong
        // language.
        setLoadedLocale(DEFAULT_LOCALE);
        void reportLocalizationProblems({
          failures: ["load_failed"],
          catalogue: locale,
          language: locale,
        });
      });

    return () => {
      cancelled = true;
    };
  }, [locale]);

  // --- the document -------------------------------------------------------- //

  const direction = directionOf(locale);

  React.useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.lang = locale;
    document.documentElement.dir = direction;
  }, [locale, direction]);

  // --- reporting ----------------------------------------------------------- //

  // Missing keys are batched rather than sent per render: a table of two hundred
  // rows referencing one absent label would otherwise be two hundred requests.
  // Deduplicated in a set, flushed on a timer, and never re-reported in this
  // session — the metric this feeds counts *distinct* keys, and a loop that kept
  // re-sending the same one would drown it.
  const pending = React.useRef(new Set<string>());
  const reported = React.useRef(new Set<string>());
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = React.useCallback(() => {
    timer.current = null;
    const keys = [...pending.current];
    pending.current.clear();
    if (keys.length === 0) return;
    void reportLocalizationProblems({ missingKeys: keys, language: locale });
  }, [locale]);

  const noteMissing = React.useCallback(
    (key: string) => {
      if (reported.current.has(key)) return;
      reported.current.add(key);
      pending.current.add(key);
      if (timer.current === null) {
        timer.current = setTimeout(flush, REPORT_DEBOUNCE_MS);
      }
    },
    [flush],
  );

  React.useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  // --- switching ----------------------------------------------------------- //

  const setLocale = React.useCallback(
    (next: Locale) => {
      // Applied before the request, so the interface changes on the click rather
      // than on the round trip — `21-localization.md`'s *"immediate language
      // switching"*. A refused save leaves the previous stored value in place and
      // the next load reconciles, which is the same posture every other setting
      // on this platform takes.
      adopted.current = true;
      storeLocale(next);
      if (isAuthenticated) {
        updateSettings.mutate([{ key: SETTING_KEY.language, value: next }]);
      }
    },
    [isAuthenticated, updateSettings],
  );

  const value = React.useMemo<LocaleContextValue>(
    () => ({
      locale,
      direction,
      tag: localeTag(locale),
      isReady: loadedLocale === locale,
      setLocale,
    }),
    [locale, direction, loadedLocale, setLocale],
  );

  return (
    <LocaleContext.Provider value={value}>
      <NextIntlClientProvider
        locale={locale}
        messages={messages}
        // Every timestamp on this platform arrives in UTC and is rendered in the
        // reader's own zone — the Language & Region setting, which
        // `hooks/use-date-format.ts` already applies. Declaring it here makes
        // next-intl's own date helpers agree with that hook rather than falling
        // back to the machine's zone.
        timeZone={preferences.timezone || "UTC"}
        onError={(error) => {
          // `MISSING_MESSAGE` is the only one worth acting on: it is the spec's
          // *"missing translations"*, and every other error next-intl raises is a
          // developer mistake that shows up in the console anyway. Reported by
          // **key**, never with the value it would have interpolated.
          if (error.code === "MISSING_MESSAGE") {
            const key = error.originalMessage?.match(/`([^`]+)`/)?.[1];
            if (key) noteMissing(key);
          }
        }}
        getMessageFallback={({ key, namespace }) =>
          humanizeKey(namespace ? `${namespace}.${key}` : key)
        }
      >
        {children}
      </NextIntlClientProvider>
    </LocaleContext.Provider>
  );
}

/**
 * The language in force, its direction, and the way to change it.
 *
 * Safe to call anywhere below `Providers`, including on a page a signed-out
 * visitor can reach: the fallbacks are the platform's own defaults.
 */
export function useLocale(): LocaleContextValue {
  return React.useContext(LocaleContext);
}
