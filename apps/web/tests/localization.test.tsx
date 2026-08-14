/**
 * Localization tests.
 *
 * The claims `21-localization.md` makes about the browser half of this feature,
 * and the ones no other suite covers because `tests/setup.ts` binds
 * `useTranslations` to the English catalogue for every other test: the selection
 * chain, catalogue loading and its fallback, RTL, formatting, and the promise that
 * a translation key never reaches a screen.
 *
 * The vocabulary and formatting helpers are pure, so they are tested directly; the
 * catalogues are tested as data, which is where "missing translations should never
 * break the application" is actually decided.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { useNumberFormat } from "@/hooks/use-number-format";
import {
  DEFAULT_LOCALE,
  LOCALES,
  LOCALE_DIRECTIONS,
  LOCALE_NAMES,
  LOCALE_TAGS,
  directionOf,
  isLocale,
  localeTag,
  normalizeLocale,
  resolveLocale,
} from "@/lib/i18n/config";
import { humanizeKey } from "@/lib/i18n/messages";
import ar from "@/messages/ar.json";
import en from "@/messages/en.json";
import fr from "@/messages/fr.json";

// --------------------------------------------------------------------------- //
// The vocabulary
// --------------------------------------------------------------------------- //

describe("the language vocabulary", () => {
  it("serves the three the spec names, in its order", () => {
    expect(LOCALES).toEqual(["en", "fr", "ar"]);
    expect(DEFAULT_LOCALE).toBe("en");
  });

  it("writes Arabic right to left and the others left to right", () => {
    expect(directionOf("ar")).toBe("rtl");
    expect(directionOf("fr")).toBe("ltr");
    expect(directionOf("en")).toBe("ltr");
  });

  it("reads an unknown language left to right rather than mirroring a page", () => {
    expect(directionOf("de")).toBe("ltr");
    expect(directionOf(null)).toBe("ltr");
  });

  it("formats with a regional tag rather than a bare code", () => {
    // A bare `ar` leaves `Intl` to choose a region, and some choices render
    // Eastern Arabic numerals — which would make a case number's year unreadable
    // to a French colleague on the same matter.
    for (const locale of LOCALES) {
      expect(LOCALE_TAGS[locale]).toContain("-");
    }
    expect(localeTag("ar")).toBe("ar-MA");
    expect(localeTag("de")).toBe(LOCALE_TAGS[DEFAULT_LOCALE]);
  });

  it("names every language in its own language", () => {
    // The one string on the platform that must never be translated: somebody
    // looking for their own language should find the word they recognise rather
    // than having to read the current one first.
    expect(LOCALE_NAMES.fr).toBe("Français");
    expect(LOCALE_NAMES.ar).toBe("العربية");
  });
});

describe("normalizing a language tag", () => {
  it.each(["fr", "FR", "  fr  ", "fr-FR", "fr_FR"])("reduces %s to its code", (value) => {
    expect(normalizeLocale(value)).toBe("fr");
  });

  it.each([null, undefined, "", "de", "klingon"])(
    "answers null for %s rather than a default",
    (value) => {
      // `null` rather than a default is what makes it composable: a caller walking
      // a candidate list has to tell "did not answer" from "said English".
      expect(normalizeLocale(value)).toBeNull();
    },
  );

  it("narrows a supported value", () => {
    expect(isLocale("ar")).toBe(true);
    expect(isLocale("de")).toBe(false);
  });
});

describe("the selection chain", () => {
  it("takes the first supported candidate", () => {
    expect(resolveLocale(null, "de", "ar", "fr")).toBe("ar");
  });

  it("falls back to the application default when nothing resolves", () => {
    expect(resolveLocale(null, "", "klingon")).toBe(DEFAULT_LOCALE);
    expect(resolveLocale()).toBe(DEFAULT_LOCALE);
  });
});

// --------------------------------------------------------------------------- //
// The catalogues
// --------------------------------------------------------------------------- //

/** Flatten a nested catalogue into dotted keys. */
function flatten(node: unknown, prefix = ""): Record<string, string> {
  const flat: Record<string, string> = {};

  for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") flat[path] = value;
    else Object.assign(flat, flatten(value, path));
  }
  return flat;
}

const catalogues = { en, fr, ar } as const;

describe("the translation catalogues", () => {
  it("translate every key the default language defines", () => {
    // The spec's fallback strategy keeps a missing key from breaking anything;
    // this is what keeps it from being needed. A key present in English and
    // absent in Arabic renders English — correct, and not what was intended.
    const expected = Object.keys(flatten(en)).sort();

    for (const [locale, catalogue] of Object.entries(catalogues)) {
      expect({ locale, keys: Object.keys(flatten(catalogue)).sort() }).toEqual({
        locale,
        keys: expected,
      });
    }
  });

  it("keep every interpolated placeholder across languages", () => {
    // A translation that lost `{time}` renders a sentence with a hole in it, and
    // one that invented `{date}` throws at render — both are the kind of failure
    // that only appears in the language nobody on the team reads.
    //
    // **Argument *names*, not brace groups.** A naive `/\{[^}]+\}/` also matches
    // the branch bodies of an ICU plural — `{# passages}` — and those are
    // translated text that *must* differ between languages: French pluralizes at
    // a different boundary from English, and Arabic has six categories rather
    // than two. What has to match is the set of arguments the message consumes,
    // which is what a caller supplies and what throws when it is wrong.
    //
    // The pattern requires an identifier immediately followed by `,` or `}`, so
    // `{count, plural` and `{time}` match while `{# passages}`, `{1 source}`, and
    // `{Aucun dossier}` do not.
    // A **set**, not a list: Arabic has five plural branches where English has
    // two, so a message interpolating `{from}` inside each branch mentions it
    // five times. What must match is which arguments the message consumes.
    const placeholders = (value: string) =>
      [...new Set([...value.matchAll(/\{\s*([A-Za-z_]\w*)\s*[,}]/g)].map((m) => m[1]))]
        .sort()
        .join(",");

    const base = flatten(en);
    for (const [locale, catalogue] of Object.entries(catalogues)) {
      const translated = flatten(catalogue);
      for (const [key, value] of Object.entries(base)) {
        expect({ locale, key, placeholders: placeholders(translated[key]) }).toEqual({
          locale,
          key,
          placeholders: placeholders(value),
        });
      }
    }
  });

  it("keep every ICU plural a plural in every language", () => {
    // The companion to the check above, and the reason it had to be loosened: a
    // translator who flattened `{count, plural, …}` into one sentence would
    // produce a message that renders "1 passages" in French and is simply wrong
    // in Arabic. The *argument* survives the check above; that it is still a
    // plural is this one.
    const base = flatten(en);
    for (const [locale, catalogue] of Object.entries(catalogues)) {
      const translated = flatten(catalogue);
      for (const [key, value] of Object.entries(base)) {
        if (!value.includes(", plural,")) continue;
        expect({ locale, key, plural: translated[key].includes(", plural,") }).toEqual({
          locale,
          key,
          plural: true,
        });
      }
    }
  });

  it("never leave a translation empty", () => {
    for (const [locale, catalogue] of Object.entries(catalogues)) {
      for (const [key, value] of Object.entries(flatten(catalogue))) {
        expect({ locale, key, empty: value.trim() === "" }).toEqual({
          locale,
          key,
          empty: false,
        });
      }
    }
  });

  it("name every language the platform serves in `navigation` and `common`", () => {
    // A sanity check that the catalogue actually covers the shell, which is what
    // a signed-out visitor sees before anything else loads.
    for (const catalogue of Object.values(catalogues)) {
      const flat = flatten(catalogue);
      expect(flat["navigation.items.dashboard"]).toBeTruthy();
      expect(flat["common.actions.save"]).toBeTruthy();
      expect(flat["shell.language.label"]).toBeTruthy();
    }
  });
});

describe("the last-resort fallback", () => {
  it("renders a readable phrase rather than a key", () => {
    // "The application should never expose translation keys to users."
    expect(humanizeKey("cases.filters.clearAll")).toBe("Clear All");
    expect(humanizeKey("documents.upload_document")).toBe("Upload Document");
    expect(humanizeKey("save")).toBe("Save");
  });

  it("uses the final segment only", () => {
    // `Cases Filters Clear All` on a button is worse than the alternative: the
    // namespace says where a string lives, not what it says.
    expect(humanizeKey("a.b.c.title")).toBe("Title");
  });
});

// --------------------------------------------------------------------------- //
// Number localization
// --------------------------------------------------------------------------- //

describe("number formatting", () => {
  function Probe({ value }: { value: number }) {
    const { formatNumber, formatPercent, formatBytes } = useNumberFormat();

    return (
      <ul>
        <li data-testid="number">{formatNumber(value)}</li>
        <li data-testid="percent">{formatPercent(42.5)}</li>
        <li data-testid="bytes">{formatBytes(value)}</li>
        <li data-testid="missing">{formatNumber(null)}</li>
      </ul>
    );
  }

  it("groups digits, writes a percentage, and scales a byte count", () => {
    render(<Probe value={2_621_440} />);

    // `tests/setup.ts` pins the locale to English, so these are the English
    // conventions — the point under test is that the values go through `Intl`
    // rather than through string concatenation, which is what makes them follow
    // the reader's language at all.
    expect(screen.getByTestId("number").textContent).toBe("2,621,440");
    expect(screen.getByTestId("percent").textContent).toBe("42.5%");
    expect(screen.getByTestId("bytes").textContent).toBe("2.5 MB");
  });

  it("renders a missing figure as an em dash rather than as zero", () => {
    // The same rule the dashboard states: an average over no observations and a
    // measured zero are different facts.
    render(<Probe value={0} />);

    expect(screen.getByTestId("missing").textContent).toBe("—");
    expect(screen.getByTestId("bytes").textContent).toBe("0 B");
  });
});

// --------------------------------------------------------------------------- //
// Direction
// --------------------------------------------------------------------------- //

describe("right-to-left", () => {
  it("declares a direction for every supported language", () => {
    for (const locale of LOCALES) {
      expect(LOCALE_DIRECTIONS[locale]).toMatch(/^(ltr|rtl)$/);
    }
  });

  it("uses logical spacing utilities rather than physical ones", async () => {
    // The whole of RTL support outside the document's `dir`: a component that
    // named a physical edge would need a mirrored stylesheet, which is a second
    // layout to keep in step. Asserted on the shipped source rather than on a
    // rendered page, because the failure is a class name a reviewer would miss.
    const { readFileSync, readdirSync, statSync } = await import("node:fs");
    const { join } = await import("node:path");

    const offenders: string[] = [];
    const physical =
      /(?<![\w-])(?:[a-z0-9-]+:)*-?(?:ml-|mr-|pl-|pr-|left-|right-|text-left|text-right|border-l\b|border-r\b)/;

    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir)) {
        const path = join(dir, entry);
        // `components/ui` holds generated shadcn/ui primitives, which
        // `ai-workflow-rules.md` lists as protected files.
        if (statSync(path).isDirectory()) {
          if (entry !== "ui") walk(path);
          continue;
        }
        if (!entry.endsWith(".tsx")) continue;

        for (const line of readFileSync(path, "utf8").split("\n")) {
          // Only class strings, so prose in a comment cannot trip this.
          const classes = line.match(/className=(?:"[^"]*"|\{[^}]*\})/g) ?? [];
          for (const chunk of classes) {
            if (physical.test(chunk)) offenders.push(`${path}: ${chunk.trim()}`);
          }
        }
      }
    };

    walk("components");
    expect(offenders).toEqual([]);
  });
});
