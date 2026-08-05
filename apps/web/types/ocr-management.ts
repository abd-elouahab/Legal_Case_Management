/**
 * OCR DTOs.
 *
 * The request and result shapes of the OCR endpoints, in the app's camelCase
 * domain vocabulary. Mapping to and from the API's snake_case wire format happens
 * once, in `lib/api/ocr.ts`, so nothing above that layer ever sees a wire shape.
 */

import type { OcrResult, OcrStatus } from "@/types/ocr";

/** Default page size, matching `DEFAULT_PAGE_SIZE` in `apps/api/schemas/ocr.py`. */
export const DEFAULT_PAGE_SIZE = 20;

/**
 * The full query state of the extraction-run list: filters and page.
 *
 * Held as one object so a change to any part can reset the page in a single
 * place — filtering while on page 4 would otherwise show an empty result. `null`
 * means "any" for every filter, which is how the Selects express "All".
 *
 * No search term and no sort controls: a run has no text of its own to search
 * (the *document* does, and that list already offers it), and the default
 * newest-first ordering is the only one an operational list is read in.
 */
export interface OcrListQuery {
  page: number;
  pageSize: number;
  status: OcrStatus | null;
  documentId: string | null;
  caseId: string | null;
  errorCode: string | null;
}

export const DEFAULT_OCR_LIST_QUERY: OcrListQuery = {
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  status: null,
  documentId: null,
  caseId: null,
  errorCode: null,
};

/** One page of extraction runs, with the totals needed to render pagination. */
export interface OcrResultPage {
  items: OcrResult[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}
