"use client";

import { useTranslations } from "next-intl";
import { Info, Scissors } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { isRtlLanguage, type ReportSection } from "@/types/report";

/**
 * A finished report's body, section by section.
 *
 * **Rendered as written, not as Markdown.** Interpreting generated text as markup
 * would mean deciding what to do with a `[1]` citation marker, a `#` from a
 * statute reference, and an underscore in a filename — and rendering generated
 * text as HTML in a legal platform is a much larger decision than it looks. The
 * same rule the AI Assistant's answers follow, for the same reason.
 *
 * Line breaks *are* preserved (`whitespace-pre-wrap`), because a model that wrote
 * a chronology as one item per line meant it, and collapsing that into a
 * paragraph would destroy the one structure the section has.
 *
 * **A section the case file does not cover is shown, and marked.** Hiding it
 * would leave a reader to conclude the report simply forgot to mention the
 * parties; the badge and the note say plainly that the documents do not support
 * it, which is a finding rather than an omission.
 *
 * `dir="auto"` throughout, so an Arabic report renders right-to-left without this
 * component detecting script — and the headings get it too, because a heading is
 * as much a piece of the report's language as its prose.
 */
export function ReportSections({
  sections,
  language,
}: {
  sections: ReportSection[];
  language: string;
}) {
  const t = useTranslations("reports.sections");

  if (sections.length === 0) return null;

  const rtl = isRtlLanguage(language);

  return (
    <div className="flex flex-col gap-6">
      {sections.map((section) => (
        <section key={section.key} className="flex flex-col gap-2">
          <div
            className={`flex flex-wrap items-center gap-2 ${rtl ? "flex-row-reverse justify-end" : ""}`}
          >
            <h3 className="text-base font-semibold text-foreground" dir="auto">
              {section.title}
            </h3>
            {section.grounded ? null : (
              <Badge variant="outline" className="gap-1.5 border-warning/30 bg-warning/10 text-warning">
                <Info className="h-3.5 w-3.5" aria-hidden="true" />
                {t("notCovered")}
              </Badge>
            )}
            {/* A section cut off at the model's output ceiling is the one way
                this view could actively mislead: it reads as a complete finding
                that happens to be short. The same flag the AI Assistant shows on
                a truncated answer. */}
            {section.truncated ? (
              <Badge
                variant="outline"
                className="gap-1.5 border-warning/30 bg-warning/10 text-warning"
              >
                <Scissors className="h-3.5 w-3.5" aria-hidden="true" />
                {t("stopsEarly")}
              </Badge>
            ) : null}
          </div>

          <p
            dir="auto"
            className="whitespace-pre-wrap text-sm leading-relaxed text-secondary-foreground"
          >
            {section.content}
          </p>

          {section.grounded && section.citationMarkers.length > 0 ? (
            <p className="text-xs text-muted-foreground" dir="auto">
              {t("sources", {
                markers: section.citationMarkers
                  .map((marker) => `[${marker}]`)
                  .join(" "),
              })}
            </p>
          ) : null}
        </section>
      ))}
    </div>
  );
}
