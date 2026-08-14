"use client";

import { useTranslations } from "next-intl";
import { FileStack } from "lucide-react";

import { DocumentList } from "@/components/documents/document-list";

/**
 * The Documents section of a case workspace.
 *
 * The same list as `/documents`, pinned to one case: `DocumentList` hides the
 * case column, pre-selects the case in the upload dialog, and keeps the scope
 * through "Clear filters". Reusing it rather than building a second table is what
 * keeps the two surfaces from drifting — a filter, a sort, or a row action added
 * to one appears in the other by construction.
 *
 * This replaces the dashed Documents placeholder the case details page carried
 * while the module was unbuilt.
 */
export function CaseDocuments({ caseId }: { caseId: string }) {
  const t = useTranslations("pages.documents");

  return (
    <section className="flex flex-col gap-4" aria-labelledby="case-documents">
      <h3
        id="case-documents"
        className="flex items-center gap-2 text-sm font-medium text-foreground"
      >
        <FileStack className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        {t("title")}
      </h3>

      <DocumentList caseId={caseId} />
    </section>
  );
}
