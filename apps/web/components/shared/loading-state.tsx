import { Spinner } from "@/components/shared/spinner";
import { cn } from "@/lib/utils";

/**
 * Centered loading indicator with an optional label.
 *
 * Reusable full-region loading state for suspense/loading boundaries.
 */
export function LoadingState({
  label = "Loading…",
  className,
}: {
  label?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex min-h-64 flex-col items-center justify-center gap-3 text-center",
        className,
      )}
    >
      <Spinner className="h-6 w-6" />
      <p className="text-sm text-muted-foreground">{label}</p>
    </div>
  );
}
