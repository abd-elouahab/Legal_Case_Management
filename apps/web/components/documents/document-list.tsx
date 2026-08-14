"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { FileStack, SearchX, Upload } from "lucide-react";
import { toast } from "sonner";

import { Protected } from "@/components/auth/protected";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";
import { DeleteDocumentDialog } from "@/components/documents/delete-document-dialog";
import { DocumentDetailsDialog } from "@/components/documents/document-details-dialog";
import { DocumentFilters } from "@/components/documents/document-filters";
import { DocumentPagination } from "@/components/documents/document-pagination";
import { DocumentPreviewDialog } from "@/components/documents/document-preview-dialog";
import { DocumentTable } from "@/components/documents/document-table";
import { DocumentTableSkeleton } from "@/components/documents/document-table-skeleton";
import { ReplaceDocumentDialog } from "@/components/documents/replace-document-dialog";
import { UploadDocumentDialog } from "@/components/documents/upload-document-dialog";
import { IndexMetricsPanel } from "@/components/indexing/index-metrics-panel";
import { OcrMetricsPanel } from "@/components/ocr/ocr-metrics-panel";
import { SearchMetricsPanel } from "@/components/search/search-metrics-panel";
import { useDocumentListQuery } from "@/hooks/use-document-list-query";
import {
  useDocumentErrorMessage,
  useDocuments,
  useDownloadDocument,
} from "@/hooks/use-documents";
import { PERMISSION } from "@/types/authorization";
import type { LegalDocument } from "@/types/document";

/**
 * The document list: search, filters, table, pagination, and the dialogs.
 *
 * A client component because everything here is interactive. It holds only *UI*
 * state — which dialog is open and which document it targets. Query state lives
 * in `useDocumentListQuery`, server state in `useDocuments`, so this component
 * composes them rather than implementing either.
 *
 * What a user sees is scoped by the API: a caller without `cases:view-all` gets
 * only documents on the cases they are assigned to, and the totals count only
 * those. Nothing here has to know that.
 *
 * Passing `caseId` pins the list to one case — the case column disappears, the
 * upload dialog pre-selects it, and "Clear filters" does not widen the view back
 * to the whole platform.
 */

/** Which dialog is open, and on which document. */
type DialogState =
  | { kind: "none" }
  | { kind: "upload" }
  | { kind: "details"; document: LegalDocument }
  | { kind: "preview"; document: LegalDocument }
  | { kind: "replace"; document: LegalDocument }
  | { kind: "delete"; document: LegalDocument };

export function DocumentList({ caseId }: { caseId?: string } = {}) {
  const list = useDocumentListQuery(caseId ? { caseId } : {});
  const { data, isLoading, isFetching, isError, error, refetch } = useDocuments(list.query);
  const download = useDownloadDocument();
  const t = useTranslations("documents");
  const tActions = useTranslations("common.actions");
  const errorMessage = useDocumentErrorMessage();

  const [dialog, setDialog] = React.useState<DialogState>({ kind: "none" });
  const close = React.useCallback(() => setDialog({ kind: "none" }), []);

  const handleDownload = React.useCallback(
    async (document: LegalDocument) => {
      try {
        await download.mutateAsync({ id: document.id, filename: document.originalFilename });
      } catch (cause) {
        toast.error(errorMessage(cause));
      }
    },
    [download, errorMessage],
  );

  const documents = data?.items ?? [];
  // `isFetching` without `isLoading` is a background refresh — a new page or
  // filter — where the previous data is still on screen.
  const isRefreshing = isFetching && !isLoading;

  return (
    <div className="flex flex-col gap-6">
      {/* Pipeline health, for whoever operates it. Shown only on the platform-wide
          list: pinned to one case it would report figures that have nothing to do
          with the case being read. Gated on `ocr:monitor`, which the API requires
          independently — the panel is an operational view, not a case view. */}
      {!caseId ? (
        <div className="grid gap-6 lg:grid-cols-2">
          <Protected permission={PERMISSION.ocrMonitor}>
            <OcrMetricsPanel />
          </Protected>
          {/* The second stage of the same pipeline, beside the first: a failure
              rate on one and a healthy rate on the other is the difference
              between "documents are unreadable" and "documents are readable but
              not searchable". Gated on its own permission. */}
          <Protected permission={PERMISSION.indexingMonitor}>
            <IndexMetricsPanel />
          </Protected>
          {/* The third stage, and the one that says whether the first two are
              paying off: extraction and indexing can both be healthy while every
              search still fails, because retrieval depends on the embedding model
              being loadable *at query time* and on Qdrant answering reads. Gated
              on its own permission. */}
          <Protected permission={PERMISSION.searchMonitor}>
            <SearchMetricsPanel />
          </Protected>
        </div>
      ) : null}

      <div className="flex flex-col gap-4">
        <div className="flex justify-end">
          <Protected permission={PERMISSION.documentsUpload}>
            <Button onClick={() => setDialog({ kind: "upload" })}>
              <Upload className="h-4 w-4" />
              {t("uploadDialog.title")}
            </Button>
          </Protected>
        </div>

        <DocumentFilters list={list} />
      </div>

      {isLoading ? (
        <DocumentTableSkeleton showCase={!caseId} />
      ) : isError ? (
        <ErrorState
          title={t("errors.listTitle")}
          description={errorMessage(error)}
          onRetry={() => void refetch()}
        />
      ) : documents.length === 0 ? (
        // Two genuinely different situations: an empty case needs a way to add
        // the first file, a fruitless search needs a way back to the full list.
        list.isFiltered ? (
          <EmptyState
            icon={SearchX}
            titleKey="documents.empty.filteredTitle"
            descriptionKey="documents.empty.filteredDescription"
            action={
              <Button variant="outline" onClick={list.reset}>
                {tActions("clearFilters")}
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={FileStack}
            titleKey="documents.empty.title"
            descriptionKey="documents.empty.description"
            action={
              <Protected permission={PERMISSION.documentsUpload}>
                <Button onClick={() => setDialog({ kind: "upload" })}>
                  <Upload className="h-4 w-4" />
                  {t("uploadDialog.title")}
                </Button>
              </Protected>
            }
          />
        )
      ) : (
        <>
          <DocumentTable
            documents={documents}
            sortBy={list.query.sortBy}
            sortOrder={list.query.sortOrder}
            onToggleSort={list.toggleSort}
            onView={(document) => setDialog({ kind: "details", document })}
            onPreview={(document) => setDialog({ kind: "preview", document })}
            onDownload={(document) => void handleDownload(document)}
            onReplace={(document) => setDialog({ kind: "replace", document })}
            onDelete={(document) => setDialog({ kind: "delete", document })}
            showCase={!caseId}
            isRefreshing={isRefreshing}
          />

          <DocumentPagination
            page={data?.page ?? 1}
            pageSize={data?.pageSize ?? list.query.pageSize}
            totalPages={data?.totalPages ?? 1}
            totalRecords={data?.totalRecords ?? 0}
            onPageChange={list.setPage}
            disabled={isRefreshing}
          />
        </>
      )}

      <UploadDocumentDialog
        open={dialog.kind === "upload"}
        onOpenChange={(open) => (open ? setDialog({ kind: "upload" }) : close())}
        {...(caseId ? { caseId } : {})}
      />

      <DocumentDetailsDialog
        documentId={dialog.kind === "details" ? dialog.document.id : null}
        open={dialog.kind === "details"}
        onOpenChange={(open) => (open ? undefined : close())}
      />

      <DocumentPreviewDialog
        document={dialog.kind === "preview" ? dialog.document : null}
        open={dialog.kind === "preview"}
        onOpenChange={(open) => (open ? undefined : close())}
      />

      <ReplaceDocumentDialog
        document={dialog.kind === "replace" ? dialog.document : null}
        open={dialog.kind === "replace"}
        onOpenChange={(open) => (open ? undefined : close())}
      />

      <DeleteDocumentDialog
        document={dialog.kind === "delete" ? dialog.document : null}
        open={dialog.kind === "delete"}
        onOpenChange={(open) => (open ? undefined : close())}
      />
    </div>
  );
}
