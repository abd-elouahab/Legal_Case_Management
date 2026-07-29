import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { CaseList } from "@/components/cases/case-list";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Cases",
  "Create, assign, track, and archive legal cases.",
);

/**
 * Case Management — the platform's central workspace.
 *
 * A thin Server Component: it renders the shared page chrome and delegates all
 * interactivity to `CaseList`. Authorization is not asserted here — the
 * `RouteGuard` in the protected layout applies the `cases:view` rule declared for
 * `/cases` in `config/navigation.ts`, so every page under `(protected)` is
 * guarded by construction rather than by each page remembering to. Which *cases*
 * the caller sees is decided by the API, per assignment.
 */
export default function CasesPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Cases"
        description="Create, organize, and track legal cases through their lifecycle."
      />
      <CaseList />
    </PageContainer>
  );
}
