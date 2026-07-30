/**
 * Document types.
 *
 * Mirror the API's document payload (`apps/api/schemas/document.py`) and the
 * category set defined in `apps/api/models/document.py`. Union types rather than
 * magic strings, per the code standards: referencing a category the platform does
 * not define is a compile error.
 */

import type { UserRole } from "@/types/user";

/**
 * Document categories, in the order the platform displays them.
 *
 * The order mirrors `CATEGORY_RANK` on the API, which is what "sort by category"
 * uses — it groups the substantive documents first and keeps `other` last, which
 * alphabetical ordering would not.
 */
export const DOCUMENT_CATEGORIES = [
  "contract",
  "evidence",
  "court_decision",
  "pleading",
  "correspondence",
  "invoice",
  "identity_document",
  "other",
] as const;
export type DocumentCategory = (typeof DOCUMENT_CATEGORIES)[number];

/** Human-readable category labels (future: i18n keys). */
export const DOCUMENT_CATEGORY_LABELS: Record<DocumentCategory, string> = {
  contract: "Contract",
  evidence: "Evidence",
  court_decision: "Court Decision",
  pleading: "Pleading",
  correspondence: "Correspondence",
  invoice: "Invoice",
  identity_document: "Identity Document",
  other: "Other",
};

/**
 * File types the platform accepts.
 *
 * Mirrors the API's default `ALLOWED_DOCUMENT_EXTENSIONS`, and is used only to
 * populate the file picker's `accept` attribute and to give immediate feedback.
 * **The server is the authority**: a deployment may narrow the list, and every
 * upload is re-validated there — a file this list accepts but the server refuses
 * surfaces as a field error on the file input.
 */
export const SUPPORTED_DOCUMENT_EXTENSIONS = [
  "pdf",
  "docx",
  "doc",
  "txt",
  "jpg",
  "jpeg",
  "png",
] as const;
export type SupportedDocumentExtension = (typeof SUPPORTED_DOCUMENT_EXTENSIONS)[number];

/** File types a browser renders inline; mirrors `PREVIEWABLE_EXTENSIONS`. */
export const PREVIEWABLE_DOCUMENT_EXTENSIONS = ["pdf", "jpg", "jpeg", "png", "txt"] as const;

/**
 * Upload ceiling in megabytes, mirroring the API's `MAX_DOCUMENT_SIZE_MB`
 * default. Client-side only, for immediate feedback; the server enforces it.
 */
export const MAX_DOCUMENT_SIZE_MB = 25;
export const MAX_DOCUMENT_SIZE_BYTES = MAX_DOCUMENT_SIZE_MB * 1024 * 1024;

/**
 * A person referenced by a document — currently its uploader.
 *
 * Deliberately narrower than {@link ManagedUser}: a lawyer may read a document
 * without holding `users:view`, so the API serves only what a document screen
 * shows. The same shape the case module receives.
 */
export interface DocumentUserSummary {
  id: string;
  fullName: string;
  email: string;
  role: UserRole;
}

/** The case a document is filed under, as shown beside the document. */
export interface DocumentCaseSummary {
  id: string;
  caseNumber: string;
  title: string;
}

/** One entry in a document's version history. */
export interface DocumentVersion {
  version: number;
  originalFilename: string;
  fileExtension: string;
  mimeType: string;
  fileSize: number;
  /** Rendered server-side, so every surface expresses a size identically. */
  fileSizeLabel: string;
  uploadedBy: string | null;
  uploader: DocumentUserSummary | null;
  /** ISO-8601 timestamp. */
  createdAt: string;
}

/**
 * A document as returned by `GET /documents` and `GET /documents/{id}`.
 *
 * Describes the **current** version: the file fields mirror the version whose
 * number is {@link version}, and the whole history is on {@link versions}.
 */
export interface LegalDocument {
  id: string;
  caseId: string;
  case: DocumentCaseSummary | null;

  originalFilename: string;
  /** Opaque generated name; never shown to a user. */
  storedFilename: string;
  fileExtension: string;
  mimeType: string;
  fileSize: number;
  fileSizeLabel: string;

  storageBucket: string;
  storageKey: string;

  category: DocumentCategory;
  description: string | null;

  version: number;
  versionCount: number;
  uploadedBy: string | null;
  uploader: DocumentUserSummary | null;
  /** ISO-8601. When the *current* version was uploaded. */
  uploadedAt: string;

  createdAt: string;
  updatedAt: string;
  deletedAt: string | null;
  isDeleted: boolean;

  /**
   * Whether the preview endpoint will serve this file inline.
   *
   * Taken from the server rather than re-derived here, for the same reason as a
   * case's `allowedTransitions`: the policy lives in `apps/api/core/documents.py`,
   * and a second copy in the browser would be the one the user sees when the two
   * disagree — offering a Preview action the API answers with 415.
   */
  isPreviewable: boolean;

  versions: DocumentVersion[];
}
