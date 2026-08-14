import { NotificationCenter } from "@/components/notifications/notification-center";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Notifications",
  "Case updates, document activity, hearings, and AI results.",
);

/**
 * Notifications page.
 *
 * The feed is fetched client-side rather than on the server because the access
 * token lives in browser memory only (see `lib/api/token-store.ts`) — a server
 * render has no credential to call the API with. Authorization still applies: the
 * `RouteGuard` gates the route, and the API authorizes every request
 * independently, keyed by recipient.
 *
 * The page itself stays lightweight per the code standards: it renders a header
 * and hands everything else to {@link NotificationCenter}.
 */
export default function NotificationsPage() {
  return (
    <PageContainer>
      <PageHeader
        titleKey="notifications.title"
        descriptionKey="notifications.description"
      />
      <NotificationCenter />
    </PageContainer>
  );
}
