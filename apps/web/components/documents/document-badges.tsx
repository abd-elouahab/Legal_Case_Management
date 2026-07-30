import type { LucideIcon } from "lucide-react";
import { File, FileImage, FileText, FileType } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { DOCUMENT_CATEGORY_LABELS, type DocumentCategory } from "@/types/document";

/**
 * Category badge and file-type icon.
 *
 * Colour comes from the platform's state tokens via the `success` / `warning` /
 * `info` utilities, never from a hardcoded hex value. Each badge also carries its
 * label as text, so the category is never conveyed by colour alone — a WCAG
 * requirement, and the reason these are not bare dots.
 */

const CATEGORY_STYLES: Record<DocumentCategory, string> = {
  // The two that bind the parties, and the two the court produces or receives,
  // read as the substantive material; the rest stay quiet so a table of
  // documents does not become a wall of colour.
  contract: "border-primary/40 bg-primary/10 text-primary",
  evidence: "border-info/30 bg-info/10 text-info",
  court_decision: "border-success/30 bg-success/10 text-success",
  pleading: "border-warning/30 bg-warning/10 text-warning",
  correspondence: "border-border text-foreground",
  invoice: "border-border text-foreground",
  identity_document: "border-border text-foreground",
  other: "border-border bg-muted text-muted-foreground",
};

export function DocumentCategoryBadge({
  category,
  className,
}: {
  category: DocumentCategory;
  className?: string;
}) {
  return (
    <Badge variant="outline" className={cn(CATEGORY_STYLES[category], className)}>
      {DOCUMENT_CATEGORY_LABELS[category]}
    </Badge>
  );
}

/**
 * Icon for a file type.
 *
 * Purely decorative — the extension is always shown as text beside it — so it is
 * `aria-hidden` and never the only way to tell two formats apart.
 */
const TYPE_ICONS: Record<string, LucideIcon> = {
  pdf: FileType,
  doc: FileText,
  docx: FileText,
  txt: FileText,
  jpg: FileImage,
  jpeg: FileImage,
  png: FileImage,
};

export function DocumentTypeIcon({
  extension,
  className,
}: {
  extension: string;
  className?: string;
}) {
  const Icon = TYPE_ICONS[extension.toLowerCase()] ?? File;
  return <Icon className={cn("h-4 w-4 text-muted-foreground", className)} aria-hidden="true" />;
}
