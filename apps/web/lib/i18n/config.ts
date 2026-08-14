/**
 * The client's language vocabulary.
 *
 * The browser half of `apps/api/core/localization.py`, and deliberately the same
 * three facts: which languages exist, which way each is written, and which
 * BCP-47 tag each formats with. The API serves all three from
 * `GET /localization/languages`, and this file is what the application uses
 * *before* that response arrives — the login screen has no session, so it has no
 * catalogue either, and a sign-in page that could not render its own copy until
 * an authenticated request completed would be a sign-in page nobody could read.
 *
 * **The two are kept in step by a test rather than by a comment.** A language
 * present here and absent there would render an interface the platform cannot
 * write an email in; the reverse would send somebody an Arabic hearing alert
 * linking to an English screen.
 *
 * **The names live here and nowhere else on the platform.** `English`,
 * `Français`, `العربية` — each written *in its own language*, because a language
 * selector that translated its options would show an Arabic reader the word
 * "Arabic" in Arabic and the word "French" in Arabic, and somebody looking for
 * their own language would have to already read the current one to find it. That
 * is also why these are not in the message catalogues: they are the one string on
 * the platform that must not be translated.
 */

/** Every language the platform serves, in display order. */
export const LOCALES = ["en", "fr", "ar"] as const;

export type Locale = (typeof LOCALES)[number];

/**
 * The application's default language.
 *
 * `21-localization.md` names English as the default, and the API's
 * `DEFAULT_LANGUAGE` is what actually decides — this constant is the value used
 * before the API has answered, and the fallback a missing translation resolves
 * against.
 */
export const DEFAULT_LOCALE: Locale = "en";

/** Which way each language is written. */
export const LOCALE_DIRECTIONS: Record<Locale, "ltr" | "rtl"> = {
  en: "ltr",
  fr: "ltr",
  ar: "rtl",
};

/**
 * The BCP-47 tag each language formats dates, times, and numbers with.
 *
 * Regional tags rather than bare codes, for the reason `hooks/use-date-format.ts`
 * already records and `core/localization.py` restates: bare `ar` leaves `Intl` to
 * choose a region, and some choices render Eastern Arabic numerals — which would
 * make a case number's year unreadable to a French colleague on the same matter.
 * Pinning them also keeps a server render and a client render identical, which is
 * what avoids a hydration mismatch.
 */
export const LOCALE_TAGS: Record<Locale, string> = {
  en: "en-GB",
  fr: "fr-FR",
  ar: "ar-MA",
};

/**
 * Each language's name, written in that language.
 *
 * Never translated — see the module docstring.
 */
export const LOCALE_NAMES: Record<Locale, string> = {
  en: "English",
  fr: "Français",
  ar: "العربية",
};

/** Whether a value is a language this application can render. */
export function isLocale(value: string | null | undefined): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

/**
 * Reduce a language tag to a supported locale, or `null`.
 *
 * Accepts what the outside world actually sends — `fr`, `FR`, `fr-FR`, `ar-MA`,
 * `en_GB` — because `navigator.language`, a stored setting, and an API response
 * are three spellings of one idea. The mirror of
 * `core.localization.normalize_language`, down to returning `null` rather than a
 * default: a caller walking a list of candidates has to tell *"this one did not
 * answer"* apart from *"this one said English"*.
 */
export function normalizeLocale(value: string | null | undefined): Locale | null {
  if (!value) return null;
  const primary = value.trim().toLowerCase().split(/[-_]/)[0];
  return isLocale(primary) ? primary : null;
}

/**
 * The first supported locale among `candidates`, or the default.
 *
 * `21-localization.md`'s Language Selection and Fallback Strategy in one
 * function, exactly as `core.localization.resolve_language` is on the server:
 * callers pass their candidates in priority order and never branch on emptiness
 * themselves.
 */
export function resolveLocale(...candidates: (string | null | undefined)[]): Locale {
  for (const candidate of candidates) {
    const resolved = normalizeLocale(candidate);
    if (resolved) return resolved;
  }
  return DEFAULT_LOCALE;
}

/** `"rtl"` for Arabic, `"ltr"` for everything else. */
export function directionOf(locale: string | null | undefined): "ltr" | "rtl" {
  const resolved = normalizeLocale(locale);
  return resolved ? LOCALE_DIRECTIONS[resolved] : "ltr";
}

/** The BCP-47 tag a locale formats with. */
export function localeTag(locale: string | null | undefined): string {
  return LOCALE_TAGS[resolveLocale(locale)];
}

/**
 * The best supported locale the browser asks for, or `null`.
 *
 * Step 2 of the spec's selection chain — *"browser language (first login only)"*
 * — and the reason it is only consulted on a first login lives in
 * `components/i18n/locale-provider.tsx`, not here: this function answers *what
 * does the browser want*, and the provider decides *when that matters*.
 *
 * `navigator.languages` is walked in order and the first supported entry wins, so
 * a browser configured `de, ar, en` resolves to Arabic rather than giving up at
 * German. Returns `null` when the browser names nothing this platform serves, so
 * a caller can fall through rather than being handed a default that hides the
 * fact that the browser said nothing useful.
 */
export function browserLocale(): Locale | null {
  if (typeof navigator === "undefined") return null;

  const requested =
    navigator.languages && navigator.languages.length > 0
      ? navigator.languages
      : [navigator.language];

  for (const tag of requested) {
    const resolved = normalizeLocale(tag);
    if (resolved) return resolved;
  }
  return null;
}
