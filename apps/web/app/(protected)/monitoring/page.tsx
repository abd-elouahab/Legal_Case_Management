import { MonitoringView } from "@/components/monitoring/monitoring-view";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Monitoring",
  "Platform health, performance, background jobs, errors, and security.",
);

/**
 * Monitoring — the platform's operational state.
 *
 * A Server Component that renders the page frame and nothing else, for the reason
 * every other authenticated page here does: the access token lives in browser
 * memory rather than in a cookie the server can read, so the work belongs to the
 * client component below.
 *
 * **The route carries no `ProtectedRoute` of its own, and that is deliberate.**
 * Its requirement — `monitoring:view` — is declared once, on its navigation item
 * in `config/navigation.ts`, and the shell's route guard reads that same rule. A
 * second declaration here would be a second place for it to drift from the
 * sidebar's, which is exactly what deriving the guard from the navigation config
 * exists to prevent.
 *
 * None of that is a security boundary in any case: every request the view makes
 * is authorized independently by the API, which refuses a caller without the
 * permission whatever this application chose to render.
 *
 * **It is the only page on this platform that is not about the platform's subject
 * matter.** Nothing here is scoped to a case, a document, or a person — an uptime
 * and a queue depth belong to the deployment — which is why it is administrators
 * only and why there is nothing narrower to grant anybody.
 */
export default function MonitoringPage() {
  return (
    <PageContainer>
      <PageHeader
        titleKey="monitoring.title"
        descriptionKey="monitoring.description"
      />
      <MonitoringView />
    </PageContainer>
  );
}
