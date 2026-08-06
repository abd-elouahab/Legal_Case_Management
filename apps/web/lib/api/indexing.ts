/**
 * Document Indexing API calls.
 *
 * Thin, typed wrappers over the `/documents/{id}/index*` and `/indexing`
 * endpoints. Every response is validated with Zod and mapped from the API's
 * snake_case wire format to the app's camelCase domain types, so nothing above
 * this layer sees a wire shape — and a backend change that alters a payload fails
 * here, loudly, instead of surfacing as `undefined` in a status badge.
 *
 * **No search call, deliberately.** The API exposes none: reading the index back
 * is Semantic Search's feature, and this module reports only what was built.
 */

import { apiRequest } from "@/lib/api/client";
import { INDEXING_ENDPOINTS } from "@/lib/api/config";
import {
  documentIndexListSchema,
  documentIndexPageSchema,
  documentIndexSchema,
  indexMetricsSchema,
} from "@/lib/validation/indexing";
import type {
  DocumentIndex,
  IndexDocumentSummary,
  IndexMetrics,
} from "@/types/indexing";
import type { DocumentIndexPage, IndexListQuery } from "@/types/indexing-management";

type DocumentIndexWire = ReturnType<typeof documentIndexSchema.parse>;
type IndexDocumentWire = NonNullable<DocumentIndexWire["document"]>;
type IndexMetricsWire = ReturnType<typeof indexMetricsSchema.parse>;

function toIndexDocument(payload: IndexDocumentWire | null): IndexDocumentSummary | null {
  if (!payload) return null;
  return {
    id: payload.id,
    caseId: payload.case_id,
    originalFilename: payload.original_filename,
    fileExtension: payload.file_extension,
  };
}

/** Map one API index record onto the app's {@link DocumentIndex}. */
function toDocumentIndex(payload: DocumentIndexWire): DocumentIndex {
  return {
    id: payload.id,
    documentId: payload.document_id,
    documentVersion: payload.document_version,
    caseId: payload.case_id,
    document: toIndexDocument(payload.document),
    status: payload.status,
    chunkCount: payload.chunk_count,
    pageCount: payload.page_count,
    characterCount: payload.character_count,
    embeddingModel: payload.embedding_model,
    embeddingDimensions: payload.embedding_dimensions,
    vectorCollection: payload.vector_collection,
    chunkSize: payload.chunk_size,
    chunkOverlap: payload.chunk_overlap,
    detectedLanguage: payload.detected_language,
    startedAt: payload.started_at,
    finishedAt: payload.finished_at,
    durationMs: payload.duration_ms,
    durationSeconds: payload.duration_seconds,
    attemptCount: payload.attempt_count,
    errorCode: payload.error_code,
    errorMessage: payload.error_message,
    requestedBy: payload.requested_by,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
    isTerminal: payload.is_terminal,
    isActive: payload.is_active,
    canReindex: payload.can_reindex,
  };
}

function toIndexMetrics(payload: IndexMetricsWire): IndexMetrics {
  return {
    windowDays: payload.window_days,
    totalRuns: payload.total_runs,
    pending: payload.pending,
    indexing: payload.indexing,
    indexed: payload.indexed,
    failed: payload.failed,
    finishedRuns: payload.finished_runs,
    totalChunks: payload.total_chunks,
    averageChunksPerDocument: payload.average_chunks_per_document,
    successRate: payload.success_rate,
    failureRate: payload.failure_rate,
    averageDurationMs: payload.average_duration_ms,
    averageDurationSeconds: payload.average_duration_seconds,
    failuresByCode: payload.failures_by_code,
    embeddingModel: payload.embedding_model,
    embeddingDimensions: payload.embedding_dimensions,
    embeddingAvailable: payload.embedding_available,
    chunker: payload.chunker,
    chunkSize: payload.chunk_size,
    chunkOverlap: payload.chunk_overlap,
    vectorCollection: payload.vector_collection,
    vectorStoreAvailable: payload.vector_store_available,
    vectorCollectionExists: payload.vector_collection_exists,
    storedVectors: payload.stored_vectors,
    enabled: payload.enabled,
  };
}

/**
 * Build the query string for a list request.
 *
 * "Any" filters are omitted rather than sent as blanks, so the request URL
 * reflects what is actually being asked — which also makes the query a stable
 * cache key for TanStack Query.
 */
export function buildIndexListParams(query: IndexListQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
  });

  const optional: Array<[string, string | null]> = [
    ["status", query.status],
    ["document_id", query.documentId],
    ["case_id", query.caseId],
    ["error_code", query.errorCode],
    ["embedding_model", query.embeddingModel],
  ];

  for (const [key, value] of optional) {
    if (value) params.set(key, value);
  }

  return params.toString();
}

/** Fetch a document's index status. Defaults to the current version. */
export async function fetchDocumentIndex(
  documentId: string,
  version?: number,
): Promise<DocumentIndex> {
  const raw = await apiRequest<unknown>(INDEXING_ENDPOINTS.status(documentId, version));
  return toDocumentIndex(documentIndexSchema.parse(raw));
}

/** Fetch every index recorded for a document, oldest version first. */
export async function fetchDocumentIndexHistory(
  documentId: string,
): Promise<DocumentIndex[]> {
  const raw = await apiRequest<unknown>(INDEXING_ENDPOINTS.history(documentId));
  return documentIndexListSchema.parse(raw).map(toDocumentIndex);
}

/**
 * Queue a fresh index for a document whose text is already extracted.
 *
 * Nothing travels: the API takes the document's identifier and re-reads the text
 * it already holds. Answers 202 with the run in its `pending` state, so the
 * caller polls {@link fetchDocumentIndex} for the outcome.
 */
export async function reindexDocument(
  documentId: string,
  version?: number,
): Promise<DocumentIndex> {
  const raw = await apiRequest<unknown>(INDEXING_ENDPOINTS.reindex(documentId, version), {
    method: "POST",
  });
  return toDocumentIndex(documentIndexSchema.parse(raw));
}

/** Fetch one page of the indexing-run list. */
export async function fetchDocumentIndexes(
  query: IndexListQuery,
): Promise<DocumentIndexPage> {
  const raw = await apiRequest<unknown>(
    `${INDEXING_ENDPOINTS.list}?${buildIndexListParams(query)}`,
  );
  const data = documentIndexPageSchema.parse(raw);

  return {
    items: data.items.map(toDocumentIndex),
    totalRecords: data.total_records,
    page: data.page,
    pageSize: data.page_size,
    totalPages: data.total_pages,
  };
}

/** Fetch platform-wide indexing health. Requires `indexing:monitor`. */
export async function fetchIndexMetrics(windowDays?: number): Promise<IndexMetrics> {
  const path = windowDays
    ? `${INDEXING_ENDPOINTS.metrics}?window_days=${windowDays}`
    : INDEXING_ENDPOINTS.metrics;
  const raw = await apiRequest<unknown>(path);
  return toIndexMetrics(indexMetricsSchema.parse(raw));
}
