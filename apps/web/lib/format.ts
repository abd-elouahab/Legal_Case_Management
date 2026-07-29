/**
 * Display formatting helpers.
 *
 * Dates arrive from the API as ISO-8601 strings and must never be shown raw.
 * Formatting goes through `Intl`, which already knows about locale conventions —
 * so when localization (next-intl) lands, these take a locale argument instead of
 * defaulting to the platform locale, and nothing above them changes.
 */

/**
 * Locale used until language switching ships.
 *
 * `undefined` would use the *browser's* locale, which differs between the server
 * render and the client and produces a hydration mismatch. Pinning it keeps the
 * two identical.
 */
const DEFAULT_LOCALE = "en-GB";

const DATE_FORMAT = new Intl.DateTimeFormat(DEFAULT_LOCALE, {
  day: "2-digit",
  month: "short",
  year: "numeric",
});

const DATE_TIME_FORMAT = new Intl.DateTimeFormat(DEFAULT_LOCALE, {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Format a date, or return `fallback` when there is nothing to show. */
export function formatDate(value: string | null | undefined, fallback = "—"): string {
  const date = parse(value);
  return date ? DATE_FORMAT.format(date) : fallback;
}

/** Format a date and time, or return `fallback`. */
export function formatDateTime(value: string | null | undefined, fallback = "—"): string {
  const date = parse(value);
  return date ? DATE_TIME_FORMAT.format(date) : fallback;
}

/**
 * Initials for an avatar fallback.
 *
 * Uses the first character of the first two words, so "Amina Benali" → "AB".
 * Falls back to the first character of whatever is available rather than
 * rendering an empty circle.
 */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";

  return words
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("");
}
