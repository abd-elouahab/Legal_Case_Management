import { LayoutDashboard } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Dashboard",
  "Key case metrics at a glance.",
);

/** Dashboard — PLACEHOLDER. Widgets and charts arrive with later features. */
export default function DashboardPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Dashboard"
        description="Overview of cases, hearings, and activity across the platform."
      />
      <EmptyState
        icon={LayoutDashboard}
        title="Dashboard coming soon"
        description="Case metrics, upcoming hearings, and activity widgets will appear here."
      />
    </PageContainer>
  );
}
