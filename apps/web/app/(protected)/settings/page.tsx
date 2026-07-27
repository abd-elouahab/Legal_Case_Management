import { Settings } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata("Settings", "Workspace preferences.");

/** Settings — PLACEHOLDER. Preferences arrive with later features. */
export default function SettingsPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Settings"
        description="Manage workspace preferences and account settings."
      />
      <EmptyState
        icon={Settings}
        title="Settings coming soon"
        description="Workspace and account settings will be implemented in an upcoming feature."
      />
    </PageContainer>
  );
}
