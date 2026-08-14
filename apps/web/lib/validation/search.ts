/**
 * Zod schemas for Semantic Search.
 *
 * Response validation, plus the one *form* this feature has — the query box.
 * API responses are external input, so they are parsed before entering
 * application state (per the code standards). The rules mirror
 * `apps/api/schemas/search.py`; where they must agree, the API is the authority.
 *
 * The client-side query rules exist to spare a round trip and to put the message
 * next to the field, **not** as a security boundary: the API validates the same
 * request independently, and a caller bypassing this reaches exactly the same
 * refusal.
 */

import { z } from "zod";

import { vm } from "@/lib/validation/messages";

import { DOCUMENT_CATEGORIES } from "@/types/document";

/** Shortest query worth sending, matching `MIN_QUERY_LENGTH` on the server. */
export const MIN_QUERY_LENGTH = 2;

/** Longest query the API accepts, matching `SEARCH_QUERY_MAX_LENGTH`. */
export const MAX_QUERY_LENGTH = 1000;

/**
 * The search box.
 *
 * A query of punctuation is rejected here for the same reason it is on the
 * server: embedding it returns a page of arbitrary passages that look exactly
 * like results.
 */
export const searchFormSchema = z.object({
  query: z
    .string()
    .trim()
    .min(MIN_QUERY_LENGTH, vm("validation.minLength", { min: MIN_QUERY_LENGTH }))
    .max(MAX_QUERY_LENGTH, vm("validation.search.queryTooLong", { max: MAX_QUERY_LENGTH }))
    .refine(
      (value) => /[\p{L}\p{N}]/u.test(value),
      vm("validation.search.queryRequired"),
    ),
});

export type SearchFormValues = z.infer<typeof searchFormSchema>;

const searchResultDocumentSchema = z.object({
  id: z.string(),
  case_id: z.string(),
  original_filename: z.string(),
  file_extension: z.string(),
  category: z.enum(DOCUMENT_CATEGORIES),
});

/**
 * One retrieved passage.
 *
 * `language` is a tolerant string rather than an enum: the label is produced per
 * passage by a heuristic on the server, and a future backend may report a code
 * this build has never heard of. A passage must still render.
 */
export const searchResultSchema = z.object({
  document_id: z.string(),
  document_version: z.number().int().positive(),
  case_id: z.string(),
  page_number: z.number().int().nonnegative(),
  chunk_number: z.number().int().nonnegative(),
  score: z.number(),
  text: z.string(),
  language: z.string().nullable().default(null),
  rank: z.number().int().positive(),
  document: searchResultDocumentSchema.nullable().default(null),
});

export const searchResponseSchema = z.object({
  query: z.string(),
  results: z.array(searchResultSchema),
  result_count: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
  has_more: z.boolean(),
  duration_ms: z.number().int().nonnegative(),
  top_score: z.number().nullable().default(null),
  average_score: z.number().nullable().default(null),
  is_empty: z.boolean(),
});

export const searchMetricsSchema = z.object({
  since: z.string(),

  total_searches: z.number().int().nonnegative(),
  successful_searches: z.number().int().nonnegative(),
  failed_searches: z.number().int().nonnegative(),
  success_rate: z.number(),
  failure_rate: z.number(),

  average_latency_ms: z.number().nullable().default(null),
  average_latency_seconds: z.number().nullable().default(null),
  average_score: z.number().nullable().default(null),
  total_results: z.number().int().nonnegative(),
  average_results: z.number().nullable().default(null),
  // Open by construction: the keys are failure codes, which a future backend may
  // extend.
  failures_by_code: z.record(z.string(), z.number().int().nonnegative()).default({}),

  embedding_model: z.string(),
  embedding_dimensions: z.number().int().positive(),
  embedding_available: z.boolean(),

  vector_collection: z.string(),
  vector_store_available: z.boolean(),

  ranker: z.string(),
  default_limit: z.number().int().positive(),
  max_limit: z.number().int().positive(),
  min_score: z.number(),
  enabled: z.boolean(),
});
