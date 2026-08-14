/**
 * Zod schemas for Document Management.
 *
 * Two jobs, both required by the code standards ("validate all external input
 * using Zod"):
 *
 * 1. **Form validation** — drives the upload, edit, and replace forms through
 *    React Hook Form, mirroring the server's rules so a user gets immediate
 *    feedback. The server always re-validates; this is UX, not security.
 * 2. **Response validation** — API responses are external input too, so they are
 *    parsed before entering application state.
 *
 * The rules deliberately mirror `apps/api/schemas/document.py` and
 * `apps/api/core/documents.py`. Where they must agree, the API is the authority:
 * a file this schema accepts but the server rejects surfaces as a field error on
 * the file input rather than as a silent inconsistency.
 */

import { z } from "zod";

import { vm } from "@/lib/validation/messages";

import { SORT_ORDERS } from "@/types/case-management";
import { DOCUMENT_SORT_FIELDS } from "@/types/document-management";
import {
  DOCUMENT_CATEGORIES,
  MAX_DOCUMENT_SIZE_BYTES,
  MAX_DOCUMENT_SIZE_MB,
  SUPPORTED_DOCUMENT_EXTENSIONS,
} from "@/types/document";
import { USER_ROLES } from "@/types/user";

/** Matches the limits in `apps/api/core/documents.py`. */
export const MAX_DESCRIPTION_LENGTH = 2_000;
export const MAX_EXTENSION_LENGTH = 20;

/** A calendar date as `<input type="date">` produces it, and as the API expects it. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** The `accept` attribute for a file picker, derived from the supported list. */
export const FILE_ACCEPT_ATTRIBUTE = SUPPORTED_DOCUMENT_EXTENSIONS.map(
  (extension) => `.${extension}`,
).join(",");

/** The lowercase extension of a filename, or `""` when it has none. */
export function fileExtensionOf(filename: string): string {
  const index = filename.lastIndexOf(".");
  if (index <= 0 || index === filename.length - 1) return "";
  return filename.slice(index + 1).toLowerCase();
}

// --------------------------------------------------------------------------- //
// Field schemas
// --------------------------------------------------------------------------- //

/** A description is prose: trimmed, but its paragraphs are preserved. */
const optionalDescriptionField = z
  .string()
  .transform((value) => value.trim())
  .refine(
    (value) => value.length <= MAX_DESCRIPTION_LENGTH,
    vm("validation.maxLengthDescription", { max: MAX_DESCRIPTION_LENGTH }),
  );

/**
 * The chosen file.
 *
 * Validated as a `FileList` because that is what an `<input type="file">`
 * registered with React Hook Form yields. The three checks mirror the server's:
 * a file must be chosen, it must not be empty, it must be an accepted type, and
 * it must be within the size ceiling. The server additionally inspects the
 * file's leading bytes, which the browser cannot do without reading it — a
 * mislabelled file therefore fails on submit, with the server's message shown
 * against this same field.
 */
const fileField = z
  .custom<FileList | null | undefined>()
  .refine((files) => Boolean(files && files.length > 0), vm("validation.document.fileRequired"))
  .refine((files) => {
    const file = files?.[0];
    return !file || file.size > 0;
  }, vm("validation.document.fileEmpty"))
  .refine((files) => {
    const file = files?.[0];
    if (!file) return true;
    return (SUPPORTED_DOCUMENT_EXTENSIONS as readonly string[]).includes(
      fileExtensionOf(file.name),
    );
  }, vm("validation.document.unsupportedType", {
      types: SUPPORTED_DOCUMENT_EXTENSIONS.join(", "),
    }))
  .refine((files) => {
    const file = files?.[0];
    return !file || file.size <= MAX_DOCUMENT_SIZE_BYTES;
  }, vm("validation.document.fileTooLarge", { max: MAX_DOCUMENT_SIZE_MB }));

// --------------------------------------------------------------------------- //
// Form schemas
// --------------------------------------------------------------------------- //

/**
 * Upload form.
 *
 * No `.default()` anywhere, for the same reason as the case forms: a defaulted
 * field makes a schema's input type differ from its output type, which then has
 * to be threaded through React Hook Form as two separate generics. Every field is
 * supplied by `defaultValues`.
 */
export const uploadDocumentFormSchema = z.object({
  caseId: z.string().min(1, vm("validation.document.caseRequired")),
  category: z.enum(DOCUMENT_CATEGORIES, { required_error: vm("validation.document.categoryRequired") }),
  description: optionalDescriptionField,
  file: fileField,
});

export type UploadDocumentFormValues = z.infer<typeof uploadDocumentFormSchema>;

/** Metadata edit form — the only two fields the API lets a PATCH change. */
export const editDocumentFormSchema = z.object({
  category: z.enum(DOCUMENT_CATEGORIES, { required_error: vm("validation.document.categoryRequired") }),
  description: optionalDescriptionField,
});

export type EditDocumentFormValues = z.infer<typeof editDocumentFormSchema>;

/** Replace form — the file alone; category and description carry over. */
export const replaceDocumentFormSchema = z.object({ file: fileField });

export type ReplaceDocumentFormValues = z.infer<typeof replaceDocumentFormSchema>;

// --------------------------------------------------------------------------- //
// Response schemas
// --------------------------------------------------------------------------- //

const documentUserSummarySchema = z.object({
  id: z.string(),
  full_name: z.string(),
  email: z.string(),
  role: z.enum(USER_ROLES),
});

const documentCaseSummarySchema = z.object({
  id: z.string(),
  case_number: z.string(),
  title: z.string(),
});

export const documentVersionSchema = z.object({
  version: z.number().int().positive(),
  original_filename: z.string(),
  file_extension: z.string(),
  mime_type: z.string(),
  file_size: z.number().int().nonnegative(),
  file_size_label: z.string(),
  uploaded_by: z.string().nullable().default(null),
  uploader: documentUserSummarySchema.nullable().default(null),
  created_at: z.string(),
});

/**
 * A document record from `GET /documents` and `GET /documents/{id}`.
 *
 * `category` is a strict enum rather than a tolerant string: unlike a case's
 * `allowed_transitions`, a category is *the* value the row is filed under, and
 * silently dropping an unrecognised one would render the document as though it
 * had none. A backend that adds a category therefore needs a client release —
 * which is the honest trade, since the label has to be written somewhere.
 */
export const legalDocumentSchema = z.object({
  id: z.string(),
  case_id: z.string(),
  case: documentCaseSummarySchema.nullable().default(null),

  original_filename: z.string(),
  stored_filename: z.string(),
  file_extension: z.string(),
  mime_type: z.string(),
  file_size: z.number().int().nonnegative(),
  file_size_label: z.string(),

  storage_bucket: z.string(),
  storage_key: z.string(),

  category: z.enum(DOCUMENT_CATEGORIES),
  description: z.string().nullable().default(null),

  version: z.number().int().positive(),
  version_count: z.number().int().nonnegative(),
  uploaded_by: z.string().nullable().default(null),
  uploader: documentUserSummarySchema.nullable().default(null),
  uploaded_at: z.string(),

  created_at: z.string(),
  updated_at: z.string(),
  deleted_at: z.string().nullable().default(null),
  is_deleted: z.boolean(),
  is_previewable: z.boolean(),

  versions: z.array(documentVersionSchema).default([]),
});

export const documentPageSchema = z.object({
  items: z.array(legalDocumentSchema),
  total_records: z.number().int().nonnegative(),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total_pages: z.number().int().nonnegative(),
});

export const documentVersionListSchema = z.array(documentVersionSchema);

// --------------------------------------------------------------------------- //
// Query validation
// --------------------------------------------------------------------------- //

/**
 * The list query, validated before it becomes a query string.
 *
 * The values come from UI state rather than from a user typing a URL, but they
 * are still the input to a request — and validating here means an out-of-range
 * page from a stale link is corrected rather than sent to the API to be rejected.
 */
export const documentListQuerySchema = z.object({
  page: z.coerce.number().int().min(1).catch(1),
  pageSize: z.coerce.number().int().min(1).max(100).catch(20),
  search: z.string().trim().max(200).catch(""),
  caseId: z.string().nullable().catch(null),
  category: z.enum(DOCUMENT_CATEGORIES).nullable().catch(null),
  uploadedBy: z.string().nullable().catch(null),
  fileExtension: z.string().trim().max(MAX_EXTENSION_LENGTH).nullable().catch(null),
  uploadedFrom: z.string().regex(ISO_DATE).or(z.literal("")).catch(""),
  uploadedTo: z.string().regex(ISO_DATE).or(z.literal("")).catch(""),
  sortBy: z.enum(DOCUMENT_SORT_FIELDS).catch("created_at"),
  sortOrder: z.enum(SORT_ORDERS).catch("desc"),
});
