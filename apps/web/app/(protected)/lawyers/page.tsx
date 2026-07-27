import { Users } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata("Lawyers", "Assigned lawyers.");

/** Lawyers — PLACEHOLDER. Lawyer management arrives with a later feature. */
export default function LawyersPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Lawyers"
        description="Manage lawyers and their case assignments."
      />
      <EmptyState
        icon={Users}
        title="No lawyers yet"
        description="Lawyer management will be implemented in an upcoming feature."
      />
    </PageContainer>
  );
}
