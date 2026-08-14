import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { UserDirectory } from "@/components/users/user-directory";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Users",
  "Create, manage, and deactivate platform accounts.",
);

/**
 * User Management — the administrator's account directory.
 *
 * A thin Server Component: it renders the shared page chrome and delegates all
 * interactivity to `UserDirectory`. Authorization is not asserted here — the
 * `RouteGuard` in the protected layout applies the `users:view` rule declared for
 * `/users` in `config/navigation.ts`, so every page under `(protected)` is
 * guarded by construction rather than by each page remembering to.
 */
export default function UsersPage() {
  return (
    <PageContainer>
      <PageHeader
        titleKey="users.title"
        descriptionKey="users.description"
      />
      <UserDirectory />
    </PageContainer>
  );
}
