import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { CaseDetails } from "@/components/cases/case-details";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata("Case details", "View and manage a legal case.");

/**
 * Case details page.
 *
 * The record is fetched client-side rather than on the server because the access
 * token lives in browser memory only (see `lib/api/token-store.ts`) — a server
 * render has no credential to call the API with. Authorization still applies: the
 * `RouteGuard` gates `/cases/*` through the `/cases` rule (longest-prefix match),
 * and the API authorizes the request independently, including the per-case
 * assignment check.
 */
export default async function CaseDetailsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  return (
    <PageContainer>
      <PageHeader
        titleKey="caseDetails.title"
        descriptionKey="caseDetails.description"
      />
      <CaseDetails caseId={id} />
    </PageContainer>
  );
}
