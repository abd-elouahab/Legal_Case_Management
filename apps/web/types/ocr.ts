/**
 * OCR types.
 *
 * Mirror the API's OCR payloads (`apps/api/schemas/ocr.py`) and the status set
 * defined in `apps/api/models/ocr.py`. Union types rather than magic strings, per
 * the code standards: referencing a status the platform does not define is a
 * compile error.
 *
 * Unlike the timeline's event registry, the status set is **closed**: it is the
 * whole lifecycle, it is a PostgreSQL enum on the server, and it will not grow by
 * accretion — so a union is the right shape here and a tolerant string would only
 * lose type safety for nothing.
 */

/** Lifecycle states of one extraction run, in the order they occur. */
export const OCR_STATUSES = ["pending", "processing", "completed", "failed"] as const;
export type OcrStatus = (typeof OCR_STATUSES)[number];

/**
 * Where the words for a status live: `ocr.statuses` in the message catalogues.
 *
 * Not here, since `21-localization.md`: a module constant cannot read the
 * reader's language, so a badge built from one stayed English on an Arabic
 * screen. The *values* stay here because a lifecycle is a fact about the
 * platform; the catalogue is what each one says.
 */
export const OCR_STATUS_NAMESPACE = "ocr.statuses";

/**
 * File types text extraction applies to.
 *
 * Mirrors the API's `SUPPORTED_EXTENSIONS`, and is used only to decide whether to
 * show the extraction panel at all — a Word file has machine-readable text
 * already and no run will ever exist for it. **The server is the authority**: it
 * answers 404 for a document with no run and 422 for a retry it does not support,
 * and `GET /ocr/metrics` publishes the list. This copy exists so a client does not
 * have to provoke a 404 to decide what to render.
 */
export const OCR_SUPPORTED_EXTENSIONS = ["pdf", "png", "jpg", "jpeg"] as const;

/** Whether extraction applies to a file of this type. */
export function isOcrSupported(fileExtension: string): boolean {
  return (OCR_SUPPORTED_EXTENSIONS as readonly string[]).includes(
    fileExtension.trim().toLowerCase(),
  );
}

/**
 * Why a run ended without usable text.
 *
 * Open, deliberately: a later engine may report a cause this build has never
 * heard of, and a failed run must still render. {@link ocrFailureLabel} falls
 * back — and the API always sends a human-readable `errorMessage` alongside, so
 * the label is a refinement rather than the only thing a user sees.
 */
export const OCR_FAILURE_CODES = [
  "corrupted_document",
  "unreadable_document",
  "timeout",
  "unsupported_format",
  "engine_failure",
  "storage_failure",
  "unknown",
] as const;
export type KnownOcrFailureCode = (typeof OCR_FAILURE_CODES)[number];

/**
 * Where the words for a failure cause live: `ocr.failures` in the catalogues.
 *
 * `ocrFailureLabel` used to be a function here that mapped a code to an English
 * phrase and humanized anything it did not recognise. Both halves moved: the
 * mapping is now catalogue keys, and the humanizing is
 * `components/i18n/locale-provider.tsx`'s `getMessageFallback`, which already
 * does exactly that for every missing key on the platform. So a cause a later
 * engine invents still renders as readable words, in one place rather than six.
 */
export const OCR_FAILURE_NAMESPACE = "ocr.failures";


/** The document an extraction run belongs to, as shown beside the run. */
export interface OcrDocumentSummary {
  id: string;
  caseId: string;
  originalFilename: string;
  fileExtension: string;
}

/**
 * One extraction run's status and metadata.
 *
 * Deliberately carries **no text**: this is what a client polls while a document
 * is being processed, and the pages are a separate request.
 */
export interface OcrResult {
  id: string;
  documentId: string;
  /** Which version of the document was read. Each version keeps its own run. */
  documentVersion: number;
  document: OcrDocumentSummary | null;

  status: OcrStatus;

  engine: string | null;
  engineVersion: string | null;
  detectedLanguage: string | null;
  pageCount: number | null;
  /** Mean recognition confidence, 0-100, when the engine reports one. */
  confidence: number | null;

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
   * Taken from the server rather than re-derived here, for the same reason as a
   * document's `isPreviewable`: the rule lives in `apps/api/core/ocr.py`, and a
   * second copy in the browser would be the one the user sees when the two
   * disagree.
   */
  isTerminal: boolean;
  isActive: boolean;
  /** Whether a Retry would be accepted right now — false while queued or running. */
  canRetry: boolean;
}

/** One page of extracted text. */
export interface OcrPage {
  pageNumber: number;
  text: string;
  confidence: number | null;
  characterCount: number;
  isEmpty: boolean;
}

/**
 * The text one run extracted.
 *
 * `pages` is the canonical form — page order and page boundaries are what a
 * later indexing feature needs — and `fullText` is the same content joined with
 * {@link pageSeparator}, offered for copying and searching.
 */
export interface OcrText {
  ocrResultId: string;
  documentId: string;
  documentVersion: number;
  status: OcrStatus;
  detectedLanguage: string | null;
  pages: OcrPage[];
  pageCount: number;
  characterCount: number;
  fullText: string;
  /** U+000C FORM FEED. Published by the API so a client need not hardcode it. */
  pageSeparator: string;
}

/** Platform-wide extraction health, for the monitoring view. */
export interface OcrMetrics {
  windowDays: number | null;

  totalRuns: number;
  pending: number;
  processing: number;
  completed: number;
  failed: number;
  finishedRuns: number;

  successRate: number;
  failureRate: number;
  averageDurationMs: number | null;
  averageDurationSeconds: number | null;
  failuresByCode: Record<string, number>;

  engine: string;
  /** False means new work will fail: the engine is not installed or reachable. */
  engineAvailable: boolean;
  enabled: boolean;
  supportedExtensions: string[];
}
