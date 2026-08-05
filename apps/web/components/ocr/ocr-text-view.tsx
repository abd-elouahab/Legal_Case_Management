"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { EmptyState } from "@/components/shared/empty-state";
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
      toast.error("Your browser did not allow copying. Select the text instead.");
    }
  }

  return (
    <Button type="button" variant="outline" size="sm" onClick={() => void copy()}>
      {copied ? (
        <>
          <Check className="h-4 w-4" aria-hidden="true" />
          Copied
        </>
      ) : (
        <>
          <Copy className="h-4 w-4" aria-hidden="true" />
          Copy all text
        </>
      )}
    </Button>
  );
}

export function OcrTextView({ text }: { text: OcrText }) {
  if (text.pages.length === 0) {
    return (
      <EmptyState
        title="No text was extracted"
        description="This document produced no readable text. It may be a blank scan or contain images only."
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          {text.pageCount} page{text.pageCount === 1 ? "" : "s"} ·{" "}
          {text.characterCount.toLocaleString()} characters
          {text.detectedLanguage ? ` · ${text.detectedLanguage}` : ""}
        </p>
        <CopyTextButton text={text.fullText} />
      </div>

      <div className="flex flex-col gap-4">
        {text.pages.map((page, index) => (
          <div key={page.pageNumber} className="flex flex-col gap-2">
            {index > 0 ? <Separator /> : null}
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Page {page.pageNumber}
              </h4>
              {page.confidence !== null ? (
                <span className="text-xs text-muted-foreground">
                  {Math.round(page.confidence)}% confidence
                </span>
              ) : null}
            </div>

            {page.isEmpty ? (
              <p className="text-sm italic text-muted-foreground">
                This page produced no text.
              </p>
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
