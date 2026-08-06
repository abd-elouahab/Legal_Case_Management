/**
 * Semantic Search API calls.
 *
 * Thin, typed wrappers over the `/search` endpoints. Every response is validated
 * with Zod and mapped from the API's snake_case wire format to the app's
 * camelCase domain types, so nothing above this layer sees a wire shape — and a
 * backend change that alters a payload fails here, loudly, instead of surfacing
 * as `undefined` in a result card.
 *
 * **Retrieval only.** There is no call here that asks for an answer, a summary,
 * or a completion, because the API exposes none: this feature returns passages
 * that already exist in the corpus, and anything generated from them belongs to
 * the RAG pipeline.
 */

import { apiRequest } from "@/lib/api/client";
import { SEARCH_ENDPOINTS } from "@/lib/api/config";
import { searchMetricsSchema, searchResponseSchema } from "@/lib/validation/search";
import type {
  SearchFilters,
  SearchMetrics,
  SearchQuery,
  SearchResponse,
  SearchResult,
  SearchResultDocument,
} from "@/types/search";

type SearchResponseWire = ReturnType<typeof searchResponseSchema.parse>;
type SearchResultWire = SearchResponseWire["results"][number];
type SearchResultDocumentWire = NonNullable<SearchResultWire["document"]>;
type SearchMetricsWire = ReturnType<typeof searchMetricsSchema.parse>;

function toResultDocument(
  payload: SearchResultDocumentWire | null,
): SearchResultDocument | null {
  if (!payload) return null;
  return {
    id: payload.id,
    caseId: payload.case_id,
    originalFilename: payload.original_filename,
    fileExtension: payload.file_extension,
    category: payload.category,
  };
}

function toSearchResult(payload: SearchResultWire): SearchResult {
  return {
    documentId: payload.document_id,
    documentVersion: payload.document_version,
    caseId: payload.case_id,
    pageNumber: payload.page_number,
    chunkNumber: payload.chunk_number,
    score: payload.score,
    text: payload.text,
    language: payload.language,
    rank: payload.rank,
    document: toResultDocument(payload.document),
  };
}

function toSearchResponse(payload: SearchResponseWire): SearchResponse {
  return {
    query: payload.query,
    results: payload.results.map(toSearchResult),
    resultCount: payload.result_count,
    limit: payload.limit,
    offset: payload.offset,
    hasMore: payload.has_more,
    durationMs: payload.duration_ms,
    topScore: payload.top_score,
    averageScore: payload.average_score,
    isEmpty: payload.is_empty,
  };
}

function toSearchMetrics(payload: SearchMetricsWire): SearchMetrics {
  return {
    since: payload.since,
    totalSearches: payload.total_searches,
    successfulSearches: payload.successful_searches,
    failedSearches: payload.failed_searches,
    successRate: payload.success_rate,
    failureRate: payload.failure_rate,
    averageLatencyMs: payload.average_latency_ms,
    averageLatencySeconds: payload.average_latency_seconds,
    averageScore: payload.average_score,
    totalResults: payload.total_results,
    averageResults: payload.average_results,
    failuresByCode: payload.failures_by_code,
    embeddingModel: payload.embedding_model,
    embeddingDimensions: payload.embedding_dimensions,
    embeddingAvailable: payload.embedding_available,
    vectorCollection: payload.vector_collection,
    vectorStoreAvailable: payload.vector_store_available,
    ranker: payload.ranker,
    defaultLimit: payload.default_limit,
    maxLimit: payload.max_limit,
    minScore: payload.min_score,
    enabled: payload.enabled,
  };
}

/**
 * Build the filter object sent in the request body.
 *
 * "Any" filters are omitted rather than sent as nulls, so the body reflects what
 * is actually being asked — which also keeps the request a stable cache key for
 * TanStack Query. An empty array is dropped too: "filter by nothing" and "do not
 * filter" are the same intent, and sending the first would match nothing.
 */
export function buildSearchFilters(filters: SearchFilters): Record<string, unknown> {
  const body: Record<string, unknown> = {};

  if (filters.caseId) body.case_id = filters.caseId;
  if (filters.documentId) body.document_id = filters.documentId;
  if (filters.documentVersion) body.document_version = filters.documentVersion;
  if (filters.languages?.length) body.languages = filters.languages;
  if (filters.categories?.length) body.categories = filters.categories;
  if (filters.fileTypes?.length) body.file_types = filters.fileTypes;
  if (filters.indexedFrom) body.indexed_from = filters.indexedFrom;
  if (filters.indexedTo) body.indexed_to = filters.indexedTo;

  return body;
}

/**
 * Retrieve the passages most relevant to a natural-language query.
 *
 * A POST because the query travels in the body: a query string would be written
 * to the proxy's access log, the browser's history, and the `Referer` header of
 * anything loaded next. It is still a read — nothing is created, and the API
 * answers 200.
 *
 * Which passages come back is decided by the API per case assignment; this
 * function has no say in it and could not widen it if it tried.
 */
export async function searchDocuments(query: SearchQuery): Promise<SearchResponse> {
  const raw = await apiRequest<unknown>(SEARCH_ENDPOINTS.search, {
    method: "POST",
    body: {
      query: query.query,
      limit: query.limit,
      offset: query.offset,
      ...(query.minScore !== null ? { min_score: query.minScore } : {}),
      filters: buildSearchFilters(query.filters),
    },
  });
  return toSearchResponse(searchResponseSchema.parse(raw));
}

/** Fetch platform-wide retrieval health. Requires `search:monitor`. */
export async function fetchSearchMetrics(): Promise<SearchMetrics> {
  const raw = await apiRequest<unknown>(SEARCH_ENDPOINTS.metrics);
  return toSearchMetrics(searchMetricsSchema.parse(raw));
}
