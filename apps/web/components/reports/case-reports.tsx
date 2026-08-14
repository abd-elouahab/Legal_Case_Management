"use client";

import { useTranslations } from "next-intl";

import { ReportList } from "@/components/reports/report-list";

/**
 * The reports panel inside a case workspace.
 *
 * The same list as `/reports`, pinned to one matter: the case column disappears,
 * *Generate report* pre-selects the case, and *Clear filters* does not widen the
 * list back to the whole platform. The same rule `CaseDocuments` and `CaseSearch`
 * follow, and the reason is the same — a panel inside a case that quietly showed
 * material from other cases would be a panel nobody could trust.
 *
 * A thin wrapper rather than a second implementation, so the two surfaces cannot
 * drift: a change to how a report is opened, exported, or deleted lands in both.
 */
export function CaseReports({ caseId }: { caseId: string }) {
  const t = useTranslations("reports.caseSection");

  return (
    <ReportList caseId={caseId} title={t("title")} description={t("description")} />
  );
}
