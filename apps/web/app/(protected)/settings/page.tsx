import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { SettingsWorkspace } from "@/components/settings/settings-workspace";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Settings",
  "Your profile, security, notifications, and preferences.",
);

/**
 * Settings — the platform's configuration in one place.
 *
 * A Server Component that renders the page frame and nothing else: every section
 * needs the caller's own data, and the access token lives in browser memory
 * rather than in a cookie the server can read, so the work belongs to the client
 * component below. That is the same split every other authenticated page on this
 * platform uses.
 */
export default function SettingsPage() {
  return (
    <PageContainer>
      <PageHeader
        titleKey="settings.title"
        descriptionKey="settings.description"
      />
      <SettingsWorkspace />
    </PageContainer>
  );
}
