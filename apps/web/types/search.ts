/**
 * Semantic search types.
 *
 * Mirror the API's search payloads (`apps/api/schemas/search.py`). Union types
 * rather than magic strings, per the code standards: referencing a filter or a
 * failure the platform does not define is a compile error.
 *
 * **Nothing here describes an answer, a summary, a prompt, or a conversation.**
 * Semantic search retrieves passages that already exist in the corpus, verbatim,
 * with their provenance. Anything generated from them belongs to the RAG
 * pipeline and the AI assistant, which are separate features with their own
 * types — and a field for one of them here is how that boundary would quietly
 * move.
 */

import type { DocumentCategory } from "@/types/document";

/** The document a retrieved passage came from, as shown beside it. */
export interface SearchResultDocument {
  id: string;
  caseId: string;
  originalFilename: string;
  fileExtension: string;
  category: DocumentCategory;
}

/**
 * One retrieved passage and where it came from.
 *
 * The seven references the feature spec lists — document, version, case, page,
 * chunk number, score, and the passage itself — plus its language, its rank, and
 * the document summary. Deliberately no point identifier, no embedding model,
 * and no vector: none of them tells a reader anything about the passage they are
 * reading.
 */
export interface SearchResult {
  documentId: string;
  documentVersion: number;
  caseId: string;
  pageNumber: number;
  chunkNumber: number;
  /**
   * Cosine similarity to the query, higher is closer. Comparable across searches
   * and across months, because every vector the platform stores is unit length.
   */
  score: number;
  /** The passage, verbatim as it was indexed. */
  text: string;
  /**
   * ISO 639-1 code, or `und` when it could not be determined. A tolerant string
   * rather than a union: the label is produced per passage by a heuristic that a
   * future backend may extend, and an unrecognised value must still render.
   */
  language: string | null;
  /** 1-based position after ranking. */
  rank: number;
  document: SearchResultDocument | null;
}

/** One page of results. */
export interface SearchResponse {
  /** The normalised query the server actually embedded, echoed back. */
  query: string;
  results: SearchResult[];
  resultCount: number;
  limit: number;
  offset: number;
  /**
   * Whether another page may exist. True when this page filled — a similarity
   * search has no cheap exact total, and the API deliberately reports none
   * rather than a wrong one.
   */
  hasMore: boolean;
  durationMs: number;
  topScore: number | null;
  averageScore: number | null;
  /** Whether the search matched nothing. A successful outcome, not an error. */
  isEmpty: boolean;
}

/**
 * Metadata restrictions on a search.
 *
 * Every field is optional and they combine with AND. **None of them can widen
 * what the caller may reach**: the API applies the case scope inside the vector
 * query, so these narrow a set that has already been restricted. Filtering by a
 * case or document the caller is not party to is refused with a 403 rather than
 * answered with an empty page.
 */
export interface SearchFilters {
  caseId: string | null;
  documentId: string | null;
  documentVersion: number | null;
  languages: string[] | null;
  categories: DocumentCategory[] | null;
  fileTypes: string[] | null;
  indexedFrom: string | null;
  indexedTo: string | null;
}

/** What a search asks for. */
export interface SearchQuery {
  query: string;
  limit: number;
  offset: number;
  minScore: number | null;
  filters: SearchFilters;
}

/** Platform-wide retrieval health. Requires `search:monitor`. */
export interface SearchMetrics {
  /**
   * When the counting window began. Not decoration: the counters live in the API
   * process rather than in a table, so they reset on restart and each instance
   * counts only its own traffic.
   */
  since: string;

  totalSearches: number;
  successfulSearches: number;
  failedSearches: number;
  successRate: number;
  failureRate: number;

  averageLatencyMs: number | null;
  averageLatencySeconds: number | null;
  averageScore: number | null;
  totalResults: number;
  averageResults: number | null;
  failuresByCode: Record<string, number>;

  embeddingModel: string;
  embeddingDimensions: number;
  embeddingAvailable: boolean;

  vectorCollection: string;
  vectorStoreAvailable: boolean;

  ranker: string;
  defaultLimit: number;
  maxLimit: number;
  minScore: number;
  enabled: boolean;
}

/** No filters at all — the starting state of every search form. */
export const EMPTY_SEARCH_FILTERS: SearchFilters = {
  caseId: null,
  documentId: null,
  documentVersion: null,
  languages: null,
  categories: null,
  fileTypes: null,
  indexedFrom: null,
  indexedTo: null,
};

/** Whether a set of filters restricts anything at all. */
export function hasActiveFilters(filters: SearchFilters): boolean {
  return Object.values(filters).some(
    (value) => value !== null && (!Array.isArray(value) || value.length > 0),
  );
}

/**
 * Why a search could not be answered.
 *
 * Open, deliberately: a later embedding backend or vector database may report a
 * cause this build has never heard of, and a failed search must still render a
 * message. {@link searchFailureLabel} falls back — and the API always sends a
 * human-readable message alongside the code.
 */
export const SEARCH_FAILURE_CODES = [
  "embedding_unavailable",
  "vector_store_unavailable",
  "unknown",
] as const;
export type KnownSearchFailureCode = (typeof SEARCH_FAILURE_CODES)[number];

const SEARCH_FAILURE_LABELS: Record<KnownSearchFailureCode, string> = {
  embedding_unavailable: "Search model unavailable",
  vector_store_unavailable: "Search index unavailable",
  unknown: "Search did not complete",
};

/** A short label for a failure cause, falling back to a readable identifier. */
export function searchFailureLabel(code: string | null): string {
  if (!code) return "Search failed";

  const known = SEARCH_FAILURE_LABELS[code as KnownSearchFailureCode];
  if (known) return known;

  const words = code.replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Search failed";
}

/**
 * Languages a passage can be labelled with.
 *
 * ISO 639-1, matching `apps/api/core/indexing.py`. `und` is "undetermined" and
 * is a legitimate answer for a page of figures — not an error.
 */
export const SEARCH_LANGUAGE_LABELS: Record<string, string> = {
  ar: "Arabic",
  fr: "French",
  en: "English",
  und: "Undetermined",
};

/** A readable name for a language code, falling back to the code itself. */
export function searchLanguageLabel(code: string | null): string {
  if (!code) return "Unknown";
  return SEARCH_LANGUAGE_LABELS[code] ?? code.toUpperCase();
}

/**
 * A relevance score as a percentage, for display.
 *
 * Cosine similarity is `[-1, 1]`, and a negative score means the passage points
 * away from the query — clamped to 0 rather than shown as a negative percentage,
 * which reads as a data error rather than as "not very relevant".
 */
export function relevancePercent(score: number): number {
  return Math.round(Math.max(0, Math.min(1, score)) * 100);
}
