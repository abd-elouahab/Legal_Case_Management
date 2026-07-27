import { Scale } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata("Cases", "Manage legal cases.");

/** Cases — PLACEHOLDER. Case management arrives with a later feature. */
export default function CasesPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Cases"
        description="Create, organize, and track legal cases through their lifecycle."
      />
      <EmptyState
        icon={Scale}
        title="No cases yet"
        description="Case management will be implemented in an upcoming feature."
      />
    </PageContainer>
  );
}
