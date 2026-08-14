"use client";

import * as React from "react";

import { useLocale } from "@/components/i18n/locale-provider";

/**
 * Number formatting that honours the reader's language.
 *
 * `21-localization.md`'s Number Localization section names four things —
 * **numbers, percentages, file sizes, and storage values** — and this hook is all
 * four. `hooks/use-date-format.ts` is its counterpart for instants; the two are
 * separate because a date depends on three settings (language, time zone, and the
 * date and time styles) and a number depends on one.
 *
 * **The separator is the whole point.** `1,024` in English is `1 024` in French
 * and `1٬024` in Arabic, and a platform that showed a French lawyer `1,024` has
 * shown them a number with a decimal point in it. `Intl.NumberFormat` knows all
 * of that; what it needs is the locale, which is what this hook supplies.
 *
 * **A file size is a number and a unit, and only the number is localized.** `MB`
 * is a symbol rather than a word — SI and IEC prefixes are the same in every
 * language this platform serves, and translating them would produce
 * `1,5 مو` for a figure every reader of a legal file already recognises. So the
 * unit stays and the digits, the separator, and the decimal mark change. That is
 * also why the units are not in the message catalogues.
 */

/** Units for a byte count, in the order they are stepped through. */
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

interface NumberFormatters {
  /** `1,024` / `1 024` — a plain count. */
  formatNumber: (value: number | null | undefined, fallback?: string) => string;
  /**
   * `4.2%` — a share already expressed in percentage points (`4.2`), not a
   * fraction. That matches every figure the API sends: a success rate, a
   * grounding rate, and a relevance score all arrive as `0`–`100`.
   */
  formatPercent: (
    value: number | null | undefined,
    options?: { decimals?: number; fallback?: string },
  ) => string;
  /** `2.4 MB` — a byte count with the largest unit that keeps it readable. */
  formatBytes: (value: number | null | undefined, fallback?: string) => string;
  /**
   * `4 of 12` is *not* here on purpose: a sentence joining two numbers is
   * user-facing text and belongs in a message catalogue, interpolated with the
   * numbers these functions produce.
   */
  /** The BCP-47 tag in force, for anything doing its own `Intl` work. */
  locale: string;
}

function isFinite(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function useNumberFormat(): NumberFormatters {
  const { tag } = useLocale();

  return React.useMemo(() => {
    // Built once per locale rather than per call: constructing an
    // `Intl.NumberFormat` is the expensive part, and a table of two hundred rows
    // would otherwise build two hundred of them.
    const decimal = new Intl.NumberFormat(tag);
    const oneDecimal = new Intl.NumberFormat(tag, {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    });

    const formatNumber = (value: number | null | undefined, fallback = "—") =>
      isFinite(value) ? decimal.format(value) : fallback;

    return {
      formatNumber,
      formatPercent: (value, options) => {
        const fallback = options?.fallback ?? "—";
        if (!isFinite(value)) return fallback;
        const decimals = options?.decimals ?? 1;
        return `${new Intl.NumberFormat(tag, {
          minimumFractionDigits: decimals,
          maximumFractionDigits: decimals,
        }).format(value)}%`;
      },
      formatBytes: (value, fallback = "—") => {
        if (!isFinite(value) || value < 0) return fallback;
        if (value === 0) return `${decimal.format(0)} ${BYTE_UNITS[0]}`;

        let size = value;
        let unit = 0;
        while (size >= 1024 && unit < BYTE_UNITS.length - 1) {
          size /= 1024;
          unit += 1;
        }
        // Whole bytes read as whole bytes ("512 B", never "512.0 B"); everything
        // above gets one decimal, which is the precision a person actually reads
        // a file size at.
        const formatted =
          unit === 0 ? decimal.format(Math.round(size)) : oneDecimal.format(size);
        return `${formatted} ${BYTE_UNITS[unit]}`;
      },
      locale: tag,
    };
  }, [tag]);
}
