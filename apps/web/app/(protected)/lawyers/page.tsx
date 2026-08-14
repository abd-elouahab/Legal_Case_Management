import { UpcomingFeature } from "@/components/shared/upcoming-feature";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata("Lawyers", "Assigned lawyers.");

/** Lawyers — PLACEHOLDER. Lawyer management arrives with a later feature. */
export default function LawyersPage() {
  return (
    <PageContainer>
      <PageHeader
        titleKey="lawyers.title"
        descriptionKey="lawyers.description"
      />
      <UpcomingFeature page="lawyers" />
    </PageContainer>
  );
}
