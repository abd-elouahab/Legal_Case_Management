import { AssistantMetricsPanel } from "@/components/ai/assistant-metrics-panel";
import { AssistantWorkspace } from "@/components/ai/assistant-workspace";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "AI Assistant",
  "Ask questions about your case documents and get source-grounded answers.",
);

/**
 * AI Assistant.
 *
 * A thin page, per the code standards: the workspace is a client component and
 * everything it needs — which conversations exist, which one is open, whether the
 * caller may ask — is decided there, against the session the API supplied.
 *
 * The page carries no permission check of its own. The route guard gates `/ai` on
 * `ai:chat` from `config/navigation.ts`, so the sidebar and the guard cannot
 * disagree, and the workspace re-checks before rendering a composer. None of that
 * is a security boundary: every request is authorized independently by the API.
 */
export default function AiAssistantPage() {
  return (
    <PageContainer>
      <PageHeader
        titleKey="aiAssistant.title"
        descriptionKey="aiAssistant.description"
      />
      {/* Gates itself on `ai:monitor` and renders nothing otherwise, so the page
          does not have to know who is allowed to see operational figures. */}
      <AssistantMetricsPanel />
      <AssistantWorkspace />
    </PageContainer>
  );
}
