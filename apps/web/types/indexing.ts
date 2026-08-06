/**
 * Document indexing types.
 *
 * Mirror the API's indexing payloads (`apps/api/schemas/indexing.py`) and the
 * status set defined in `apps/api/models/indexing.py`. Union types rather than
 * magic strings, per the code standards: referencing a status the platform does
 * not define is a compile error.
 *
 * As with OCR, the status set is **closed**: it is the whole lifecycle, it is a
 * PostgreSQL enum on the server, and it will not grow by accretion — so a union
 * is the right shape and a tolerant string would only lose type safety for
 * nothing.
 *
 * **Nothing here describes a chunk, a vector, or a search result.** Indexing
 * reports that a document was made searchable; reading the index back belongs to
 * Semantic Search, which is a separate feature with its own types.
 */

/** Lifecycle states of one indexing run, in the order they occur. */
export const INDEX_STATUSES = ["pending", "indexing", "indexed", "failed"] as const;
export type IndexStatus = (typeof INDEX_STATUSES)[number];

/** Human-readable status labels (future: i18n keys). */
export const INDEX_STATUS_LABELS: Record<IndexStatus, string> = {
  pending: "Queued",
  indexing: "Indexing",
  indexed: "Searchable",
  failed: "Failed",
};

/**
 * Why a run ended without a usable index.
 *
 * Open, deliberately: a later embedding backend or vector store may report a
 * cause this build has never heard of, and a failed run must still render.
 * {@link indexFailureLabel} falls back — and the API always sends a
 * human-readable `errorMessage` alongside, so the label is a refinement rather
 * than the only thing a user sees.
 */
export const INDEX_FAILURE_CODES = [
  "invalid_ocr_output",
  "chunking_failure",
  "embedding_failure",
  "vector_store_unavailable",
  "timeout",
  "unknown",
] as const;
export type KnownIndexFailureCode = (typeof INDEX_FAILURE_CODES)[number];

const INDEX_FAILURE_LABELS: Record<KnownIndexFailureCode, string> = {
  invalid_ocr_output: "No extracted text",
  chunking_failure: "Text could not be divided",
  embedding_failure: "Embedding service failed",
  vector_store_unavailable: "Search index unavailable",
  timeout: "Took too long",
  unknown: "Did not complete",
};

/** A short label for a failure cause, falling back to a readable identifier. */
export function indexFailureLabel(code: string | null): string {
  if (!code) return "Failed";

  const known = INDEX_FAILURE_LABELS[code as KnownIndexFailureCode];
  if (known) return known;

  const words = code.replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Failed";
}

/**
 * Language codes a chunk can be labelled with.
 *
 * ISO 639-1, matching `apps/api/core/indexing.py`. `und` is "undetermined" and is
 * a legitimate answer for a page of figures — not an error.
 */
export const INDEX_LANGUAGE_LABELS: Record<string, string> = {
  ar: "Arabic",
  fr: "French",
  en: "English",
  und: "Undetermined",
};

/** A readable name for a language code, falling back to the code itself. */
export function indexLanguageLabel(code: string | null): string | null {
  if (!code) return null;
  return INDEX_LANGUAGE_LABELS[code] ?? code;
}

/** The document an indexing run belongs to, as shown beside the run. */
export interface IndexDocumentSummary {
  id: string;
  caseId: string;
  originalFilename: string;
  fileExtension: string;
}

/**
 * One indexing run's status and metadata.
 *
 * Carries **no passages and no vectors**: what a client needs is whether the
 * document is searchable, how much of it was indexed, and with which model.
 */
export interface DocumentIndex {
  id: string;
  documentId: string;
  /** Which version of the document was indexed. Each version keeps its own. */
  documentVersion: number;
  caseId: string;
  document: IndexDocumentSummary | null;

  status: IndexStatus;

  chunkCount: number | null;
  pageCount: number | null;
  characterCount: number | null;

  embeddingModel: string | null;
  embeddingDimensions: number | null;
  vectorCollection: string | null;
  chunkSize: number | null;
  chunkOverlap: number | null;
  detectedLanguage: string | null;

  /** ISO-8601, or null before the run starts. */
  startedAt: string | null;
  /** ISO-8601, or null until the run reaches a terminal state. */
  finishedAt: string | null;
  durationMs: number | null;
  durationSeconds: number | null;
  attemptCount: number;

  errorCode: string | null;
  errorMessage: string | null;

  requestedBy: string | null;
  createdAt: string;
  updatedAt: string;

  /**
   * Whether the run has finished, and whether to keep polling.
   *
   * Taken from the server rather than re-derived here, for the same reason as
   * an OCR run's `isActive`: the rule lives in `apps/api/core/indexing.py`, and
   * a second copy in the browser would be the one the user sees when the two
   * disagree.
   */
  isTerminal: boolean;
  isActive: boolean;
  /** Whether a Rebuild would be accepted right now — false while queued or running. */
  canReindex: boolean;
}

/** Platform-wide indexing health, for the monitoring view. */
export interface IndexMetrics {
  windowDays: number | null;

  totalRuns: number;
  pending: number;
  indexing: number;
  indexed: number;
  failed: number;
  finishedRuns: number;

  totalChunks: number;
  averageChunksPerDocument: number | null;

  successRate: number;
  failureRate: number;
  averageDurationMs: number | null;
  averageDurationSeconds: number | null;
  failuresByCode: Record<string, number>;

  embeddingModel: string;
  embeddingDimensions: number;
  /** False means new work will fail: the model is not installed or cannot load. */
  embeddingAvailable: boolean;

  chunker: string;
  chunkSize: number;
  chunkOverlap: number;

  vectorCollection: string;
  /** False means new work will fail: the vector database is unreachable. */
  vectorStoreAvailable: boolean;
  vectorCollectionExists: boolean;
  /** Null when the vector database is unreachable — which is not the same as 0. */
  storedVectors: number | null;

  enabled: boolean;
}
