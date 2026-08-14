import { DashboardMetricsPanel } from "@/components/dashboard/dashboard-metrics-panel";
import { DashboardView } from "@/components/dashboard/dashboard-view";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { Protected } from "@/components/auth/protected";
import { createMetadata } from "@/lib/metadata";
import { PERMISSION } from "@/types/authorization";

export const metadata = createMetadata(
  "Dashboard",
  "What needs your attention, what changed, and what to do next.",
);

/**
 * Dashboard — the landing page after signing in.
 *
 * A **Server Component** that renders the client view, per the standard that
 * pages stay lightweight and only the interactive parts are client-side.
 *
 * **The route has no permission requirement, and that is deliberate.** Every
 * authenticated role has a dashboard; what it *contains* is decided widget by
 * widget by the API, against the capability that owns each widget's rows. A
 * permission on this page would be one every role holds, and it would put a
 * second copy of the decision somewhere it could drift from the server's.
 *
 * The metrics panel is gated separately on `dashboard:monitor`: a person's own
 * dashboard is theirs, while the platform-wide view of how the dashboard is
 * performing is administrative — the same split the reports, OCR, and
 * notification pages make.
 */
export default function DashboardPage() {
  return (
    <PageContainer>
      <PageHeader
        titleKey="dashboard.title"
        descriptionKey="dashboard.description"
      />

      <Protected permission={PERMISSION.dashboardMonitor}>
        <DashboardMetricsPanel />
      </Protected>

      <DashboardView />
    </PageContainer>
  );
}
