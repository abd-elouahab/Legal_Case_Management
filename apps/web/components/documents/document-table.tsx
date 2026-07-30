"use client";

import Link from "next/link";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { DocumentCategoryBadge, DocumentTypeIcon } from "@/components/documents/document-badges";
import { DocumentRowActions } from "@/components/documents/document-row-actions";
import { formatDateTime } from "@/lib/format";
import { caseRoute } from "@/lib/routes";
import { cn } from "@/lib/utils";
import type { LegalDocument } from "@/types/document";
import type { DocumentSortField, SortOrder } from "@/types/document-management";

/**
 * The document list table.
 *
 * Columns are the ones the spec lists: file name, category, size, version,
 * uploader, upload date, and actions — plus the case, which the global list
 * needs and the case-scoped list hides (`showCase={false}`), because repeating
 * one case number down every row is noise.
 *
 * Sortable columns are `<button>`s inside the header cell rather than click
 * handlers on the cell itself, so they are reachable and operable by keyboard,
 * and each carries `aria-sort` so a screen reader announces the current order.
 *
 * On small screens the table scrolls horizontally inside its own container, and
 * the columns hide progressively — the uploader and date below `xl`, the case and
 * version below `lg` — so a phone still shows what identifies a document.
 */

interface SortableColumn {
  field: DocumentSortField;
  label: string;
}

function SortButton({
  column,
  sortBy,
  sortOrder,
  onToggle,
}: {
  column: SortableColumn;
  sortBy: DocumentSortField;
  sortOrder: SortOrder;
  onToggle: (field: DocumentSortField) => void;
}) {
  const isActive = sortBy === column.field;
  const Icon = !isActive ? ChevronsUpDown : sortOrder === "asc" ? ArrowUp : ArrowDown;

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => onToggle(column.field)}
      className={cn(
        "-ml-2 h-8 gap-1 px-2 font-medium",
        isActive ? "text-foreground" : "text-muted-foreground",
      )}
    >
      {column.label}
      <Icon className="h-4 w-4" aria-hidden="true" />
      <span className="sr-only">
        {isActive
          ? `Sorted ${sortOrder === "asc" ? "ascending" : "descending"}. Activate to reverse.`
          : `Sort by ${column.label}`}
      </span>
    </Button>
  );
}

function ariaSort(
  field: DocumentSortField,
  sortBy: DocumentSortField,
  sortOrder: SortOrder,
): "ascending" | "descending" | "none" {
  if (sortBy !== field) return "none";
  return sortOrder === "asc" ? "ascending" : "descending";
}

export function DocumentTable({
  documents,
  sortBy,
  sortOrder,
  onToggleSort,
  onView,
  onPreview,
  onDownload,
  onReplace,
  onDelete,
  showCase = true,
  isRefreshing = false,
}: {
  documents: LegalDocument[];
  sortBy: DocumentSortField;
  sortOrder: SortOrder;
  onToggleSort: (field: DocumentSortField) => void;
  onView: (document: LegalDocument) => void;
  onPreview: (document: LegalDocument) => void;
  onDownload: (document: LegalDocument) => void;
  onReplace: (document: LegalDocument) => void;
  onDelete: (document: LegalDocument) => void;
  /** Hidden when the list is already scoped to one case. */
  showCase?: boolean;
  /** Dim the table while a new page or filter is loading. */
  isRefreshing?: boolean;
}) {
  return (
    <div
      className={cn(
        "w-full overflow-x-auto rounded-lg border border-border transition-opacity",
        isRefreshing && "opacity-60",
      )}
      aria-busy={isRefreshing}
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead aria-sort={ariaSort("original_filename", sortBy, sortOrder)}>
              <SortButton
                column={{ field: "original_filename", label: "File name" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            {showCase ? <TableHead className="hidden lg:table-cell">Case</TableHead> : null}

            <TableHead aria-sort={ariaSort("category", sortBy, sortOrder)}>
              <SortButton
                column={{ field: "category", label: "Category" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead aria-sort={ariaSort("file_size", sortBy, sortOrder)}>
              <SortButton
                column={{ field: "file_size", label: "Size" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead
              className="hidden lg:table-cell"
              aria-sort={ariaSort("version", sortBy, sortOrder)}
            >
              <SortButton
                column={{ field: "version", label: "Version" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead className="hidden xl:table-cell">Uploaded by</TableHead>

            <TableHead
              className="hidden xl:table-cell"
              aria-sort={ariaSort("created_at", sortBy, sortOrder)}
            >
              <SortButton
                column={{ field: "created_at", label: "Upload date" }}
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead className="w-12 text-right">
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>

        <TableBody>
          {documents.map((document) => (
            <TableRow key={document.id}>
              <TableCell className="max-w-72">
                <div className="flex items-center gap-2">
                  <DocumentTypeIcon extension={document.fileExtension} />
                  <button
                    type="button"
                    onClick={() => onView(document)}
                    className="truncate text-left font-medium text-foreground hover:underline"
                  >
                    {document.originalFilename}
                  </button>
                </div>
                {document.description ? (
                  <p className="truncate pl-6 text-xs text-muted-foreground">
                    {document.description}
                  </p>
                ) : null}
              </TableCell>

              {showCase ? (
                <TableCell className="hidden lg:table-cell">
                  {document.case ? (
                    <Link
                      href={caseRoute(document.case.id)}
                      className="font-mono text-xs text-muted-foreground hover:underline"
                    >
                      {document.case.caseNumber}
                    </Link>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
              ) : null}

              <TableCell>
                <DocumentCategoryBadge category={document.category} />
              </TableCell>

              <TableCell className="whitespace-nowrap text-muted-foreground">
                {document.fileSizeLabel}
              </TableCell>

              <TableCell className="hidden text-muted-foreground lg:table-cell">
                v{document.version}
              </TableCell>

              <TableCell className="hidden text-muted-foreground xl:table-cell">
                {document.uploader?.fullName ?? "—"}
              </TableCell>

              <TableCell className="hidden whitespace-nowrap text-muted-foreground xl:table-cell">
                {formatDateTime(document.createdAt)}
              </TableCell>

              <TableCell className="text-right">
                <DocumentRowActions
                  document={document}
                  onView={onView}
                  onPreview={onPreview}
                  onDownload={onDownload}
                  onReplace={onReplace}
                  onDelete={onDelete}
                />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
