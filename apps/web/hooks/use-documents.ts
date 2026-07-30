"use client";

import * as React from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  deleteDocument,
  downloadDocumentFile,
  fetchDocument,
  fetchDocuments,
  previewDocumentFile,
  replaceDocument,
  updateDocument,
  uploadDocument,
} from "@/lib/api/documents";
import { ApiError, NetworkError } from "@/lib/api/errors";
import { saveFile, type FetchedFile } from "@/lib/api/upload";
import type { LegalDocument } from "@/types/document";
import type {
  DocumentListQuery,
  DocumentPage,
  ReplaceDocumentPayload,
  UpdateDocumentPayload,
  UploadDocumentPayload,
} from "@/types/document-management";

/**
 * Server state for Document Management.
 *
 * TanStack Query per `architecture.md`: documents are server state, so they are
 * cached and invalidated rather than mirrored into a client store. Every mutation
 * invalidates the list, which is what keeps a table consistent with a change made
 * from a dialog or a row action without either knowing about the other.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the documents API.
 */

/**
 * Query keys.
 *
 * The list key includes the full query object, so changing a filter is a
 * different cache entry rather than a refetch that discards the previous page —
 * which is what makes paging back and forth instant.
 */
export const documentKeys = {
  all: ["documents"] as const,
  lists: () => [...documentKeys.all, "list"] as const,
  list: (query: DocumentListQuery) => [...documentKeys.lists(), query] as const,
  details: () => [...documentKeys.all, "detail"] as const,
  detail: (id: string) => [...documentKeys.details(), id] as const,
};

/**
 * Translate a failure into a message safe to show the user.
 *
 * Branches on the API's machine-readable `code` rather than on message text,
 * which is localizable and may change. `invalid_document_file` carries the
 * server's own message through verbatim, because only the server knows the
 * specifics — which limit was exceeded, which types are accepted here.
 */
export function documentErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "document_not_found":
        return "This document no longer exists. Refresh the list and try again.";
      case "document_version_not_found":
        return "That version of the document is no longer available.";
      case "case_not_found":
        return "That case no longer exists. Refresh the page and try again.";
      case "invalid_document_file":
        return error.details[0]?.message ?? error.message;
      case "preview_unavailable":
        return "This file type cannot be previewed. Download it instead.";
      case "document_storage_unavailable":
        return "Document storage is temporarily unavailable. Please try again.";
      case "validation_error":
        return error.details[0]?.message ?? "Check the details you entered.";
      case "forbidden":
        return "You do not have permission to perform this action.";
      case "invalid_token":
      case "token_expired":
      case "missing_token":
        return "Your session has expired. Sign in again to continue.";
      default:
        return error.message || "Something went wrong. Please try again.";
    }
  }

  return "Something went wrong. Please try again.";
}

/** Whether a failure is the preview endpoint saying "download this instead". */
export function isPreviewUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.code === "preview_unavailable";
}

/**
 * Field-level errors from a 422, keyed by the app's form field names.
 *
 * Lets a form show the server's complaint next to the offending input instead of
 * as an opaque banner. The API reports snake_case wire names, so they are mapped
 * back to the camelCase names the forms use — and `file` maps to itself, which is
 * where every upload rejection lands.
 */
const FIELD_NAMES: Record<string, string> = {
  file: "file",
  case_id: "caseId",
  category: "category",
  description: "description",
};

export function documentFieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError)) return {};

  const errors: Record<string, string> = {};
  for (const detail of error.details) {
    const field = detail.field ? FIELD_NAMES[detail.field] : undefined;
    if (field && !errors[field]) errors[field] = detail.message;
  }
  return errors;
}

// --------------------------------------------------------------------------- //
// Queries
// --------------------------------------------------------------------------- //

/**
 * One page of the document list.
 *
 * `placeholderData` keeps the previous page on screen while the next one loads,
 * so paging or filtering does not blank the table out and shift the layout.
 */
export function useDocuments(
  query: DocumentListQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<DocumentPage, unknown> {
  return useQuery({
    queryKey: documentKeys.list(query),
    queryFn: () => fetchDocuments(query),
    enabled: options.enabled ?? true,
    placeholderData: (previous) => previous,
  });
}

/** One document's complete record, including its version history. */
export function useDocument(
  id: string,
  options: { enabled?: boolean } = {},
): UseQueryResult<LegalDocument, unknown> {
  return useQuery({
    queryKey: documentKeys.detail(id),
    queryFn: () => fetchDocument(id),
    enabled: (options.enabled ?? true) && Boolean(id),
  });
}

// --------------------------------------------------------------------------- //
// Mutations
// --------------------------------------------------------------------------- //

/**
 * Invalidate everything the document list shows.
 *
 * Deliberately broad: a category change alters which filters a document falls
 * under, a replacement alters its size, name, and sort position, and a deletion
 * removes it entirely. Reconciling each of those by hand would be an ongoing
 * source of stale rows.
 */
function useInvalidateDocuments(): () => Promise<void> {
  const queryClient = useQueryClient();

  return React.useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: documentKeys.all });
  }, [queryClient]);
}

/** Seed the detail cache with the record the server just returned, then invalidate. */
function useApplyDocument(): (document: LegalDocument) => Promise<void> {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateDocuments();

  return React.useCallback(
    async (document: LegalDocument) => {
      // So an open details dialog reflects the change before its refetch lands.
      queryClient.setQueryData(documentKeys.detail(document.id), document);
      await invalidate();
    },
    [queryClient, invalidate],
  );
}

export function useUploadDocument(): UseMutationResult<
  LegalDocument,
  unknown,
  UploadDocumentPayload
> {
  const invalidate = useInvalidateDocuments();

  return useMutation({
    mutationFn: (payload: UploadDocumentPayload) => uploadDocument(payload),
    onSuccess: invalidate,
  });
}

export function useReplaceDocument(): UseMutationResult<
  LegalDocument,
  unknown,
  { id: string; payload: ReplaceDocumentPayload }
> {
  const apply = useApplyDocument();

  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: ReplaceDocumentPayload }) =>
      replaceDocument(id, payload),
    onSuccess: apply,
  });
}

export function useUpdateDocument(): UseMutationResult<
  LegalDocument,
  unknown,
  { id: string; payload: UpdateDocumentPayload }
> {
  const apply = useApplyDocument();

  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: UpdateDocumentPayload }) =>
      updateDocument(id, payload),
    onSuccess: apply,
  });
}

export function useDeleteDocument(): UseMutationResult<LegalDocument, unknown, string> {
  const apply = useApplyDocument();

  return useMutation({
    mutationFn: (id: string) => deleteDocument(id),
    onSuccess: apply,
  });
}

/** What a download or preview request identifies. */
export interface DocumentFileRequest {
  id: string;
  filename: string;
  version?: number;
}

/**
 * Download a document's file and hand it to the browser.
 *
 * A mutation rather than a query: it is an action with a side effect, it must not
 * be cached, and it must not re-run on a window focus — all three of which a
 * query would get wrong.
 */
export function useDownloadDocument(): UseMutationResult<
  FetchedFile,
  unknown,
  DocumentFileRequest
> {
  return useMutation({
    mutationFn: ({ id, filename, version }: DocumentFileRequest) =>
      downloadDocumentFile(id, filename, version),
    onSuccess: saveFile,
  });
}

/**
 * Fetch a document's file for inline display.
 *
 * Returns the blob rather than saving it; the caller turns it into an object URL.
 * A 415 surfaces as an {@link ApiError} the caller can recognise with
 * {@link isPreviewUnavailable} and answer by offering a download.
 */
export function usePreviewDocument(): UseMutationResult<
  FetchedFile,
  unknown,
  DocumentFileRequest
> {
  return useMutation({
    mutationFn: ({ id, filename, version }: DocumentFileRequest) =>
      previewDocumentFile(id, filename, version),
  });
}
