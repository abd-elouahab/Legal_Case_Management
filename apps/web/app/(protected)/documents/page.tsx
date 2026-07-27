import { FileText } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Documents",
  "Legal documents and files.",
);

/** Documents — PLACEHOLDER. Document management arrives with a later feature. */
export default function DocumentsPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Documents"
        description="Upload, index, and manage legal documents securely."
      />
      <EmptyState
        icon={FileText}
        title="No documents yet"
        description="Document upload and processing will be implemented in an upcoming feature."
      />
    </PageContainer>
  );
}
