"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { Check, Copy } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { EmptyState } from "@/components/shared/empty-state";
import { useNumberFormat } from "@/hooks/use-number-format";
import type { OcrText } from "@/types/ocr";

/**
 * The extracted text of a document, rendered page by page.
 *
 * **Pages are rendered as pages**, with their numbers, rather than as one block:
 * the boundaries are what a lawyer needs in order to cite a passage, and they are
 * what a later indexing feature will chunk on. Joining them into a single scroll
 * would throw away the one piece of structure OCR recovers.
 *
 * `whitespace-pre-wrap` and a monospace face keep the layout the engine
 * recognised — indentation, aligned columns in a table of costs, the shape of a
 * signature block — which a proportional font with collapsed whitespace would
 * silently destroy. `dir="auto"` lets the browser pick the direction per page, so
 * an Arabic page renders right-to-left beside a French one without the platform
 * having to detect the script itself.
 */

function CopyTextButton({ text }: { text: string }) {
  const [copied, setCopied] = React.useState(false);
  const t = useTranslations("ocr.text");
  const tActions = useTranslations("common.actions");

  React.useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2_000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // Clipboard access is refused in insecure contexts and by some browser
      // settings. Saying so beats a button that silently does nothing.
      toast.error(t("copyRefused"));
    }
  }

  return (
    <Button type="button" variant="outline" size="sm" onClick={() => void copy()}>
      {copied ? (
        <>
          <Check className="h-4 w-4" aria-hidden="true" />
          {tActions("copied")}
        </>
      ) : (
        <>
          <Copy className="h-4 w-4" aria-hidden="true" />
          {t("copyAll")}
        </>
      )}
    </Button>
  );
}

export function OcrTextView({ text }: { text: OcrText }) {
  const t = useTranslations("ocr.text");
  const tLanguages = useTranslations("common.languages");
  const { formatNumber } = useNumberFormat();

  if (text.pages.length === 0) {
    return (
      <EmptyState
        titleKey="ocr.text.emptyTitle"
        descriptionKey="ocr.text.emptyDescription"
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* One ICU message rather than a count, a conditional "s", and a
            hand-formatted number: the plural rule, the separator, and the digits
            are all language-dependent. */}
        <p className="text-sm text-muted-foreground">
          {t("summary", {
            pages: text.pageCount,
            characters: formatNumber(text.characterCount),
          })}
          {text.detectedLanguage
            ? ` · ${tLanguages(text.detectedLanguage)}`
            : ""}
        </p>
        <CopyTextButton text={text.fullText} />
      </div>

      <div className="flex flex-col gap-4">
        {text.pages.map((page, index) => (
          <div key={page.pageNumber} className="flex flex-col gap-2">
            {index > 0 ? <Separator /> : null}
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {t("page", { number: page.pageNumber })}
              </h4>
              {page.confidence !== null ? (
                <span className="text-xs text-muted-foreground">
                  {t("confidence", { value: Math.round(page.confidence) })}
                </span>
              ) : null}
            </div>

            {page.isEmpty ? (
              <p className="text-sm italic text-muted-foreground">{t("emptyPage")}</p>
            ) : (
              <p
                dir="auto"
                className="max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground"
              >
                {page.text}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
