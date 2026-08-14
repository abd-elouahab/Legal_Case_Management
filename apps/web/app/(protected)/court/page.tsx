import { UpcomingFeature } from "@/components/shared/upcoming-feature";
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
        titleKey="courtUpdates.title"
        descriptionKey="courtUpdates.description"
      />
      <UpcomingFeature page="courtUpdates" />
    </PageContainer>
  );
}
