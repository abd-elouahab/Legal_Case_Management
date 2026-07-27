import { AccessDenied } from "@/components/shared/access-denied";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Access Denied",
  "You don’t have permission to view this page.",
);

/**
 * Access Denied route — PLACEHOLDER.
 *
 * Renders the shared {@link AccessDenied} state. No authorization is evaluated
 * yet; RBAC enforcement is added with the Authentication feature.
 */
export default function AccessDeniedPage() {
  return <AccessDenied />;
}
