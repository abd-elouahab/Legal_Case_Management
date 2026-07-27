import { Bell } from "lucide-react";

import { EmptyState } from "@/components/shared/empty-state";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Notifications",
  "Alerts and reminders.",
);

/** Notifications — PLACEHOLDER. Notification center arrives with a later feature. */
export default function NotificationsPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Notifications"
        description="Case updates, court decisions, hearing reminders, and system alerts."
      />
      <EmptyState
        icon={Bell}
        title="No notifications yet"
        description="The notification center will be implemented in an upcoming feature."
      />
    </PageContainer>
  );
}
