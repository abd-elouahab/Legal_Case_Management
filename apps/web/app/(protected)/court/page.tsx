import { Gavel } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Court Updates",
  "Hearings and court decisions.",
);

/** Court Updates — PLACEHOLDER. Hearings/decisions arrive with a later feature. */
export default function CourtUpdatesPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Court Updates"
        description="Track hearings, court decisions, and case deadlines."
      />
      <EmptyState
        icon={Gavel}
        title="No court updates yet"
        description="Hearing and court decision tracking will be implemented in an upcoming feature."
      />
    </PageContainer>
  );
}
