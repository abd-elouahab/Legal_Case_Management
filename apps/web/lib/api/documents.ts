/**
 * Document Management API calls.
 *
 * Thin, typed wrappers over the `/documents/*` endpoints. Every JSON response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape —
 * and a backend change that alters the payload fails here, loudly, instead of
 * surfacing as `undefined` in a table cell.
 *
 * The two file-carrying operations go through `lib/api/upload.ts` rather than the
 * JSON client, because a multipart body needs `XMLHttpRequest` to report progress
 * and a download needs a `Blob` rather than a parsed object.
 */

import { apiRequest } from "@/lib/api/client";
import { DOCUMENT_ENDPOINTS } from "@/lib/api/config";
import { apiFetchFile, apiUpload, type FetchedFile } from "@/lib/api/upload";
import {
  documentPageSchema,
  documentVersionListSchema,
  legalDocumentSchema,
} from "@/lib/validation/document";
import type {
  DocumentCaseSummary,
  DocumentUserSummary,
  DocumentVersion,
  LegalDocument,
} from "@/types/document";
import type {
  DocumentListQuery,
  DocumentPage,
  ReplaceDocumentPayload,
  UpdateDocumentPayload,
  UploadDocumentPayload,
} from "@/types/document-management";

type LegalDocumentWire = ReturnType<typeof legalDocumentSchema.parse>;
type DocumentVersionWire = LegalDocumentWire["versions"][number];
type DocumentUserWire = NonNullable<LegalDocumentWire["uploader"]>;
type DocumentCaseWire = NonNullable<LegalDocumentWire["case"]>;

function toDocumentUser(payload: DocumentUserWire | null): DocumentUserSummary | null {
  if (!payload) return null;
  return {
    id: payload.id,
    fullName: payload.full_name,
    email: payload.email,
    role: payload.role,
  };
}

function toDocumentCase(payload: DocumentCaseWire | null): DocumentCaseSummary | null {
  if (!payload) return null;
  return { id: payload.id, caseNumber: payload.case_number, title: payload.title };
}

function toDocumentVersion(payload: DocumentVersionWire): DocumentVersion {
  return {
    version: payload.version,
    originalFilename: payload.original_filename,
    fileExtension: payload.file_extension,
    mimeType: payload.mime_type,
    fileSize: payload.file_size,
    fileSizeLabel: payload.file_size_label,
    uploadedBy: payload.uploaded_by,
    uploader: toDocumentUser(payload.uploader),
    createdAt: payload.created_at,
  };
}

/** Map one API document record onto the app's {@link LegalDocument}. */
function toLegalDocument(payload: LegalDocumentWire): LegalDocument {
  return {
    id: payload.id,
    caseId: payload.case_id,
    case: toDocumentCase(payload.case),
    originalFilename: payload.original_filename,
    storedFilename: payload.stored_filename,
    fileExtension: payload.file_extension,
    mimeType: payload.mime_type,
    fileSize: payload.file_size,
    fileSizeLabel: payload.file_size_label,
    storageBucket: payload.storage_bucket,
    storageKey: payload.storage_key,
    category: payload.category,
    description: payload.description,
    version: payload.version,
    versionCount: payload.version_count,
    uploadedBy: payload.uploaded_by,
    uploader: toDocumentUser(payload.uploader),
    uploadedAt: payload.uploaded_at,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
    deletedAt: payload.deleted_at,
    isDeleted: payload.is_deleted,
    isPreviewable: payload.is_previewable,
    versions: payload.versions.map(toDocumentVersion),
  };
}

/**
 * Build the query string for a list request.
 *
 * Empty search terms, blank dates, and "any" filters are omitted rather than
 * sent as blanks, so the request URL reflects what is actually being asked —
 * which also makes the query a stable cache key for TanStack Query.
 */
export function buildDocumentListParams(query: DocumentListQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
    sort_by: query.sortBy,
    sort_order: query.sortOrder,
  });

  const optional: Array<[string, string | null]> = [
    ["search", query.search.trim() || null],
    ["case_id", query.caseId],
    ["category", query.category],
    ["uploaded_by", query.uploadedBy],
    ["file_extension", query.fileExtension],
    ["uploaded_from", query.uploadedFrom || null],
    ["uploaded_to", query.uploadedTo || null],
  ];

  for (const [key, value] of optional) {
    if (value) params.set(key, value);
  }

  return params.toString();
}

/** Fetch one page of the document list. */
export async function fetchDocuments(query: DocumentListQuery): Promise<DocumentPage> {
  const raw = await apiRequest<unknown>(
    `${DOCUMENT_ENDPOINTS.list}?${buildDocumentListParams(query)}`,
  );
  const data = documentPageSchema.parse(raw);

  return {
    items: data.items.map(toLegalDocument),
    totalRecords: data.total_records,
    page: data.page,
    pageSize: data.page_size,
    totalPages: data.total_pages,
  };
}

/** Fetch one document's complete record, including its version history. */
export async function fetchDocument(id: string): Promise<LegalDocument> {
  const raw = await apiRequest<unknown>(DOCUMENT_ENDPOINTS.detail(id));
  return toLegalDocument(legalDocumentSchema.parse(raw));
}

/** Fetch a document's version history, oldest first. */
export async function fetchDocumentVersions(id: string): Promise<DocumentVersion[]> {
  const raw = await apiRequest<unknown>(DOCUMENT_ENDPOINTS.versions(id));
  return documentVersionListSchema.parse(raw).map(toDocumentVersion);
}

/**
 * Upload a file as a new document.
 *
 * `multipart/form-data`, so the bytes travel as bytes: base64 in a JSON body
 * would inflate every upload by a third and buffer the whole file twice.
 */
export async function uploadDocument(payload: UploadDocumentPayload): Promise<LegalDocument> {
  const form = new FormData();
  form.append("case_id", payload.caseId);
  form.append("category", payload.category);
  if (payload.description) form.append("description", payload.description);
  form.append("file", payload.file, payload.file.name);

  const raw = await apiUpload<unknown>(DOCUMENT_ENDPOINTS.upload, form, {
    ...(payload.onProgress ? { onProgress: payload.onProgress } : {}),
  });
  return toLegalDocument(legalDocumentSchema.parse(raw));
}

/**
 * Upload a new version of an existing document.
 *
 * The document's identifier, category, and description are unchanged — they
 * describe the document, not the particular file standing in for it.
 */
export async function replaceDocument(
  id: string,
  payload: ReplaceDocumentPayload,
): Promise<LegalDocument> {
  const form = new FormData();
  form.append("file", payload.file, payload.file.name);

  const raw = await apiUpload<unknown>(DOCUMENT_ENDPOINTS.replace(id), form, {
    ...(payload.onProgress ? { onProgress: payload.onProgress } : {}),
  });
  return toLegalDocument(legalDocumentSchema.parse(raw));
}

/** Wire names for the fields a PATCH may carry, in the app's vocabulary. */
const UPDATE_FIELDS = {
  category: "category",
  description: "description",
} as const satisfies Record<keyof UpdateDocumentPayload, string>;

/**
 * Update a document's metadata.
 *
 * Only the keys present in `payload` are sent, so an omitted field is left
 * untouched by the server while an explicit `null` clears it. The file is never
 * involved: no field of this payload can describe one.
 */
export async function updateDocument(
  id: string,
  payload: UpdateDocumentPayload,
): Promise<LegalDocument> {
  const body: Record<string, unknown> = {};

  for (const [key, wireName] of Object.entries(UPDATE_FIELDS)) {
    const value = payload[key as keyof UpdateDocumentPayload];
    if (value !== undefined) body[wireName] = value;
  }

  const raw = await apiRequest<unknown>(DOCUMENT_ENDPOINTS.detail(id), { method: "PATCH", body });
  return toLegalDocument(legalDocumentSchema.parse(raw));
}

/**
 * Delete a document (soft delete).
 *
 * Returns the updated record rather than nothing, so a caller can reflect the
 * deletion without a follow-up fetch.
 */
export async function deleteDocument(id: string): Promise<LegalDocument> {
  const raw = await apiRequest<unknown>(DOCUMENT_ENDPOINTS.detail(id), { method: "DELETE" });
  return toLegalDocument(legalDocumentSchema.parse(raw));
}

/** Fetch a document's file for saving. Defaults to the current version. */
export async function downloadDocumentFile(
  id: string,
  fallbackName: string,
  version?: number,
): Promise<FetchedFile> {
  return apiFetchFile(DOCUMENT_ENDPOINTS.download(id, version), fallbackName);
}

/**
 * Fetch a document's file for inline display.
 *
 * Answers a 415 {@link ApiError} for a type no browser can render, which is what
 * tells the caller to offer a download instead.
 */
export async function previewDocumentFile(
  id: string,
  fallbackName: string,
  version?: number,
): Promise<FetchedFile> {
  return apiFetchFile(DOCUMENT_ENDPOINTS.preview(id, version), fallbackName);
}
