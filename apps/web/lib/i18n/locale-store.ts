/**
 * The browser's copy of the chosen language.
 *
 * A tiny external store rather than React state, and the reason is the same one
 * `next-themes` has for keeping the theme in `localStorage`: this value has to be
 * readable **before** anything renders. The login screen has no session and
 * therefore no settings, so without a local copy somebody who has been using the
 * platform in Arabic for a year would be shown an English sign-in page every
 * time.
 *
 * **Why a store and not `useState` plus an effect.** The value is written from
 * three places that are not renders — a menu selection, the reconciliation with
 * the Settings API, and the one-off adoption of the browser's language — and it
 * has to survive the remount that switching language causes. Holding it in a
 * component would mean `setState` inside an effect on every one of those paths,
 * which is the cascading-render pattern React now warns about; writing to an
 * external store *is* what an effect is for, and `useSyncExternalStore` reads it
 * without a hydration mismatch because the server snapshot is explicitly `null`.
 *
 * **It is a cache, never the source of truth.** The durable answer is
 * `user_settings.language`, which is what follows somebody to a new device;
 * `components/i18n/locale-provider.tsx` reconciles in one direction only — the
 * server's answer wins once it arrives.
 */

import { normalizeLocale, type Locale } from "@/lib/i18n/config";

const STORAGE_KEY = "legal-platform.locale";

type Listener = () => void;

const listeners = new Set<Listener>();

/**
 * The last value read or written, so `getSnapshot` is referentially stable.
 *
 * `useSyncExternalStore` re-renders whenever the snapshot changes identity, and
 * reading `localStorage` on every call would return a new string each time on
 * some engines. Caching also means a storage-disabled browser costs one failed
 * read rather than one per render.
 */
let snapshot: Locale | null = null;
let hydrated = false;

function read(): Locale | null {
  try {
    return normalizeLocale(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    // A browser with storage disabled costs the pre-session copy and nothing
    // else: the preference still arrives from the API a moment later.
    return null;
  }
}

/** Subscribe to changes, including ones made in another tab. */
export function subscribeToLocale(listener: Listener): () => void {
  listeners.add(listener);

  const onStorage = (event: StorageEvent) => {
    if (event.key !== STORAGE_KEY) return;
    hydrated = false;
    listener();
  };
  window.addEventListener("storage", onStorage);

  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** The stored language, or `null` when nothing has been stored on this device. */
export function getStoredLocale(): Locale | null {
  if (!hydrated) {
    snapshot = read();
    hydrated = true;
  }
  return snapshot;
}

/**
 * What the *server* render sees: nothing.
 *
 * The server cannot know this device's copy, and pretending otherwise is exactly
 * the hydration mismatch this store exists to avoid. The first client render uses
 * the same default the server used, and the stored value is applied immediately
 * afterwards — before paint, because `useSyncExternalStore` schedules it
 * synchronously.
 */
export function getServerLocale(): Locale | null {
  return null;
}

/** Store a language and notify every reader, in this tab and the others. */
export function storeLocale(locale: Locale): void {
  if (getStoredLocale() === locale) return;

  snapshot = locale;
  hydrated = true;
  try {
    window.localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    /* see `read` */
  }
  for (const listener of listeners) listener();
}
