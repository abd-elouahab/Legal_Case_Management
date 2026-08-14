/**
 * Display formatting that does not depend on a language.
 *
 * **This module used to hold every date helper on the platform, pinned to one
 * locale.** `21-localization.md` is what emptied it: a timestamp depends on the
 * reader's language, time zone, date style, and time style, all four of which are
 * *settings*, and a module-level `Intl.DateTimeFormat` cannot read a setting. Two
 * formatters also meant an Arabic reader saw their hearing date one way on the
 * case page and another way in the timeline beneath it, which is exactly the
 * *"consistent translations"* the spec asks for.
 *
 * Dates, times, and relative event times now come from `hooks/use-date-format.ts`;
 * numbers, percentages, and file sizes from `hooks/use-number-format.ts`. What is
 * left here is what has no locale in it.
 */

/**
 * Initials for an avatar fallback.
 *
 * Uses the first character of the first two words, so "Amina Benali" → "AB".
 * Falls back to the first character of whatever is available rather than
 * rendering an empty circle.
 *
 * **Not localized, and deliberately not.** A name is a name in every language —
 * translating one would be renaming a person — and `toUpperCase()` without a
 * locale argument is the right call for the same reason: an Arabic name has no
 * case to change, and a Turkish dotless *ı* is a hazard this platform does not
 * have, since a person's initials must not depend on which language the reader
 * happens to be using.
 */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";

  return words
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("");
}
