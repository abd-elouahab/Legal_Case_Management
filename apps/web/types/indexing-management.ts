/**
 * Document indexing DTOs.
 *
 * The request and result shapes of the indexing endpoints, in the app's camelCase
 * domain vocabulary. Mapping to and from the API's snake_case wire format happens
 * once, in `lib/api/indexing.ts`, so nothing above that layer ever sees a wire
 * shape.
 */

import type { DocumentIndex, IndexStatus } from "@/types/indexing";

/** Default page size, matching `DEFAULT_PAGE_SIZE` in `apps/api/schemas/indexing.py`. */
export const DEFAULT_PAGE_SIZE = 20;

/**
 * The full query state of the indexing-run list: filters and page.
 *
 * Held as one object so a change to any part can reset the page in a single
 * place — filtering while on page 4 would otherwise show an empty result. `null`
 * means "any" for every filter, which is how the Selects express "All".
 *
 * `embeddingModel` is the filter that is not merely convenience: changing the
 * embedding model requires re-indexing every document, and this is how the ones
 * still built with the previous model are found.
 */
export interface IndexListQuery {
  page: number;
  pageSize: number;
  status: IndexStatus | null;
  documentId: string | null;
  caseId: string | null;
  errorCode: string | null;
  embeddingModel: string | null;
}

export const DEFAULT_INDEX_LIST_QUERY: IndexListQuery = {
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  status: null,
  documentId: null,
  caseId: null,
  errorCode: null,
  embeddingModel: null,
};

/** One page of indexing runs, with the totals needed to render pagination. */
export interface DocumentIndexPage {
  items: DocumentIndex[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}
