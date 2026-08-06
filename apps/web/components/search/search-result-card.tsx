"use client";

import Link from "next/link";
import { FileText } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { caseRoute } from "@/lib/routes";
import { DOCUMENT_CATEGORY_LABELS } from "@/types/document";
import {
  relevancePercent,
  searchLanguageLabel,
  type SearchResult,
} from "@/types/search";

/**
 * One retrieved passage.
 *
 * Four decisions worth stating, because each of them is about what a *legal*
 * search result has to be:
 *
 * * **the passage is shown verbatim and in full.** It is the evidence. Truncating
 *   it with an ellipsis would leave a lawyer unable to tell whether the clause
 *   they need is inside, and would send them to open the document to find out —
 *   which is the work this feature exists to save.
 * * **the citation is complete and adjacent to it**: file name, version, page,
 *   and passage number. A retrieved sentence with no provenance is unusable in a
 *   legal context, and the page is what a citation points at.
 * * **relevance is shown as a percentage with a label, never as colour alone**
 *   (`ui-context.md`, WCAG AA). It is a similarity, so it is presented as one
 *   figure rather than as stars or a verdict — the platform does not decide what
 *   "relevant enough" means for a given matter.
 * * **`dir="auto"`** so an Arabic passage renders right-to-left beside a French
 *   one, exactly as the OCR text view does, without the client detecting script
 *   itself.
 *
 * The card links to the **case**, not to a document viewer: the document dialog
 * is opened from the documents list, and a link that took a reader somewhere they
 * may not be entitled to open would be a broken promise. The case is the one
 * destination every result's reader is certainly party to — the API would not
 * have returned the passage otherwise.
 */
export function SearchResultCard({ result }: { result: SearchResult }) {
  const document = result.document;
  const relevance = relevancePercent(result.score);

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="flex min-w-0 items-start gap-2">
            <FileText
              className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-foreground">
                {document?.originalFilename ?? "Document"}
              </p>
              <p className="text-xs text-muted-foreground">
                Page {result.pageNumber} · Passage {result.chunkNumber + 1} · Version{" "}
                {result.documentVersion}
              </p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            {document ? (
              <Badge variant="outline">
                {DOCUMENT_CATEGORY_LABELS[document.category]}
              </Badge>
            ) : null}
            {result.language ? (
              <Badge variant="outline">{searchLanguageLabel(result.language)}</Badge>
            ) : null}
            {/* The number *and* the word, never colour alone. */}
            <Badge variant="secondary" title={`Similarity ${result.score}`}>
              {relevance}% match
            </Badge>
          </div>
        </div>

        <p
          dir="auto"
          className="whitespace-pre-wrap text-sm leading-relaxed text-secondary-foreground"
        >
          {result.text}
        </p>

        <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
          <span>Result {result.rank}</span>
          <Link
            href={caseRoute(result.caseId)}
            className="font-medium text-primary underline-offset-4 hover:underline"
          >
            Open case
          </Link>
        </div>
      </CardContent>
    </Card>
  );
}
