/**
 * Zod schemas for Document Indexing.
 *
 * Response validation only — there is no indexing *form*. Every field a run
 * carries is produced by the platform, and the one action a user takes (Rebuild)
 * has an empty body, so there is nothing for a user to fill in and nothing to
 * validate on the way out.
 *
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/indexing.py`; where they must agree, the API is the
 * authority.
 */

import { z } from "zod";

import { INDEX_STATUSES } from "@/types/indexing";

const indexDocumentSummarySchema = z.object({
  id: z.string(),
  case_id: z.string(),
  original_filename: z.string(),
  file_extension: z.string(),
});

/**
 * One indexing run.
 *
 * `status` is a strict enum, unlike a timeline event type: the lifecycle is
 * closed and enforced by a database enum on the server, so an unrecognised value
 * would be a genuine contract break rather than a newer backend being ahead of
 * this build.
 *
 * `error_code`, `embedding_model`, and `detected_language` are tolerant strings
 * for the opposite reason — a future embedding backend, vector store, or language
 * may report a value this build has never heard of, and the API always sends a
 * human-readable `error_message` beside the code.
 */
export const documentIndexSchema = z.object({
  id: z.string(),
  document_id: z.string(),
  document_version: z.number().int().positive(),
  case_id: z.string(),
  document: indexDocumentSummarySchema.nullable().default(null),

  status: z.enum(INDEX_STATUSES),

  chunk_count: z.number().int().nonnegative().nullable().default(null),
  page_count: z.number().int().nonnegative().nullable().default(null),
  character_count: z.number().int().nonnegative().nullable().default(null),

  embedding_model: z.string().nullable().default(null),
  embedding_dimensions: z.number().int().positive().nullable().default(null),
  vector_collection: z.string().nullable().default(null),
  chunk_size: z.number().int().positive().nullable().default(null),
  chunk_overlap: z.number().int().nonnegative().nullable().default(null),
  detected_language: z.string().nullable().default(null),

  started_at: z.string().nullable().default(null),
  finished_at: z.string().nullable().default(null),
  duration_ms: z.number().int().nonnegative().nullable().default(null),
  duration_seconds: z.number().nonnegative().nullable().default(null),
  attempt_count: z.number().int().nonnegative(),

  error_code: z.string().nullable().default(null),
  error_message: z.string().nullable().default(null),

  requested_by: z.string().nullable().default(null),
  created_at: z.string(),
  updated_at: z.string(),

  is_terminal: z.boolean(),
  is_active: z.boolean(),
  can_reindex: z.boolean(),
});

export const documentIndexListSchema = z.array(documentIndexSchema);

export const documentIndexPageSchema = z.object({
  items: z.array(documentIndexSchema),
  total_records: z.number().int().nonnegative(),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total_pages: z.number().int().nonnegative(),
});

export const indexMetricsSchema = z.object({
  window_days: z.number().int().positive().nullable().default(null),

  total_runs: z.number().int().nonnegative(),
  pending: z.number().int().nonnegative(),
  indexing: z.number().int().nonnegative(),
  indexed: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),
  finished_runs: z.number().int().nonnegative(),

  total_chunks: z.number().int().nonnegative(),
  average_chunks_per_document: z.number().nullable().default(null),

  success_rate: z.number(),
  failure_rate: z.number(),
  average_duration_ms: z.number().nullable().default(null),
  average_duration_seconds: z.number().nullable().default(null),
  // Open by construction: the keys are failure codes, which a future backend may
  // extend.
  failures_by_code: z.record(z.string(), z.number().int().nonnegative()).default({}),

  embedding_model: z.string(),
  embedding_dimensions: z.number().int().positive(),
  embedding_available: z.boolean(),

  chunker: z.string(),
  chunk_size: z.number().int().positive(),
  chunk_overlap: z.number().int().nonnegative(),

  vector_collection: z.string(),
  vector_store_available: z.boolean(),
  vector_collection_exists: z.boolean(),
  stored_vectors: z.number().int().nonnegative().nullable().default(null),

  enabled: z.boolean(),
});

/** The list query, validated before it becomes a query string. */
export const indexListQuerySchema = z.object({
  page: z.coerce.number().int().min(1).catch(1),
  pageSize: z.coerce.number().int().min(1).max(100).catch(20),
  status: z.enum(INDEX_STATUSES).nullable().catch(null),
  documentId: z.string().nullable().catch(null),
  caseId: z.string().nullable().catch(null),
  errorCode: z.string().max(50).nullable().catch(null),
  embeddingModel: z.string().max(100).nullable().catch(null),
});
