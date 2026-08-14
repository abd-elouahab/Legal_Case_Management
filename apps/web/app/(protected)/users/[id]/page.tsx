import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { UserDetails } from "@/components/users/user-details";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata("User details", "View a platform account.");

/**
 * User details page.
 *
 * The record is fetched client-side rather than on the server because the access
 * token lives in browser memory only (see `lib/api/token-store.ts`) — a server
 * render has no credential to call the API with. Authorization still applies: the
 * `RouteGuard` gates `/users/*` through the `/users` rule (longest-prefix match),
 * and the API authorizes the request independently.
 */
export default async function UserDetailsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  return (
    <PageContainer>
      <PageHeader
        titleKey="userDetails.title"
        descriptionKey="userDetails.description"
      />
      <UserDetails userId={id} />
    </PageContainer>
  );
}
