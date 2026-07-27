import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Indeterminate loading spinner (Lucide `Loader2`, animated).
 */
export function Spinner({ className }: { className?: string }) {
  return (
    <Loader2
      role="status"
      aria-label="Loading"
      className={cn("h-5 w-5 animate-spin text-muted-foreground", className)}
    />
  );
}
