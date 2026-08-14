/**
 * Zod schemas for localization.
 *
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/localization.py`; where they must agree, the API is the
 * authority.
 *
 * **A language code is parsed as a loose string, and the direction beside it is
 * not** — the same split `lib/validation/settings.ts` makes between an open
 * registry and a closed vocabulary. The platform's set of languages is meant to
 * grow (*"allow future languages without redesign"*), so a fourth one added
 * server-side must not turn a language selector into a parse error; `direction`,
 * by contrast, is a closed set this client *branches on*, so an unrecognised one
 * is a genuine contract break.
 */

import { z } from "zod";

import { TRANSLATION_FAILURES } from "@/types/localization";

/** Longest translation key the API accepts in a report, matching `MAX_KEY_LENGTH`. */
export const MAX_TRANSLATION_KEY_LENGTH = 200;

/** Most keys one report may carry, matching `LOCALIZATION_REPORT_MAX_KEYS`. */
export const MAX_REPORTED_KEYS = 50;

export const languageDescriptorSchema = z.object({
  code: z.string().min(1),
  direction: z.enum(["ltr", "rtl"]),
  locale: z.string().min(1),
});

export const languageCatalogSchema = z.object({
  languages: z.array(languageDescriptorSchema),
  default: z.string().min(1),
  resolved: z.string().min(1),
  direction: z.enum(["ltr", "rtl"]),
  locale: z.string().min(1),
});

export const localizationMetricsSchema = z.object({
  since: z.string(),
  supported_languages: z.array(z.string()),
  default_language: z.string(),
  active_languages: z.array(z.string()),
  resolutions_by_language: z.record(z.string(), z.number()),
  unsupported_locale_requests: z.number(),
  translation_failures: z.number(),
  failures_by_reason: z.record(z.string(), z.number()),
  failing_catalogues: z.array(z.string()),
  missing_translations: z.number(),
  distinct_missing_keys: z.number(),
  missing_keys: z.array(z.string()),
  distribution: z.record(z.string(), z.number()),
  accounts_following_default: z.number(),
  reporting_enabled: z.boolean(),
});

/**
 * What may be sent to `POST /localization/report`.
 *
 * Enforced **here as well as on the API**, and the duplication is deliberate: the
 * server discards anything that is not a short, whitespace-free identifier
 * because it cannot trust a client, and this schema does the same so a bug in
 * this application cannot put a rendered sentence — which may name a case, a
 * court, or a person — onto the network in the first place. A key that fails is
 * dropped rather than throwing; the report is a courtesy and must never be able
 * to break the render that produced it.
 */
export const localizationReportSchema = z.object({
  missing_keys: z
    .array(
      z
        .string()
        .max(MAX_TRANSLATION_KEY_LENGTH)
        .refine((value) => value.length > 0 && !/\s/.test(value)),
    )
    .max(MAX_REPORTED_KEYS),
  failures: z.array(z.enum(TRANSLATION_FAILURES)).max(20),
  catalogue: z.string().max(64).nullable(),
  language: z.string().max(16).nullable(),
});
