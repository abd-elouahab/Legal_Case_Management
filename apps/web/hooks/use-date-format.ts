"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { useUserPreferences } from "@/components/settings/settings-provider";
import { localeTag } from "@/lib/i18n/config";

/**
 * Date and time formatting that honours the caller's Language & Region settings.
 *
 * `20-settings.md` asks for a preferred language, a time zone, a date format, and
 * a time format. This hook is where those four stop being stored values and start
 * being what a timestamp looks like on screen.
 *
 * **The stored settings are style *identifiers*, not `strftime` patterns**, and
 * this is the module that makes that decision pay off. A stored `"%d/%m/%Y"`
 * would be a pattern travelling through an API into a browser that does not speak
 * it, and an Arabic reader would get Latin digits from it. `Intl.DateTimeFormat`
 * takes the *shape* (`day_month_year` → 2-digit day, 2-digit month, numeric year)
 * and renders it in the reader's own locale, which is the only way
 * `ai-workflow-rules.md`'s *"locale-aware formatting for dates, numbers, and
 * time"* can be true for `fr` and `ar` at once.
 *
 * **The time zone is applied, not assumed.** Every timestamp on this platform
 * arrives from the API in UTC; without a zone the browser would render it in
 * whichever one the laptop happens to be in, which is wrong for a lawyer working
 * abroad and reading a hearing time. A zone the runtime rejects falls back to the
 * browser's rather than throwing — a settings value must never be able to make a
 * page fail to render.
 *
 * **Every timestamp on the platform goes through this hook.** `lib/format.ts` used
 * to keep pinned-locale copies for the surfaces that were not preference-aware;
 * `21-localization.md` is what removed them — two formatters meant an Arabic
 * reader saw their hearing date in Arabic on one screen and in English on the
 * next, which is precisely the *"consistent translations"* the spec asks for.
 * What remains in `lib/format.ts` is `initials`, which has no locale.
 *
 * **The relative labels are translated, the numbers are formatted.** *"Today ·
 * 14:32"* is two different kinds of thing joined by a separator: `Today` is a word
 * that belongs in a message catalogue, and `14:32` is a number `Intl` writes in
 * the reader's own digits. Keeping them apart is what lets an Arabic reader get
 * *"اليوم"* beside a correctly-formatted time rather than an English word beside
 * it.
 */

/** Maps a stored date-format identifier onto `Intl` options. */
const DATE_STYLES: Record<string, Intl.DateTimeFormatOptions> = {
  day_month_year: { day: "2-digit", month: "2-digit", year: "numeric" },
  month_day_year: { month: "2-digit", day: "2-digit", year: "numeric" },
  year_month_day: { year: "numeric", month: "2-digit", day: "2-digit" },
  long: { day: "numeric", month: "long", year: "numeric" },
};

/**
 * The BCP-47 tag a stored language code renders in.
 *
 * Imported from `lib/i18n/config.ts` rather than declared here, since
 * `21-localization.md` shipped: that module is the client's one language
 * vocabulary, and the tags are asserted against the API's own
 * (`core/localization.LOCALE_TAGS`). Two tables would be two chances for a
 * timestamp on a screen and the same timestamp in an email to be written
 * differently.
 */

interface DateFormatters {
  /** `31/12/2026` — in the caller's chosen style, locale, and zone. */
  formatDate: (value: string | null | undefined, fallback?: string) => string;
  /** `14:32`, or `2:32 pm` on a 12-hour clock. */
  formatTime: (value: string | null | undefined, fallback?: string) => string;
  /** Both, separated by a middle dot — the shape the activity feeds use. */
  formatDateTime: (value: string | null | undefined, fallback?: string) => string;
  /**
   * How an activity feed writes an instant: *Today · 14:32*, *Yesterday · 09:15*,
   * *24 July · 14:32*, and the year once it stops being obvious.
   *
   * **Relative to *now*, so it must only be rendered on the client.** Every feed
   * that uses it is fetched client-side (the access token lives in browser
   * memory), so there is no server render to disagree with — but a future server
   * component must pin `now` rather than call this.
   */
  formatEventTime: (value: string | null | undefined, now?: Date) => string;
  /** The BCP-47 tag in force, for anything doing its own `Intl` work. */
  locale: string;
  /** The IANA zone in force. */
  timeZone: string;
}

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * Build a formatter, falling back to the browser's zone if the stored one is
 * unusable.
 *
 * `Intl.DateTimeFormat` throws a `RangeError` for a zone it does not know, and a
 * stored preference is exactly the kind of value that can outlive a runtime's tz
 * database. The API validates against Python's database on write; the browser's
 * may be older, so the same value can be acceptable there and unknown here.
 */
function makeFormat(
  locale: string,
  options: Intl.DateTimeFormatOptions,
  timeZone: string,
): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat(locale, { ...options, timeZone });
  } catch {
    return new Intl.DateTimeFormat(locale, options);
  }
}

/** Whether two instants fall on the same calendar day, in the reader's zone. */
function isSameDay(a: Date, b: Date, formatter: Intl.DateTimeFormat): boolean {
  // Compared through the *formatter* rather than through `getDate()`, because the
  // reader's zone is a preference and the browser's is a fact about a laptop: a
  // lawyer in Casablanca reading a platform configured for Paris must see
  // "Today" for the day it is where their hearings are, not where their machine
  // is.
  return formatter.format(a) === formatter.format(b);
}

export function useDateFormat(): DateFormatters {
  const { preferences } = useUserPreferences();
  const t = useTranslations("common.time");

  return React.useMemo(() => {
    const locale = localeTag(preferences.language);
    const timeZone = preferences.timezone || "UTC";
    const dateStyle = DATE_STYLES[preferences.dateFormat] ?? DATE_STYLES.day_month_year;
    const hour12 = preferences.timeFormat === "hour_12";

    const dateFormatter = makeFormat(locale, dateStyle, timeZone);
    const timeFormatter = makeFormat(
      locale,
      { hour: "2-digit", minute: "2-digit", hour12 },
      timeZone,
    );

    const formatDate = (value: string | null | undefined, fallback = "—") => {
      const date = parse(value);
      return date ? dateFormatter.format(date) : fallback;
    };

    const formatTime = (value: string | null | undefined, fallback = "—") => {
      const date = parse(value);
      return date ? timeFormatter.format(date) : fallback;
    };

    // The three shapes an activity feed steps through, each in the reader's own
    // locale and zone. Built here so the memo owns every formatter it hands out.
    const dayKeyFormatter = makeFormat(
      locale,
      { year: "numeric", month: "2-digit", day: "2-digit" },
      timeZone,
    );
    const dayMonthFormatter = makeFormat(
      locale,
      { day: "numeric", month: "long" },
      timeZone,
    );
    const dayMonthYearFormatter = makeFormat(
      locale,
      { day: "numeric", month: "long", year: "numeric" },
      timeZone,
    );
    const yearFormatter = makeFormat(locale, { year: "numeric" }, timeZone);

    return {
      formatDate,
      formatTime,
      formatDateTime: (value: string | null | undefined, fallback = "—") => {
        const date = parse(value);
        if (!date) return fallback;
        return `${dateFormatter.format(date)} • ${timeFormatter.format(date)}`;
      },
      formatEventTime: (value: string | null | undefined, now: Date = new Date()) => {
        const date = parse(value);
        if (!date) return "—";

        const time = timeFormatter.format(date);

        if (isSameDay(date, now, dayKeyFormatter)) {
          return t("todayAt", { time });
        }

        const yesterday = new Date(now);
        yesterday.setDate(now.getDate() - 1);
        if (isSameDay(date, yesterday, dayKeyFormatter)) {
          return t("yesterdayAt", { time });
        }

        // The year is dropped while it is still obvious, because repeating it on
        // every entry of a case opened this year is noise.
        const day =
          yearFormatter.format(date) === yearFormatter.format(now)
            ? dayMonthFormatter.format(date)
            : dayMonthYearFormatter.format(date);

        return t("dateAt", { date: day, time });
      },
      locale,
      timeZone,
    };
  }, [
    t,
    preferences.language,
    preferences.timezone,
    preferences.dateFormat,
    preferences.timeFormat,
  ]);
}
