import { PageSkeleton } from "@/components/shared/page-skeleton";

/**
 * Route-level loading UI for protected pages. Rendered inside the app shell
 * while a page segment streams in.
 */
export default function ProtectedLoading() {
  return <PageSkeleton />;
}
