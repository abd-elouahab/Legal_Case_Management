/**
 * Where the Settings page's words come from.
 *
 * **Not from an API response**, which is the rule `19-dashboard-analytics.md`
 * states for widget labels and `16-notifications.md` states for notification
 * prose: a section, a setting, and a permitted value each travel as a stable
 * identifier, and an API response is a place a translation cannot live.
 *
 * **And, since `21-localization.md`, not from this file either.** It used to hold
 * five `Record<string, string>` constants with a note saying they would become
 * translation keys when next-intl landed; this is that change. The sentences are
 * now `settings.sections.*`, `settings.definitions.*`, `settings.values.*`,
 * `settings.widgets.*`, and `settings.failures.*` in `apps/web/messages/*.json`.
 *
 * **The fallback survived the move, and got better.** Every lookup used to fall
 * back to the raw identifier — which is what made the server-described catalogue
 * worth having, since a setting added on the server appears on the page in a
 * browser nobody redeployed. `useTranslations` falls back through the provider's
 * `getMessageFallback`, which renders a *humanized* form of the key rather than
 * the key itself, so the same gap now reads as "Ai Streaming" instead of
 * `ai_streaming`. A missing label is still cosmetic and never a blank control.
 *
 * What remains here is the one value set that must **not** be translated: the
 * language names.
 */

import { LOCALE_NAMES, type Locale } from "@/lib/i18n/config";

/** Namespaces the Settings components resolve their copy from. */
export const SECTION_NAMESPACE = "settings.sections";
export const SETTING_NAMESPACE = "settings.definitions";
export const VALUE_NAMESPACE = "settings.values";
export const WIDGET_NAMESPACE = "settings.widgets";
export const SETTINGS_FAILURE_NAMESPACE = "settings.failures";

/**
 * A permitted value that is a **language**, rendered in that language.
 *
 * `Français`, `العربية`, `English` — each written in its own language, for the
 * reason `lib/i18n/config.ts` records: a language selector that translated its
 * options would show an Arabic reader the word "French" in Arabic, and somebody
 * looking for their own language would have to already read the current one to
 * find it. These are the one string on the platform that must not be translated,
 * so they are read from the language vocabulary rather than from a catalogue.
 *
 * Returns `null` for anything that is not a language, so a caller can fall
 * through to the ordinary `settings.values` lookup.
 */
export function languageValueLabel(value: string): string | null {
  return value in LOCALE_NAMES ? LOCALE_NAMES[value as Locale] : null;
}
