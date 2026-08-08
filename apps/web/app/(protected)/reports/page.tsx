import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { Protected } from "@/components/auth/protected";
import { ReportList } from "@/components/reports/report-list";
import { ReportMetricsPanel } from "@/components/reports/report-metrics-panel";
import { createMetadata } from "@/lib/metadata";
import { PERMISSION } from "@/types/authorization";

export const metadata = createMetadata(
  "Reports",
  "Structured, cited legal reports generated from indexed case documents.",
);

/**
 * Reports — the AI report generation destination.
 *
 * A **Server Component** that renders the client list, per the standard that
 * pages stay lightweight and only the interactive parts are client-side.
 *
 * The route itself is gated on `reports:view` by the route guard, which reads the
 * rule declared once on the navigation item — so the sidebar never offers a
 * destination the guard would block.
 *
 * The metrics panel is gated separately on `reports:monitor`: the history is
 * every report author's, while the platform-wide view is administrative and is
 * not scoped to a case or to a user.
 */
export default function ReportsPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Reports"
        description="Generate structured, cited legal reports from a case's indexed documents. Each report is written in the background, section by section, and only the reports you generated appear here."
      />

      <Protected permission={PERMISSION.reportsMonitor}>
        <ReportMetricsPanel />
      </Protected>

      <ReportList />
    </PageContainer>
  );
}
