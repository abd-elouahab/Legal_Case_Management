"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
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
import { useDateFormat } from "@/hooks/use-date-format";
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

/**
 * A sortable column heading.
 *
 * Takes the **field** and translates its own label rather than being handed a
 * string — the same shape `components/cases/case-table.tsx` uses, and for the
 * same reason: a caller passing `label="File name"` is a caller with a hardcoded
 * sentence in it.
 */
function SortButton({
  field,
  sortBy,
  sortOrder,
  onToggle,
}: {
  field: DocumentSortField;
  sortBy: DocumentSortField;
  sortOrder: SortOrder;
  onToggle: (field: DocumentSortField) => void;
}) {
  const t = useTranslations("documents.table");
  const tSort = useTranslations("common.sort");

  const isActive = sortBy === field;
  const Icon = !isActive ? ChevronsUpDown : sortOrder === "asc" ? ArrowUp : ArrowDown;
  const label = t(`columns.${field}`);

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => onToggle(field)}
      className={cn(
        "-ms-2 h-8 gap-1 px-2 font-medium",
        isActive ? "text-foreground" : "text-muted-foreground",
      )}
    >
      {label}
      <Icon className="h-4 w-4" aria-hidden="true" />
      <span className="sr-only">
        {isActive
          ? tSort(sortOrder === "asc" ? "sortedAscending" : "sortedDescending")
          : tSort("sortBy", { column: label })}
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
  const { formatDateTime } = useDateFormat();
  const t = useTranslations("documents.table");
  const tStates = useTranslations("common.states");
  const tActions = useTranslations("common.actions");

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
                field="original_filename"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            {showCase ? (
              <TableHead className="hidden lg:table-cell">{t("columns.case")}</TableHead>
            ) : null}

            <TableHead aria-sort={ariaSort("category", sortBy, sortOrder)}>
              <SortButton
                field="category"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead aria-sort={ariaSort("file_size", sortBy, sortOrder)}>
              <SortButton
                field="file_size"
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
                field="version"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead className="hidden xl:table-cell">{t("columns.uploadedBy")}</TableHead>

            <TableHead
              className="hidden xl:table-cell"
              aria-sort={ariaSort("created_at", sortBy, sortOrder)}
            >
              <SortButton
                field="created_at"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>

            <TableHead className="w-12 text-end">
              <span className="sr-only">{tActions("openMenu")}</span>
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
                    className="truncate text-start font-medium text-foreground hover:underline"
                  >
                    {document.originalFilename}
                  </button>
                </div>
                {document.description ? (
                  <p className="truncate ps-6 text-xs text-muted-foreground">
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
                    <span className="text-muted-foreground">
                      {tStates("notAvailable")}
                    </span>
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
                {t("versionShort", { version: document.version })}
              </TableCell>

              <TableCell className="hidden text-muted-foreground xl:table-cell">
                {document.uploader?.fullName ?? tStates("notAvailable")}
              </TableCell>

              <TableCell className="hidden whitespace-nowrap text-muted-foreground xl:table-cell">
                {formatDateTime(document.createdAt)}
              </TableCell>

              <TableCell className="text-end">
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
