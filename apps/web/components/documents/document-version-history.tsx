"use client";

import { Download } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/shared/spinner";
import { formatDateTime } from "@/lib/format";
import type { DocumentVersion, LegalDocument } from "@/types/document";

/**
 * A document's version history.
 *
 * Shows what the spec asks for — version number, upload date, and uploader — and
 * lets any version be downloaded, current or not: a replacement never overwrites
 * its predecessor, so every entry here is still a real file in object storage.
 *
 * Ordered newest first, which is the opposite of the API's ordering, because the
 * version a reader is looking for is almost always the most recent one. The
 * current version is marked with a badge rather than only by position.
 */
export function DocumentVersionHistory({
  document,
  onDownloadVersion,
  downloadingVersion = null,
}: {
  document: LegalDocument;
  onDownloadVersion: (version: DocumentVersion) => void;
  /** The version currently being fetched, so only its button shows a spinner. */
  downloadingVersion?: number | null;
}) {
  const newestFirst = [...document.versions].sort((a, b) => b.version - a.version);

  return (
    <section className="flex flex-col gap-3" aria-labelledby="document-version-history">
      <h3 id="document-version-history" className="text-sm font-medium text-foreground">
        Version history
      </h3>

      {newestFirst.length === 0 ? (
        <p className="text-sm text-muted-foreground">No versions recorded.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">
          {newestFirst.map((version) => (
            <li
              key={version.version}
              className="flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="flex flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-foreground">
                    Version {version.version}
                  </span>
                  {version.version === document.version ? (
                    <Badge variant="outline" className="border-primary/40 bg-primary/10 text-primary">
                      Current
                    </Badge>
                  ) : null}
                  <span className="text-xs text-muted-foreground">{version.fileSizeLabel}</span>
                </div>
                <p className="truncate text-xs text-muted-foreground">
                  {version.originalFilename}
                </p>
                <p className="text-xs text-muted-foreground">
                  {formatDateTime(version.createdAt)}
                  {version.uploader ? ` · ${version.uploader.fullName}` : ""}
                </p>
              </div>

              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => onDownloadVersion(version)}
                disabled={downloadingVersion !== null}
              >
                {downloadingVersion === version.version ? (
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
                <span className="sr-only"> version {version.version}</span>
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
