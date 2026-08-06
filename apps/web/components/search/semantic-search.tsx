"use client";

import * as React from "react";
import { ChevronLeft, ChevronRight, Search as SearchIcon, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { SearchFiltersBar } from "@/components/search/search-filters-bar";
import { SearchResultCard } from "@/components/search/search-result-card";
import { Spinner } from "@/components/shared/spinner";
import {
  isSearchUnavailable,
  searchErrorMessage,
  SEARCH_PAGE_SIZE,
  useSearchSession,
} from "@/hooks/use-search";
import { MAX_QUERY_LENGTH, searchFormSchema } from "@/lib/validation/search";
import { EMPTY_SEARCH_FILTERS, type SearchFilters } from "@/types/search";

/**
 * Semantic search over the case file.
 *
 * The whole feature's UI: a query box, the metadata filters, and the ranked
 * passages. Four behaviours are deliberate, and each of them follows from what
 * the API actually promises:
 *
 * * **the search runs on submit, never as you type.** Every request costs a query
 *   embedding on the server, so a search-as-you-type box would embed every prefix
 *   of every question. It is also what a legal search *is*: a question someone
 *   finished writing.
 * * **paging re-runs the submitted query, not what is in the box.** Keeping the
 *   two apart is what stops "edit the box, press Next" from silently searching
 *   for something else — see {@link useSearchSession}.
 * * **"no results" and "not searched yet" are different states**, and neither is
 *   an error. An empty corpus is an answer; the API says so with `isEmpty` on a
 *   200.
 * * **a dependency outage says which one**, because "search is unavailable" sends
 *   a user to retry forever while "the search index is unreachable" sends them to
 *   an administrator.
 *
 * Which passages come back is decided by the API per case assignment. Nothing
 * here filters results, and nothing here could widen them.
 */
export function SemanticSearch({
  /** Pins every search to one case, for the case workspace. */
  caseId,
  /** Shown above the box; the page supplies its own heading otherwise. */
  compact = false,
}: {
  caseId?: string;
  compact?: boolean;
} = {}) {
  const session = useSearchSession();
  const [input, setInput] = React.useState("");
  const [filters, setFilters] = React.useState<SearchFilters>(() =>
    caseId ? { ...EMPTY_SEARCH_FILTERS, caseId } : EMPTY_SEARCH_FILTERS,
  );
  const [validationError, setValidationError] = React.useState<string | null>(null);

  // A case-scoped panel keeps its case filter whatever the filter bar does: the
  // panel is *of* that case, and "clear filters" must not widen the search to
  // the whole platform — the same rule the embedded document list follows.
  const effectiveFilters = React.useMemo(
    () => (caseId ? { ...filters, caseId } : filters),
    [caseId, filters],
  );

  function submit(event: React.FormEvent) {
    event.preventDefault();

    const parsed = searchFormSchema.safeParse({ query: input });
    if (!parsed.success) {
      setValidationError(parsed.error.issues[0]?.message ?? "Enter a search query.");
      return;
    }

    setValidationError(null);
    session.run({
      query: parsed.data.query,
      limit: SEARCH_PAGE_SIZE,
      filters: effectiveFilters,
    });
  }

  const response = session.response;
  const unavailable = isSearchUnavailable(session.error);

  return (
    <section className="flex flex-col gap-6" aria-labelledby="semantic-search-heading">
      <h2 id="semantic-search-heading" className="sr-only">
        Semantic search
      </h2>

      <Card>
        <CardContent className="flex flex-col gap-4 py-4">
          <form onSubmit={submit} className="flex flex-col gap-2" role="search">
            <Label htmlFor="semantic-search-query">
              {compact ? "Search this case's documents" : "Search your documents"}
            </Label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <div className="relative flex-1">
                <SearchIcon
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="semantic-search-query"
                  type="search"
                  value={input}
                  maxLength={MAX_QUERY_LENGTH}
                  onChange={(event) => setInput(event.target.value)}
                  placeholder="Ask in your own words — for example, when is the rent payable?"
                  className="pl-9"
                  aria-invalid={validationError !== null}
                  aria-describedby={
                    validationError ? "semantic-search-query-error" : undefined
                  }
                />
              </div>
              <Button type="submit" disabled={session.isSearching}>
                {session.isSearching ? (
                  <>
                    <Spinner className="h-4 w-4" />
                    Searching
                  </>
                ) : (
                  <>
                    <SearchIcon className="h-4 w-4" aria-hidden="true" />
                    Search
                  </>
                )}
              </Button>
            </div>
            {validationError ? (
              <p
                id="semantic-search-query-error"
                role="alert"
                className="text-sm text-destructive"
              >
                {validationError}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Natural language, in Arabic, French, or English — a question in one
                language finds passages written in the others.
              </p>
            )}
          </form>

          <SearchFiltersBar
            filters={filters}
            onChange={setFilters}
            showCaseFilter={!caseId}
            disabled={session.isSearching}
          />
        </CardContent>
      </Card>

      {session.error ? (
        <p
          role="alert"
          className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
        >
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>
            {searchErrorMessage(session.error)}
            {unavailable ? " Your documents and their text are unaffected." : null}
          </span>
        </p>
      ) : null}

      {session.isSearching ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          {Array.from({ length: 3 }, (_, index) => (
            <Skeleton key={index} className="h-32 rounded-xl" />
          ))}
        </div>
      ) : null}

      {!session.isSearching && !session.error && session.submitted && response ? (
        response.results.length === 0 ? (
          <EmptyState
            icon={SearchIcon}
            title="No matching passages"
            description={
              "Nothing in the documents you can access is close to that question. Try " +
              "different wording, or widen the filters. Documents are searchable once " +
              "their text has been extracted and indexed."
            }
          />
        ) : (
          <div className="flex flex-col gap-4">
            <p className="text-sm text-muted-foreground" aria-live="polite">
              {response.resultCount} passage{response.resultCount === 1 ? "" : "s"} for{" "}
              <span className="font-medium text-foreground">
                &ldquo;{response.query}&rdquo;
              </span>
              {response.topScore !== null
                ? ` · best match ${Math.round(response.topScore * 100)}%`
                : null}{" "}
              · {response.durationMs} ms
            </p>

            <div className="flex flex-col gap-3">
              {response.results.map((result) => (
                <SearchResultCard
                  key={`${result.documentId}-${result.documentVersion}-${result.chunkNumber}`}
                  result={result}
                />
              ))}
            </div>

            {/* Page numbers only, with no total: a similarity search has no cheap
                exact count, and the API deliberately reports `hasMore` rather
                than a figure it would have to guess at. */}
            {response.hasMore || session.page > 0 ? (
              <div className="flex items-center justify-between gap-4">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={session.previousPage}
                  disabled={session.page === 0 || session.isSearching}
                >
                  <ChevronLeft className="h-4 w-4" aria-hidden="true" />
                  Previous
                </Button>
                <span className="text-sm text-muted-foreground">
                  Page {session.page + 1}
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={session.nextPage}
                  disabled={!response.hasMore || session.isSearching}
                >
                  Next
                  <ChevronRight className="h-4 w-4" aria-hidden="true" />
                </Button>
              </div>
            ) : null}
          </div>
        )
      ) : null}

      {!session.submitted && !session.isSearching && !session.error ? (
        <EmptyState
          icon={SearchIcon}
          title="Nothing searched yet"
          description={
            "Ask a question in your own words. Results are passages from the documents " +
            "on cases you have access to, each with the file, page, and relevance."
          }
        />
      ) : null}
    </section>
  );
}
