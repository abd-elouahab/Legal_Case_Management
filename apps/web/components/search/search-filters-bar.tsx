"use client";

import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useDocumentCases } from "@/hooks/use-document-cases";
import { usePermissions } from "@/hooks/use-permissions";
import { PERMISSION } from "@/types/authorization";
import {
  DOCUMENT_CATEGORIES,
  DOCUMENT_CATEGORY_LABELS,
  SUPPORTED_DOCUMENT_EXTENSIONS,
  type DocumentCategory,
} from "@/types/document";
import {
  hasActiveFilters,
  SEARCH_LANGUAGE_LABELS,
  type SearchFilters,
} from "@/types/search";

/**
 * Metadata filters for a search.
 *
 * The spec's list — case, document, version, language, date, document type — as
 * the subset a person actually narrows by: **case, category, file type, and
 * language**. Document, version, and the indexing-date range are supported by
 * the API and reachable through the case-scoped panel and the client, but are
 * deliberately not given controls here: nobody searches "passages indexed
 * between two dates", and a filter row nobody uses costs every user the time to
 * read past it.
 *
 * None of these can widen what the caller reaches. The API applies the case scope
 * inside the vector query, so a filter narrows a set that has already been
 * restricted — and filtering by a case the caller is not party to is refused with
 * a 403 rather than answered with an empty page. The case select only ever offers
 * cases the caller can already see, so that refusal should never be reachable
 * from this UI.
 */

/** Sentinel for "no filter" — a Radix select item may not have an empty value. */
const ANY = "__any__";

/** Languages offered, matching the labels the indexer can produce. */
const LANGUAGES = ["ar", "fr", "en"] as const;

export function SearchFiltersBar({
  filters,
  onChange,
  /** Hides the case select where the case is already fixed by the surroundings. */
  showCaseFilter = true,
  disabled = false,
}: {
  filters: SearchFilters;
  onChange: (filters: SearchFilters) => void;
  showCaseFilter?: boolean;
  disabled?: boolean;
}) {
  const { can } = usePermissions();
  // Only offered to callers who may read the case list; everyone else has no way
  // to resolve a case name from an identifier, so the control would be a list of
  // UUIDs.
  const cases = useDocumentCases({ enabled: showCaseFilter && can(PERMISSION.casesView) });

  function set<Key extends keyof SearchFilters>(
    key: Key,
    value: SearchFilters[Key],
  ): void {
    onChange({ ...filters, [key]: value });
  }

  const isFiltered = hasActiveFilters(filters);

  return (
    <div className="flex flex-col gap-4 lg:flex-row lg:flex-wrap lg:items-end">
      {showCaseFilter && can(PERMISSION.casesView) ? (
        <div className="flex flex-col gap-2 lg:w-64">
          <Label htmlFor="search-case-filter">Case</Label>
          <Select
            value={filters.caseId ?? ANY}
            onValueChange={(value) => set("caseId", value === ANY ? null : value)}
            disabled={disabled}
          >
            <SelectTrigger id="search-case-filter">
              <SelectValue placeholder="All cases" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>All cases</SelectItem>
              {cases.cases.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.caseNumber} — {option.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      ) : null}

      <div className="flex flex-col gap-2 lg:w-52">
        <Label htmlFor="search-category-filter">Category</Label>
        <Select
          value={filters.categories?.[0] ?? ANY}
          onValueChange={(value) =>
            set("categories", value === ANY ? null : [value as DocumentCategory])
          }
          disabled={disabled}
        >
          <SelectTrigger id="search-category-filter">
            <SelectValue placeholder="All categories" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>All categories</SelectItem>
            {DOCUMENT_CATEGORIES.map((option) => (
              <SelectItem key={option} value={option}>
                {DOCUMENT_CATEGORY_LABELS[option]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-2 lg:w-40">
        <Label htmlFor="search-type-filter">File type</Label>
        <Select
          value={filters.fileTypes?.[0] ?? ANY}
          onValueChange={(value) => set("fileTypes", value === ANY ? null : [value])}
          disabled={disabled}
        >
          <SelectTrigger id="search-type-filter">
            <SelectValue placeholder="All types" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>All types</SelectItem>
            {SUPPORTED_DOCUMENT_EXTENSIONS.map((option) => (
              <SelectItem key={option} value={option}>
                {option.toUpperCase()}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-2 lg:w-40">
        <Label htmlFor="search-language-filter">Language</Label>
        <Select
          value={filters.languages?.[0] ?? ANY}
          onValueChange={(value) => set("languages", value === ANY ? null : [value])}
          disabled={disabled}
        >
          <SelectTrigger id="search-language-filter">
            <SelectValue placeholder="All languages" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>All languages</SelectItem>
            {LANGUAGES.map((option) => (
              <SelectItem key={option} value={option}>
                {SEARCH_LANGUAGE_LABELS[option]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {isFiltered ? (
        <Button
          type="button"
          variant="ghost"
          disabled={disabled}
          onClick={() =>
            onChange({
              ...filters,
              caseId: showCaseFilter ? null : filters.caseId,
              categories: null,
              fileTypes: null,
              languages: null,
            })
          }
        >
          <X className="h-4 w-4" aria-hidden="true" />
          Clear filters
        </Button>
      ) : null}
    </div>
  );
}
