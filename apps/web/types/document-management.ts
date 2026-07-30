/**
 * Document Management DTOs.
 *
 * The request and result shapes of the `/documents` endpoints, in the app's
 * camelCase domain vocabulary. Mapping to and from the API's snake_case wire
 * format happens once, in `lib/api/documents.ts`, so nothing above that layer
 * ever sees a wire shape.
 */

import type { SortOrder } from "@/types/case-management";
import type { DocumentCategory, LegalDocument } from "@/types/document";

export type { SortOrder };

/** Columns the list can be ordered by. Mirrors the API's `DocumentSortField`. */
export const DOCUMENT_SORT_FIELDS = [
  "created_at",
  "original_filename",
  "file_size",
  "category",
  "version",
] as const;
export type DocumentSortField = (typeof DOCUMENT_SORT_FIELDS)[number];

/** Default page size, matching `DEFAULT_PAGE_SIZE` in `apps/api/schemas/document.py`. */
export const DEFAULT_PAGE_SIZE = 20;

/**
 * The full query state of the document list: search, filters, sort, and page.
 *
 * Held as one object so a change to any part can reset the page in a single
 * place — searching while on page 4 would otherwise show an empty result.
 * `null` means "any" for every filter, which is how the Selects express "All".
 */
export interface DocumentListQuery {
  page: number;
  pageSize: number;
  search: string;
  /** Locked to one case when the list is embedded in a case workspace. */
  caseId: string | null;
  category: DocumentCategory | null;
  uploadedBy: string | null;
  fileExtension: string | null;
  uploadedFrom: string;
  uploadedTo: string;
  sortBy: DocumentSortField;
  sortOrder: SortOrder;
}

export const DEFAULT_DOCUMENT_LIST_QUERY: DocumentListQuery = {
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  search: "",
  caseId: null,
  category: null,
  uploadedBy: null,
  fileExtension: null,
  uploadedFrom: "",
  uploadedTo: "",
  sortBy: "created_at",
  sortOrder: "desc",
};

/** One page of the document list, with the totals needed to render pagination. */
export interface DocumentPage {
  items: LegalDocument[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/**
 * Body of `POST /documents/upload`.
 *
 * Sent as `multipart/form-data`, so the file travels alongside the metadata
 * rather than being base64-encoded into a JSON body.
 */
export interface UploadDocumentPayload {
  caseId: string;
  file: File;
  category: DocumentCategory;
  description?: string | null;
  /** Called with 0–100 as the upload progresses. */
  onProgress?: (percent: number) => void;
}

/**
 * Body of `PATCH /documents/{id}`.
 *
 * Metadata only — the API has no field here that could describe a file. An
 * explicit `null` clears the description; omitting the key leaves it alone.
 */
export interface UpdateDocumentPayload {
  category?: DocumentCategory;
  description?: string | null;
}

/** Body of `POST /documents/{id}/replace`. */
export interface ReplaceDocumentPayload {
  file: File;
  onProgress?: (percent: number) => void;
}

/** A file fetched from the API, ready to be saved or displayed. */
export interface DocumentFile {
  blob: Blob;
  filename: string;
  mimeType: string;
}
