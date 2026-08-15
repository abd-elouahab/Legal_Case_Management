"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { ChevronDown, FileText } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { caseRoute } from "@/lib/routes";
import { citationRelevancePercent, type AssistantCitation } from "@/types/assistant";

/**
 * The sources an answer was grounded in.
 *
 * **Nothing here modifies a citation.** The spec is explicit — *"The AI Assistant
 * should display citations without modifying them"* — so no marker is renumbered,
 * no source is merged, and none is discarded: the `[2]` beside a source is the
 * same `[2]` the prose cites, because the pipeline assigned it before the model
 * wrote a word. What this component decides is only **what is on screen first**.
 *
 * **Three levels of disclosure, because a citation list and a retrieval log are
 * different things wearing the same clothes.** Showing eight full-width cards
 * under a three-sentence answer — five of them labelled "Not cited", each with a
 * cosine score — makes the evidence outweigh the answer and reads as a debug
 * panel. It also invites a specific misreading: "55% match" looks like the
 * platform's confidence in the *answer*, when it is the distance between two
 * embeddings and 55% is a strong score.
 *
 * So:
 *
 * 1. **collapsed** — one line: how many sources, and which document. Enough to
 *    know the answer is sourced and from what, which is what a reader needs
 *    before deciding to check anything;
 * 2. **expanded** — the sources the answer actually cited, each with its page and
 *    a collapsible excerpt. This is the citation list proper;
 * 3. **inside that** — the passages retrieval returned that the answer did *not*
 *    cite, with their scores. Still present, still counted, never removed: a
 *    model that forgot a marker has not made the evidence disappear, and this is
 *    the view that makes a thin answer diagnosable. Just not first.
 *
 * The split is presentational and reversible — every citation the pipeline
 * returned is reachable in two clicks, and the count is stated even when
 * collapsed, so nothing is hidden in the sense that matters.
 *
 * Two details that are about *legal* citation rather than layout:
 *
 * * **the reference is complete wherever it appears**: file name, version, page.
 *   The page is what a citation actually points at, and a generated statement
 *   with no provenance is unusable in a legal context.
 * * **`dir="auto"`** on every excerpt, so an Arabic passage renders right-to-left
 *   beside a French one without this component detecting script itself.
 *
 * Each source links to its **case**, never to a document viewer: the case is the
 * one destination its reader is certainly entitled to open — the API would not
 * have returned the passage otherwise.
 */
export function CitationList({ citations }: { citations: AssistantCitation[] }) {
  const [open, setOpen] = React.useState(false);
  const [showUncited, setShowUncited] = React.useState(false);
  const [expanded, setExpanded] = React.useState<Set<number>>(() => new Set());
  const t = useTranslations("assistant.citations");

  if (citations.length === 0) return null;

  // Partitioned, not sorted: within each group the pipeline's own relevance order
  // is preserved untouched.
  const cited = citations.filter((citation) => citation.referenced);
  const uncited = citations.filter((citation) => !citation.referenced);

  const documents = new Set(citations.map((citation) => citation.documentId)).size;

  function toggleExcerpt(marker: number) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(marker)) next.delete(marker);
      else next.add(marker);
      return next;
    });
  }

  function renderCitation(citation: AssistantCitation, { withScore }: { withScore: boolean }) {
    const isOpen = expanded.has(citation.marker);

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
                  className="me-1 inline h-4 w-4 align-text-bottom text-muted-foreground"
                  aria-hidden="true"
                />
                {citation.documentName}
              </p>
              <p className="text-xs text-muted-foreground">
                {t("pageAndVersion", {
                  page: citation.pageNumber,
                  version: citation.documentVersion,
                })}
              </p>
            </div>
          </div>

          {/* The score is shown only among the uncited passages, where relevance
              is the sole reason the passage is on screen at all. Beside a cited
              source it answers a question nobody asked and is read as confidence
              in the answer. */}
          {withScore ? (
            <Badge
              variant="secondary"
              className="shrink-0"
              title={t("similarity", { score: citation.score })}
            >
              {t("match", { percent: citationRelevancePercent(citation.score) })}
            </Badge>
          ) : null}
        </div>

        <div className="mt-2 flex items-center justify-between gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => toggleExcerpt(citation.marker)}
            aria-expanded={isOpen}
            className="h-7 px-2 text-xs"
          >
            <ChevronDown
              className={`h-4 w-4 transition-transform ${isOpen ? "rotate-180" : ""}`}
              aria-hidden="true"
            />
            {isOpen ? t("hideExcerpt") : t("showExcerpt")}
          </Button>
          <Link
            href={caseRoute(citation.caseId)}
            className="text-xs font-medium text-primary underline-offset-4 hover:underline"
          >
            {t("openCase")}
          </Link>
        </div>

        {isOpen ? (
          <p
            dir="auto"
            className="mt-2 whitespace-pre-wrap rounded-md bg-muted/50 p-2 text-sm leading-relaxed text-secondary-foreground"
          >
            {citation.excerpt}
            {citation.excerptTruncated ? (
              <span className="mt-1 block text-xs text-muted-foreground">{t("truncated")}</span>
            ) : null}
          </p>
        ) : null}
      </li>
    );
  }

  return (
    <section className="flex flex-col gap-2" aria-label={t("label")}>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        aria-label={open ? t("hideSources") : t("showSources")}
        className="h-auto w-fit justify-start px-2 py-1 text-xs font-medium text-muted-foreground"
      >
        <ChevronDown
          className={`h-4 w-4 shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
          aria-hidden="true"
        />
        <span className="truncate">
          {/* Naming the document is what makes the collapsed line worth reading:
              "2 sources" alone says nothing a reader can act on. With more than
              one document a name would have to be picked arbitrarily, so the
              count of documents is used instead — two passages of one contract
              are one source to a lawyer. */}
          {cited.length === 0
            ? t("noneCited", { count: citations.length })
            : documents === 1
              ? t("summaryOneDocument", {
                  sources: cited.length,
                  document: citations[0].documentName,
                })
              : t("summary", { sources: cited.length, documents })}
        </span>
      </Button>

      {open ? (
        <>
          {cited.length > 0 ? (
            <ul className="flex flex-col gap-2">
              {cited.map((citation) => renderCitation(citation, { withScore: false }))}
            </ul>
          ) : null}

          {uncited.length > 0 ? (
            <>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setShowUncited((current) => !current)}
                aria-expanded={showUncited}
                className="h-auto w-fit justify-start px-2 py-1 text-xs text-muted-foreground"
              >
                <ChevronDown
                  className={`h-4 w-4 shrink-0 transition-transform ${showUncited ? "rotate-180" : ""}`}
                  aria-hidden="true"
                />
                {showUncited ? t("hideRetrieved") : t("moreRetrieved", { count: uncited.length })}
              </Button>

              {showUncited ? (
                <ul className="flex flex-col gap-2">
                  {uncited.map((citation) => renderCitation(citation, { withScore: true }))}
                </ul>
              ) : null}
            </>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
