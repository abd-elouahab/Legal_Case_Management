/**
 * Localization domain types.
 *
 * The app-side shape of `/localization`, in camelCase. Nothing above
 * `lib/api/localization.ts` sees the API's snake_case wire format.
 *
 * **There is no `Messages` type here.** A catalogue is a static asset the web
 * application ships and imports (`lib/i18n/messages.ts`), never something the API
 * serves — see `api/v1/localization/router.py` for why. What the API contributes
 * is the *vocabulary*: which languages exist, which way each is written, and which
 * one this caller is being addressed in.
 */

import type { Locale } from "@/lib/i18n/config";

/** One language the platform serves, as the API describes it. */
export interface LanguageDescriptor {
  code: string;
  direction: "ltr" | "rtl";
  /** BCP-47 tag this language formats dates, times, and numbers with. */
  locale: string;
}

/**
 * Every language the platform serves, and the one this caller reads in.
 *
 * `resolved` is the server's own answer to the selection chain — the caller's
 * stored preference, then the platform default, then the application default —
 * so a client can adopt it without re-implementing the priority order. That is
 * what keeps an interface and an email from disagreeing about somebody's
 * language.
 */
export interface LanguageCatalog {
  languages: LanguageDescriptor[];
  default: string;
  resolved: string;
  direction: "ltr" | "rtl";
  locale: string;
}

/** Why a catalogue could not be used. Mirrors the API's closed vocabulary. */
export const TRANSLATION_FAILURES = [
  "load_failed",
  "parse_failed",
  "unsupported_locale",
  "unknown",
] as const;

export type TranslationFailure = (typeof TRANSLATION_FAILURES)[number];

/**
 * What a client tells the platform about its own translation problems.
 *
 * **Keys and identifiers only.** There is no field for a rendered string and none
 * for an interpolated value, because either would carry a case name, a court, or
 * a person into a metrics process — the API discards anything that is not a
 * short, whitespace-free identifier, and this type is shaped so a caller cannot
 * try.
 */
export interface LocalizationReport {
  missingKeys?: string[];
  failures?: TranslationFailure[];
  /** A locale, or a locale and a namespace. Never a URL. */
  catalogue?: string;
  language?: Locale | string;
}

/** Platform-wide localization health. Requires `localization:monitor`. */
export interface LocalizationMetrics {
  /** When this process started counting. Applies to the counters only. */
  since: string;
  supportedLanguages: string[];
  defaultLanguage: string;
  activeLanguages: string[];
  resolutionsByLanguage: Record<string, number>;
  unsupportedLocaleRequests: number;
  translationFailures: number;
  failuresByReason: Record<string, number>;
  failingCatalogues: string[];
  missingTranslations: number;
  distinctMissingKeys: number;
  missingKeys: string[];
  /** A SQL aggregate, so it carries no `since` caveat. */
  distribution: Record<string, number>;
  accountsFollowingDefault: number;
  reportingEnabled: boolean;
}
