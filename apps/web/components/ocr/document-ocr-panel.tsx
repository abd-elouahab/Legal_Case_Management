"use client";

import * as React from "react";
import { ChevronDown, ChevronUp, RefreshCw, ScanText } from "lucide-react";
import { toast } from "sonner";

import { Protected } from "@/components/auth/protected";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { OcrStatusBadge } from "@/components/ocr/ocr-status-badge";
import { OcrTextView } from "@/components/ocr/ocr-text-view";
import {
  isOcrResultMissing,
  ocrErrorMessage,
  useOcrCompletionSync,
  useOcrResult,
  useOcrText,
  useRetryOcr,
} from "@/hooks/use-ocr";
import { formatDateTime } from "@/lib/format";
import { PERMISSION } from "@/types/authorization";
import { isOcrSupported, ocrFailureLabel, type OcrResult } from "@/types/ocr";
import type { LegalDocument } from "@/types/document";

/**
 * Text extraction for one document: its status, its metadata, and its text.
 *
 * Lives inside the document details dialog rather than as a screen of its own,
 * because extraction is a *property* of a document — a user who wants to know
 * what a scan says is already looking at the scan.
 *
 * Three behaviours are worth stating:
 *
 * * **It polls while the run is in flight**, because extraction finishes on a
 *   background worker with nothing on the client causing it. The polling stops
 *   the moment the server says the run is terminal — see `useOcrResult`.
 * * **The text is loaded only when the reader asks for it.** A completed
 *   extraction of a 100-page bundle is a large payload, and a details dialog that
 *   fetched it on open would pay for it every time someone checked a file size.
 * * **A missing run is not an error.** A Word file will never have one, and the
 *   panel says so plainly instead of showing a failure the user cannot act on.
 */

function MetaRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-xs text-foreground">{value}</dd>
    </div>
  );
}

function OcrMetadata({ result }: { result: OcrResult }) {
  return (
    <dl className="flex flex-col gap-1.5">
      {result.pageCount !== null ? (
        <MetaRow label="Pages" value={result.pageCount} />
      ) : null}
      {result.confidence !== null ? (
        <MetaRow label="Confidence" value={`${Math.round(result.confidence)}%`} />
      ) : null}
      {result.detectedLanguage ? (
        <MetaRow label="Language" value={result.detectedLanguage} />
      ) : null}
      {result.engine ? (
        <MetaRow
          label="Engine"
          value={result.engineVersion ? `${result.engine} ${result.engineVersion}` : result.engine}
        />
      ) : null}
      {result.startedAt ? (
        <MetaRow label="Started" value={formatDateTime(result.startedAt)} />
      ) : null}
      {result.finishedAt ? (
        <MetaRow label="Finished" value={formatDateTime(result.finishedAt)} />
      ) : null}
      {result.durationSeconds !== null ? (
        <MetaRow label="Duration" value={`${result.durationSeconds}s`} />
      ) : null}
      {result.attemptCount > 1 ? (
        <MetaRow label="Attempts" value={result.attemptCount} />
      ) : null}
    </dl>
  );
}

function RetryButton({ document, result }: { document: LegalDocument; result?: OcrResult }) {
  const retry = useRetryOcr();

  // Offered only when the server says a retry would be accepted. A run already
  // queued or extracting answers 409, and a button that produces an error the
  // user could not have predicted is worse than no button.
  const disabled = retry.isPending || (result !== undefined && !result.canRetry);

  async function run() {
    try {
      await retry.mutateAsync({ documentId: document.id, version: document.version });
      toast.success("Text extraction was queued.");
    } catch (error) {
      toast.error(ocrErrorMessage(error));
    }
  }

  return (
    <Protected permission={PERMISSION.ocrRetry}>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => void run()}
        disabled={disabled}
      >
        <RefreshCw className="h-4 w-4" aria-hidden="true" />
        {result ? "Retry extraction" : "Extract text"}
      </Button>
    </Protected>
  );
}

function ExtractedText({ document }: { document: LegalDocument }) {
  const [expanded, setExpanded] = React.useState(false);
  const { data, isLoading, isError, error } = useOcrText(document.id, {
    enabled: expanded,
    version: document.version,
  });

  return (
    <div className="flex flex-col gap-3">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="self-start"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronUp className="h-4 w-4" aria-hidden="true" />
        ) : (
          <ChevronDown className="h-4 w-4" aria-hidden="true" />
        )}
        {expanded ? "Hide extracted text" : "View extracted text"}
      </Button>

      {expanded ? (
        isLoading ? (
          <div className="flex flex-col gap-2" aria-busy="true">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : isError || !data ? (
          <p role="alert" className="text-sm text-destructive">
            {ocrErrorMessage(error)}
          </p>
        ) : (
          <OcrTextView text={data} />
        )
      ) : null}
    </div>
  );
}

export function DocumentOcrPanel({ document }: { document: LegalDocument }) {
  const supported = isOcrSupported(document.fileExtension);
  const { data, isLoading, isError, error } = useOcrResult(document.id, {
    enabled: supported,
    version: document.version,
  });

  // A completed run appends events to the case timeline and produces the pages
  // the text endpoint serves — none of which the client caused.
  useOcrCompletionSync(data);

  const heading = (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="flex items-center gap-2 text-sm font-medium text-foreground">
        <ScanText className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        Text extraction
      </h3>
      {data ? <OcrStatusBadge status={data.status} /> : null}
    </div>
  );

  if (!supported) {
    return (
      <section className="flex flex-col gap-2" aria-label="Text extraction">
        {heading}
        <p className="text-sm text-muted-foreground">
          Text extraction applies to PDFs and images. This file type already carries
          machine-readable text.
        </p>
      </section>
    );
  }

  return (
    <section className="flex flex-col gap-3" aria-label="Text extraction">
      {heading}

      {isLoading ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-4 w-48" />
        </div>
      ) : isOcrResultMissing(error) ? (
        <div className="flex flex-col items-start gap-3">
          <p className="text-sm text-muted-foreground">
            This document has not been processed yet.
          </p>
          <RetryButton document={document} />
        </div>
      ) : isError || !data ? (
        <p role="alert" className="text-sm text-destructive">
          {ocrErrorMessage(error)}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {data.isActive ? (
            <p className="text-sm text-muted-foreground">
              {data.status === "pending"
                ? "Queued for extraction. This page updates automatically."
                : "Reading the document. This page updates automatically."}
            </p>
          ) : null}

          {data.status === "failed" ? (
            <div className="flex flex-col gap-1 rounded-md border border-destructive/30 bg-destructive/5 p-3">
              <p className="text-sm font-medium text-destructive">
                {ocrFailureLabel(data.errorCode)}
              </p>
              {/* The server's own message: only it knows the specifics, and it is
                  written to be safe to show — it describes what went wrong with
                  the file, never what was in it. */}
              {data.errorMessage ? (
                <p className="text-sm text-muted-foreground">{data.errorMessage}</p>
              ) : null}
              <p className="text-xs text-muted-foreground">
                The document itself is unaffected and can still be downloaded.
              </p>
            </div>
          ) : null}

          {data.isTerminal ? <OcrMetadata result={data} /> : null}

          {data.status === "completed" ? <ExtractedText document={document} /> : null}

          <div className="flex justify-end">
            <RetryButton document={document} result={data} />
          </div>
        </div>
      )}
    </section>
  );
}
