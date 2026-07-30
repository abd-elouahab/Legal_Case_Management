"use client";

import * as React from "react";
import { Download } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ErrorState } from "@/components/shared/error-state";
import { LoadingState } from "@/components/shared/loading-state";
import { Spinner } from "@/components/shared/spinner";
import {
  documentErrorMessage,
  useDownloadDocument,
  usePreviewDocument,
} from "@/hooks/use-documents";
import type { LegalDocument } from "@/types/document";

/**
 * Inline preview of a document.
 *
 * The file is fetched as a blob and rendered from an object URL rather than
 * pointing an `<iframe>` at the API: the access token lives in memory and travels
 * as an `Authorization` header, so a browser-initiated navigation to the preview
 * URL would arrive anonymous and be refused.
 *
 * The object URL is revoked when the dialog closes or the document changes — an
 * object URL pins the whole blob in memory until it is, and a scanned bundle can
 * be tens of megabytes.
 *
 * **Preview is never the only way to open a file.** When the API answers 415 for
 * a type no browser renders — or anything else goes wrong — this falls back to
 * offering the download, which is exactly what the spec asks for.
 */
export function DocumentPreviewDialog({
  document,
  open,
  onOpenChange,
}: {
  document: LegalDocument | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const preview = usePreviewDocument();
  const download = useDownloadDocument();
  const [objectUrl, setObjectUrl] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const { mutateAsync: fetchPreview } = preview;
  const documentId = document?.id ?? null;
  const filename = document?.originalFilename ?? "document";

  // Drop the previous document's result as soon as a different one is opened.
  // Adjusted during render rather than inside the effect below: a synchronous
  // setState in an effect body causes a cascading render, and here it would also
  // let the old preview stay on screen for one paint under the new title
  // (https://react.dev/learn/you-might-not-need-an-effect).
  const target = open ? documentId : null;
  const [shownTarget, setShownTarget] = React.useState(target);
  if (target !== shownTarget) {
    setShownTarget(target);
    setError(null);
    setObjectUrl(null);
  }

  // Fetching the file is external-system work keyed on which document is open,
  // and the cleanup has to revoke the URL — exactly what an effect is for.
  React.useEffect(() => {
    if (!open || !documentId) return;

    let revoked = false;
    let url: string | null = null;

    void (async () => {
      try {
        const file = await fetchPreview({ id: documentId, filename });
        if (revoked) return;
        url = URL.createObjectURL(file.blob);
        setObjectUrl(url);
      } catch (cause) {
        if (!revoked) setError(documentErrorMessage(cause));
      }
    })();

    return () => {
      revoked = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [open, documentId, filename, fetchPreview]);

  async function saveInstead() {
    if (!document) return;
    try {
      await download.mutateAsync({ id: document.id, filename: document.originalFilename });
    } catch (cause) {
      setError(documentErrorMessage(cause));
    }
  }

  const isImage = document?.mimeType.startsWith("image/") ?? false;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-hidden sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle className="truncate">{document?.originalFilename ?? "Preview"}</DialogTitle>
          <DialogDescription>
            {document
              ? `Version ${document.version} · ${document.fileSizeLabel}`
              : "Loading the document…"}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-72 overflow-auto rounded-lg border border-border bg-muted/30">
          {error ? (
            <ErrorState
              title="Cannot preview this document"
              description={`${error} You can still download it.`}
            />
          ) : !objectUrl ? (
            <LoadingState label="Loading preview…" />
          ) : isImage ? (
            /* A blob URL is not a static asset: `next/image` can neither optimize
               nor load one, so a plain <img> is the only option here. */
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={objectUrl}
              alt={document?.originalFilename ?? "Document preview"}
              className="mx-auto max-h-[65vh] w-auto"
            />
          ) : (
            // `sandbox` with no allowances: the document is user-supplied, so it
            // runs with no script, no forms, and an opaque origin. The API sends a
            // matching Content-Security-Policy on the response itself.
            <iframe
              src={objectUrl}
              title={`Preview of ${document?.originalFilename ?? "document"}`}
              sandbox=""
              className="h-[65vh] w-full border-0 bg-background"
            />
          )}
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button type="button" onClick={saveInstead} disabled={download.isPending}>
            {download.isPending ? (
              <>
                <Spinner className="h-4 w-4 text-current" />
                Downloading…
              </>
            ) : (
              <>
                <Download className="h-4 w-4" />
                Download
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
