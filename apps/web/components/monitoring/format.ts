/**
 * Formatting helpers for the monitoring page.
 *
 * Pure functions, kept out of the components for the reason `code-standards.md`
 * gives: no logic in a component, and a helper with no JSX in it is one a unit
 * test can reach.
 *
 * **Every one of them renders an em dash for an absent value rather than a
 * zero.** That distinction is the whole reason this file exists: a `null`
 * percentile means *this cannot be known from a mean-and-count recorder*, and a
 * `0` would read as *instantaneous* — which on an operational page is a
 * confidently wrong statement rather than a missing one.
 */

/** What an absent value renders as. */
export const EMPTY = "—";

/**
 * Render a duration in milliseconds at a sensible scale.
 *
 * Sub-millisecond figures keep two decimals, milliseconds keep one, and anything
 * past a second becomes seconds — because "1483.2 ms" and "1.5 s" are the same
 * number and only one of them is read at a glance.
 */
export function formatDuration(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY;
  if (value < 1) return `${value.toFixed(2)} ms`;
  if (value < 1_000) return `${value.toFixed(1)} ms`;
  return `${(value / 1_000).toFixed(2)} s`;
}

/** Render a count, or an em dash when there is none to render. */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY;
  return Math.round(value).toLocaleString();
}

/** Render a percentage the server already computed. */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY;
  return `${value.toFixed(value >= 10 ? 0 : 1)} %`;
}

/**
 * Turn a stable identifier into something readable when no translation exists.
 *
 * The fallback rule `21-localization.md` states, applied to the open
 * vocabularies this feature deliberately does not close: a component, an error
 * category, or a security event this build has never heard of renders as a
 * humanized form of its own key rather than as a blank or a raw slug. A newer
 * backend is then legible rather than broken.
 */
export function humanize(key: string): string {
  const words = key.replace(/[_.-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
