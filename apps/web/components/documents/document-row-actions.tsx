"use client";

import { useTranslations } from "next-intl";
import { Download, Eye, FileUp, MoreHorizontal, ScanEye, Trash2 } from "lucide-react";

import { Protected } from "@/components/auth/protected";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { PERMISSION } from "@/types/authorization";
import type { LegalDocument } from "@/types/document";

/**
 * Per-row actions: View details, Preview, Download, Replace, Delete.
 *
 * Each privileged item is wrapped in `<Protected>` and names a *permission*,
 * never a role — so a policy change in `core/roles.py` reaches this menu with no
 * edit here.
 *
 * Preview appears only for a file type the server says it can render inline
 * (`isPreviewable`, computed by the API from the extension). Offering it for a
 * Word document would be offering an action that can only answer 415.
 */
export function DocumentRowActions({
  document,
  onView,
  onPreview,
  onDownload,
  onReplace,
  onDelete,
}: {
  document: LegalDocument;
  onView: (document: LegalDocument) => void;
  onPreview: (document: LegalDocument) => void;
  onDownload: (document: LegalDocument) => void;
  onReplace: (document: LegalDocument) => void;
  onDelete: (document: LegalDocument) => void;
}) {
  const t = useTranslations("documents.actions");
  const tActions = useTranslations("common.actions");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          aria-label={t("menuFor", { filename: document.originalFilename })}
        >
          <MoreHorizontal className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel>{t("label")}</DropdownMenuLabel>

        <DropdownMenuItem onSelect={() => onView(document)}>
          <Eye className="h-4 w-4" />
          {t("viewDetails")}
        </DropdownMenuItem>

        {document.isPreviewable ? (
          <DropdownMenuItem onSelect={() => onPreview(document)}>
            <ScanEye className="h-4 w-4" />
            {t("preview")}
          </DropdownMenuItem>
        ) : null}

        <DropdownMenuItem onSelect={() => onDownload(document)}>
          <Download className="h-4 w-4" />
          {tActions("download")}
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        <Protected permission={PERMISSION.documentsUpdate}>
          <DropdownMenuItem onSelect={() => onReplace(document)}>
            <FileUp className="h-4 w-4" />
            {t("replace")}
          </DropdownMenuItem>
        </Protected>

        <Protected permission={PERMISSION.documentsDelete}>
          <DropdownMenuItem variant="destructive" onSelect={() => onDelete(document)}>
            <Trash2 className="h-4 w-4" />
            {tActions("delete")}
          </DropdownMenuItem>
        </Protected>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
