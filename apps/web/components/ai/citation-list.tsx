"use client";

import * as React from "react";
import Link from "next/link";
import { ChevronDown, FileText } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { caseRoute } from "@/lib/routes";
import { citationRelevancePercent, type AssistantCitation } from "@/types/assistant";

/**
 * The sources an answer was grounded in.
 *
 * **Displayed exactly as the RAG pipeline produced them.** The spec is explicit —
 * *"The AI Assistant should display citations without modifying them"* — so
 * nothing here re-orders, re-numbers, merges, or filters: the marker beside a
 * source is the same `[2]` the prose cites, because the pipeline assigned it
 * before the model ever wrote a word.
 *
 * Four decisions worth stating, because each is about what a *legal* citation has
 * to be:
 *
 * * **the reference is complete and always visible**: file name, version, page.
 *   A generated statement with no provenance is unusable in a legal context, and
 *   the page is what a citation actually points at.
 * * **the excerpt is collapsed, not omitted.** It is the evidence the answer
 *   rests on, so it must be readable — but a list of eight full passages above
 *   the answer would bury it. Expanding is one click and the state is per
 *   citation.
 * * **a source the answer did not cite is still listed, and marked.** The
 *   pipeline returns every source it was given: a model that forgot a marker has
 *   not made the evidence disappear, and hiding those would quietly overstate how
 *   much of the retrieved material the answer used.
 * * **`dir="auto"`** so an Arabic excerpt renders right-to-left beside a French
 *   one, without this component detecting script itself.
 *
 * Each source links to its **case**, never to a document viewer: the case is the
 * one destination its reader is certainly entitled to open — the API would not
 * have returned the passage otherwise.
 */
export function CitationList({ citations }: { citations: AssistantCitation[] }) {
  const [expanded, setExpanded] = React.useState<Set<number>>(() => new Set());

  if (citations.length === 0) return null;

  const documents = new Set(citations.map((citation) => citation.documentId)).size;

  function toggle(marker: number) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(marker)) next.delete(marker);
      else next.add(marker);
      return next;
    });
  }

  return (
    <section className="flex flex-col gap-2" aria-label="Sources">
      <p className="text-xs font-medium text-muted-foreground">
        {/* Distinct documents rather than citations: two passages of one contract
            are one source to a lawyer. */}
        {citations.length} source{citations.length === 1 ? "" : "s"} from {documents} document
        {documents === 1 ? "" : "s"}
      </p>

      <ul className="flex flex-col gap-2">
        {citations.map((citation) => {
          const open = expanded.has(citation.marker);
          const relevance = citationRelevancePercent(citation.score);

          return (
            <li
              key={`${citation.marker}-${citation.documentId}-${citation.pageNumber}`}
              className="rounded-lg border border-border bg-card/50 p-3"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="flex min-w-0 items-start gap-2">
                  <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded bg-muted text-xs font-medium tabular-nums text-muted-foreground">
                    {citation.marker}
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-foreground">
                      <FileText
                        className="mr-1 inline h-4 w-4 align-text-bottom text-muted-foreground"
                        aria-hidden="true"
                      />
                      {citation.documentName}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      Page {citation.pageNumber} · Version {citation.documentVersion}
                    </p>
                  </div>
                </div>

                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  {/* The number *and* the word, never colour alone (WCAG AA). */}
                  <Badge variant="secondary" title={`Similarity ${citation.score}`}>
                    {relevance}% match
                  </Badge>
                  {!citation.referenced ? (
                    <Badge variant="outline" title="Retrieved, but not cited in the answer">
                      Not cited
                    </Badge>
                  ) : null}
                </div>
              </div>

              <div className="mt-2 flex items-center justify-between gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => toggle(citation.marker)}
                  aria-expanded={open}
                  className="h-7 px-2 text-xs"
                >
                  <ChevronDown
                    className={`h-4 w-4 transition-transform ${open ? "rotate-180" : ""}`}
                    aria-hidden="true"
                  />
                  {open ? "Hide excerpt" : "Show excerpt"}
                </Button>
                <Link
                  href={caseRoute(citation.caseId)}
                  className="text-xs font-medium text-primary underline-offset-4 hover:underline"
                >
                  Open case
                </Link>
              </div>

              {open ? (
                <p
                  dir="auto"
                  className="mt-2 whitespace-pre-wrap rounded-md bg-muted/50 p-2 text-sm leading-relaxed text-secondary-foreground"
                >
                  {citation.excerpt}
                  {citation.excerptTruncated ? (
                    <span className="mt-1 block text-xs text-muted-foreground">
                      This passage was shortened to fit the answer&apos;s context budget.
                    </span>
                  ) : null}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
