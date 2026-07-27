import { AccessDenied } from "@/components/shared/access-denied";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Access Denied",
  "You don’t have permission to view this page.",
);

/**
 * Unauthorized page.
 *
 * The addressable form of the {@link AccessDenied} state, for links and manual
 * navigation. Most denials never reach this route: `ProtectedRoute` renders the
 * same component *in place of* the blocked page, so the URL the user asked for
 * stays in the address bar and reloading retries the real page rather than
 * re-showing the error.
 *
 * It carries no access rule of its own — every signed-in user must be able to
 * see it, whatever they were denied.
 */
export default function AccessDeniedPage() {
  return <AccessDenied />;
}
