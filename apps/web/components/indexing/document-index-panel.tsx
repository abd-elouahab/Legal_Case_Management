"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { RefreshCw, Search } from "lucide-react";
import { toast } from "sonner";

import { Protected } from "@/components/auth/protected";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { IndexStatusBadge } from "@/components/indexing/index-status-badge";
import {
  isDocumentIndexMissing,
  useDocumentIndex,
  useIndexCompletionSync,
  useIndexingErrorMessage,
  useReindexDocument,
} from "@/hooks/use-indexing";
import { useDateFormat } from "@/hooks/use-date-format";
import { PERMISSION } from "@/types/authorization";
import { useNumberFormat } from "@/hooks/use-number-format";
import type { DocumentIndex } from "@/types/indexing";
import { isOcrSupported } from "@/types/ocr";
import type { LegalDocument } from "@/types/document";

/**
 * Search indexing for one document: its status and what was built.
 *
 * Lives inside the document details dialog beside the extraction panel, because
 * indexing is a *property* of a document and the second half of the same
 * pipeline — a user asking "can I search this?" is already looking at the file.
 *
 * Four behaviours are worth stating:
 *
 * * **It polls while the run is in flight**, because an index is built on a
 *   background worker with nothing on the client causing it. The polling stops
 *   the moment the server says the run is terminal — see `useDocumentIndex`.
 * * **A missing index is not an error.** A document whose text has not been
 *   extracted has none, and the panel says so plainly instead of showing a
 *   failure the user cannot act on.
 * * **It shows no passages.** Reading the index back is Semantic Search's job;
 *   this panel reports that the document was indexed, how much of it, and with
 *   which model.
 * * **The model matters and is shown.** Changing the embedding model requires
 *   re-indexing, so "which model is this document on?" is the question that
 *   decides whether a Rebuild is needed.
 */

function MetaRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-xs text-foreground">{value}</dd>
    </div>
  );
}

function IndexMetadata({ index }: { index: DocumentIndex }) {
  const { formatDateTime } = useDateFormat();
  const { formatNumber } = useNumberFormat();
  const t = useTranslations("indexing.metadata");
  const tLanguages = useTranslations("common.languages");

  return (
    <dl className="flex flex-col gap-1.5">
      {index.chunkCount !== null ? (
        <MetaRow label={t("passages")} value={formatNumber(index.chunkCount)} />
      ) : null}
      {index.pageCount !== null ? (
        <MetaRow label={t("pages")} value={index.pageCount} />
      ) : null}
      {index.characterCount !== null ? (
        <MetaRow label={t("characters")} value={formatNumber(index.characterCount)} />
      ) : null}
      {index.detectedLanguage ? (
        <MetaRow label={t("language")} value={tLanguages(index.detectedLanguage)} />
      ) : null}
      {index.embeddingModel ? (
        <MetaRow label={t("model")} value={index.embeddingModel} />
      ) : null}
      {index.chunkSize !== null ? (
        <MetaRow
          label={t("passageSize")}
          value={
            index.chunkOverlap !== null
              ? t("passageSizeWithOverlap", {
                  size: index.chunkSize,
                  overlap: index.chunkOverlap,
                })
              : t("passageSizeValue", { size: index.chunkSize })
          }
        />
      ) : null}
      {index.startedAt ? (
        <MetaRow label={t("started")} value={formatDateTime(index.startedAt)} />
      ) : null}
      {index.finishedAt ? (
        <MetaRow label={t("finished")} value={formatDateTime(index.finishedAt)} />
      ) : null}
      {index.durationSeconds !== null ? (
        <MetaRow
          label={t("duration")}
          value={t("seconds", { value: index.durationSeconds })}
        />
      ) : null}
      {index.attemptCount > 1 ? (
        <MetaRow label={t("attempts")} value={index.attemptCount} />
      ) : null}
    </dl>
  );
}

function ReindexButton({
  document,
  index,
}: {
  document: LegalDocument;
  index?: DocumentIndex;
}) {
  const reindex = useReindexDocument();
  const t = useTranslations("indexing");
  const errorMessage = useIndexingErrorMessage();

  // Offered only when the server says a rebuild would be accepted. A run already
  // queued or indexing answers 409, and a button that produces an error the user
  // could not have predicted is worse than no button.
  const disabled = reindex.isPending || (index !== undefined && !index.canReindex);

  async function run() {
    try {
      await reindex.mutateAsync({ documentId: document.id, version: document.version });
      toast.success(t("queued"));
    } catch (error) {
      toast.error(errorMessage(error));
    }
  }

  return (
    <Protected permission={PERMISSION.indexingReindex}>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => void run()}
        disabled={disabled}
      >
        <RefreshCw className="h-4 w-4" aria-hidden="true" />
        {index ? t("rebuild") : t("build")}
      </Button>
    </Protected>
  );
}

export function DocumentIndexPanel({ document }: { document: LegalDocument }) {
  const t = useTranslations("indexing");
  const tFailures = useTranslations("indexing.failures");
  const errorMessage = useIndexingErrorMessage();

  // Indexing consumes extracted text, so it applies exactly where extraction
  // does. Taken from the OCR module's own predicate rather than restated, so the
  // two cannot drift — and the server remains the authority either way.
  const applicable = isOcrSupported(document.fileExtension);

  const { data, isLoading, isError, error } = useDocumentIndex(document.id, {
    enabled: applicable,
    version: document.version,
  });

  // A completed run appends events to the case timeline — which the client did
  // not cause, so nothing would otherwise invalidate it.
  useIndexCompletionSync(data);

  const heading = (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="flex items-center gap-2 text-sm font-medium text-foreground">
        <Search className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        {t("title")}
      </h3>
      {data ? <IndexStatusBadge status={data.status} /> : null}
    </div>
  );

  if (!applicable) {
    return (
      <section className="flex flex-col gap-2" aria-label={t("title")}>
        {heading}
        <p className="text-sm text-muted-foreground">{t("notApplicable")}</p>
      </section>
    );
  }

  return (
    <section className="flex flex-col gap-3" aria-label={t("title")}>
      {heading}

      {isLoading ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-4 w-48" />
        </div>
      ) : isDocumentIndexMissing(error) ? (
        <div className="flex flex-col items-start gap-3">
          <p className="text-sm text-muted-foreground">{t("notIndexed")}</p>
          <ReindexButton document={document} />
        </div>
      ) : isError || !data ? (
        <p role="alert" className="text-sm text-destructive">
          {errorMessage(error)}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {data.isActive ? (
            <p className="text-sm text-muted-foreground">
              {data.status === "pending" ? t("waitingQueued") : t("waitingBuilding")}
            </p>
          ) : null}

          {data.status === "failed" ? (
            <div className="flex flex-col gap-1 rounded-md border border-destructive/30 bg-destructive/5 p-3">
              <p className="text-sm font-medium text-destructive">
                {tFailures(data.errorCode ?? "unknown")}
              </p>
              {/* The server's own message: only it knows the specifics, and it is
                  written to be safe to show — it describes what went wrong with
                  the indexing, never what was being indexed. */}
              {data.errorMessage ? (
                <p className="text-sm text-muted-foreground">{data.errorMessage}</p>
              ) : null}
              <p className="text-xs text-muted-foreground">{t("failureNote")}</p>
            </div>
          ) : null}

          {data.isTerminal ? <IndexMetadata index={data} /> : null}

          <div className="flex justify-end">
            <ReindexButton document={document} index={data} />
          </div>
        </div>
      )}
    </section>
  );
}
