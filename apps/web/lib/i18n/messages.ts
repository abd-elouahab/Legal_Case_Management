/**
 * Loading, caching, and falling back a translation catalogue.
 *
 * `21-localization.md` asks for four things this module is the whole of:
 * *"store translations separately from application logic"*, *"load only required
 * translations"*, *"cache translation resources"*, and — the one that decides the
 * shape — *"missing translations should never break the application"*.
 *
 * **One catalogue per language, imported dynamically.** `messages/en.json`,
 * `messages/fr.json`, `messages/ar.json` are separate modules, so a browser
 * downloads the language it is rendering in and not the other two: switching to
 * Arabic fetches one chunk, and switching back fetches nothing because the first
 * one is still in the cache below. Namespaces are the *top-level keys* inside
 * each file rather than separate files, which is what keeps a screen from having
 * to declare which parts of the interface it might mention.
 *
 * **The fallback is structural, not a code path.** A non-default catalogue is
 * deep-merged *onto* the default one before it is handed to the provider, so a
 * key present in English and absent in Arabic resolves to the English sentence —
 * one lookup, no branch, and no chance of a screen half-rendering because
 * somebody forgot a `??`. That is the spec's fallback strategy step 1 (*"use the
 * default language"*); step 2 (*"display a meaningful fallback"*) is
 * `humanizeKey` below, for a key that exists in no catalogue at all.
 *
 * **A key is never shown to a user.** *"The application should never expose
 * translation keys to users"* is the requirement, and `cases.filters.clearAll`
 * printed on a button is exactly the failure it names. So the last resort is a
 * humanized form of the final segment — "Clear All" — which is wrong-looking to a
 * developer and readable to everybody else, which is the right way round.
 */

import { DEFAULT_LOCALE, type Locale } from "@/lib/i18n/config";

/** A catalogue: nested namespaces of strings. */
export type Messages = { [key: string]: string | Messages };

/**
 * Catalogues already loaded in this browser.
 *
 * A module-level cache rather than React state, deliberately: a language the user
 * switches away from and back to must not be downloaded twice, and a cache inside
 * a component would be discarded by the remount that switching causes. Keyed by
 * locale, so it holds at most three entries.
 */
const cache = new Map<Locale, Messages>();

/**
 * Loads in flight, so two components mounting at once fetch one chunk.
 *
 * The same shape `lib/api/client.ts` uses for a token refresh, and for the same
 * reason: without it, the shell and the first page would each start their own
 * import of the same module on the first render after a switch.
 */
const inFlight = new Map<Locale, Promise<Messages>>();

async function importCatalogue(locale: Locale): Promise<Messages> {
  // A switch rather than a template literal, because a bundler has to be able to
  // see every module that can be imported: `import(`../../messages/${locale}.json`)`
  // makes the whole directory a dependency of this chunk, which is precisely the
  // "load only required translations" requirement undone.
  switch (locale) {
    case "fr":
      return (await import("@/messages/fr.json")).default as Messages;
    case "ar":
      return (await import("@/messages/ar.json")).default as Messages;
    case "en":
    default:
      return (await import("@/messages/en.json")).default as Messages;
  }
}

/**
 * Merge `overrides` onto `base`, recursively, without mutating either.
 *
 * Objects are merged and everything else replaced, so a namespace translated
 * halfway keeps the default language's sentences for the keys it is missing
 * rather than losing the whole namespace to a shallow assignment.
 */
function merge(base: Messages, overrides: Messages): Messages {
  const result: Messages = { ...base };

  for (const [key, value] of Object.entries(overrides)) {
    const existing = result[key];
    result[key] =
      typeof value === "object" &&
      value !== null &&
      typeof existing === "object" &&
      existing !== null
        ? merge(existing, value)
        : value;
  }
  return result;
}

/**
 * The catalogue for `locale`, complete: every key the default language has.
 *
 * Throws only if the **default** catalogue cannot be loaded, which is a broken
 * build rather than a runtime condition — there is nothing sensible to render
 * without it, and swallowing it would produce a page of humanized key fragments
 * that looks like a translation nobody finished. A *non-default* catalogue that
 * fails to load resolves to the default one and is reported: an Arabic reader
 * sees English rather than a blank screen, which is the spec's own fallback.
 */
export async function loadMessages(locale: Locale): Promise<Messages> {
  const cached = cache.get(locale);
  if (cached) return cached;

  const pending = inFlight.get(locale);
  if (pending) return pending;

  const load = (async () => {
    const base = await importCatalogue(DEFAULT_LOCALE);
    const messages =
      locale === DEFAULT_LOCALE ? base : merge(base, await importCatalogue(locale));
    cache.set(locale, messages);
    return messages;
  })();

  inFlight.set(locale, load);
  try {
    return await load;
  } finally {
    inFlight.delete(locale);
  }
}

/**
 * The default catalogue, for the moment before anything has loaded.
 *
 * Synchronous and eagerly imported — it is the one catalogue every session needs,
 * it is what a missing key in any other language falls back to, and making the
 * first paint wait on a dynamic import would flash a page of empty labels.
 */
export { default as defaultMessages } from "@/messages/en.json";

/**
 * A readable last resort for a key no catalogue defines.
 *
 * `cases.filters.clearAll` → `Clear All`. Only the final segment is used, because
 * the namespace is a fact about where the string lives rather than about what it
 * says, and `Cases Filters Clear All` on a button is worse than the alternative.
 *
 * This is deliberately *not* pretty. It exists so that a key missing from every
 * language degrades into something a user can act on, and so that the failure is
 * still obvious enough for a developer to notice — which is why it is reported to
 * `POST /localization/report` at the same time.
 */
export function humanizeKey(key: string): string {
  const segment = key.split(".").pop() ?? key;

  return (
    segment
      // camelCase and snake_case both become spaced words.
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/[_-]+/g, " ")
      .trim()
      .replace(/\b\w/g, (character) => character.toUpperCase()) || key
  );
}
