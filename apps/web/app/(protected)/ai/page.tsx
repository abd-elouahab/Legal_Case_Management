import { Bot } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "AI Assistant",
  "Legal document Q&A and summaries.",
);

/** AI Assistant — PLACEHOLDER. AI features arrive with a later feature. */
export default function AiAssistantPage() {
  return (
    <PageContainer>
      <PageHeader
        title="AI Assistant"
        description="Ask questions, summarize documents, and generate reports with source-grounded AI."
      />
      <EmptyState
        icon={Bot}
        title="AI Assistant coming soon"
        description="The AI legal assistant will be implemented in an upcoming feature."
      />
    </PageContainer>
  );
}
