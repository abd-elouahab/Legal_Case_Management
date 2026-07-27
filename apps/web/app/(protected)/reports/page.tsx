import { FileText } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata("Reports", "Generated legal reports.");

/** Reports — PLACEHOLDER. Report generation arrives with a later feature. */
export default function ReportsPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Reports"
        description="Generate and review AI-assisted legal reports."
      />
      <EmptyState
        icon={FileText}
        title="No reports yet"
        description="Report generation will be implemented in an upcoming feature."
      />
    </PageContainer>
  );
}
