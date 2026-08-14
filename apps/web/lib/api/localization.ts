/**
 * Localization API calls.
 *
 * Thin, typed wrappers over the `/localization` endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape.
 *
 * **There is no `fetchMessages` here, and there will not be one.** A translation
 * catalogue is a static asset this application ships and imports
 * (`lib/i18n/messages.ts`): fetching it from the API would make every page load
 * wait on a database-backed process for text that changes when a release does,
 * and would put the login screen's own copy behind a login.
 *
 * **And there is no `setLanguage`.** A language preference is a *setting*, written
 * through `updateSettings` like every other one — a second path to one stored
 * thing is how two answers to one question start to disagree, which is the rule
 * `20-settings.md` states and this feature had every opportunity to break.
 */

import { apiRequest } from "@/lib/api/client";
import { LOCALIZATION_ENDPOINTS } from "@/lib/api/config";
import {
  languageCatalogSchema,
  localizationMetricsSchema,
  localizationReportSchema,
  MAX_REPORTED_KEYS,
  MAX_TRANSLATION_KEY_LENGTH,
} from "@/lib/validation/localization";
import type {
  LanguageCatalog,
  LocalizationMetrics,
  LocalizationReport,
} from "@/types/localization";

/** The languages the platform serves, and the one this caller reads in. */
export async function fetchLanguageCatalog(): Promise<LanguageCatalog> {
  const payload = languageCatalogSchema.parse(
    await apiRequest<unknown>(LOCALIZATION_ENDPOINTS.languages),
  );

  return {
    languages: payload.languages.map((entry) => ({
      code: entry.code,
      direction: entry.direction,
      locale: entry.locale,
    })),
    default: payload.default,
    resolved: payload.resolved,
    direction: payload.direction,
    locale: payload.locale,
  };
}

/**
 * Tell the platform what could not be rendered.
 *
 * **Never rejects, and that is its contract rather than a courtesy.** It is called
 * from a render path — a missing key noticed while drawing a table, a catalogue
 * chunk that failed to arrive — and a page must not break because the report
 * about it did. Every failure is swallowed: the platform loses one observation,
 * which is the correct trade against a screen that went blank.
 *
 * Keys are filtered before they are sent, on top of the API's own filtering. The
 * duplication is deliberate — see `lib/validation/localization.ts`.
 */
export async function reportLocalizationProblems(
  report: LocalizationReport,
): Promise<void> {
  const missingKeys = (report.missingKeys ?? [])
    .map((key) => key.trim())
    .filter((key) => key.length > 0 && key.length <= MAX_TRANSLATION_KEY_LENGTH)
    .filter((key) => !/\s/.test(key))
    .slice(0, MAX_REPORTED_KEYS);

  const failures = report.failures ?? [];
  if (missingKeys.length === 0 && failures.length === 0) return;

  try {
    const body = localizationReportSchema.parse({
      missing_keys: missingKeys,
      failures,
      catalogue: report.catalogue ?? null,
      language: report.language ?? null,
    });
    await apiRequest<void>(LOCALIZATION_ENDPOINTS.report, {
      method: "POST",
      body,
    });
  } catch {
    /* see the docstring: an observation is never worth a broken render */
  }
}

/** Platform-wide localization health. Requires `localization:monitor`. */
export async function fetchLocalizationMetrics(): Promise<LocalizationMetrics> {
  const payload = localizationMetricsSchema.parse(
    await apiRequest<unknown>(LOCALIZATION_ENDPOINTS.metrics),
  );

  return {
    since: payload.since,
    supportedLanguages: payload.supported_languages,
    defaultLanguage: payload.default_language,
    activeLanguages: payload.active_languages,
    resolutionsByLanguage: payload.resolutions_by_language,
    unsupportedLocaleRequests: payload.unsupported_locale_requests,
    translationFailures: payload.translation_failures,
    failuresByReason: payload.failures_by_reason,
    failingCatalogues: payload.failing_catalogues,
    missingTranslations: payload.missing_translations,
    distinctMissingKeys: payload.distinct_missing_keys,
    missingKeys: payload.missing_keys,
    distribution: payload.distribution,
    accountsFollowingDefault: payload.accounts_following_default,
    reportingEnabled: payload.reporting_enabled,
  };
}
