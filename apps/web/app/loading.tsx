import { LoadingState } from "@/components/shared/loading-state";

/**
 * Root-level loading UI. Shown for top-level route transitions that fall
 * outside the protected shell (e.g. the auth surface).
 */
export default function RootLoading() {
  return (
    <div className="flex min-h-svh items-center justify-center">
      <LoadingState />
    </div>
  );
}
