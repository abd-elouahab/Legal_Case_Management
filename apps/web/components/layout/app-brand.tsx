import Link from "next/link";
import { Scale } from "lucide-react";

import { ROUTES } from "@/lib/routes";
import { cn } from "@/lib/utils";

/**
 * Product brand mark used in the sidebar header.
 *
 * `collapsed` hides the wordmark, leaving only the icon for the desktop rail.
 */
export function AppBrand({
  collapsed = false,
  className,
}: {
  collapsed?: boolean;
  className?: string;
}) {
  return (
    <Link
      href={ROUTES.dashboard}
      className={cn(
        "flex items-center gap-2.5 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
        className,
      )}
      aria-label="Legal Case Management Platform — Dashboard"
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <Scale className="h-5 w-5" />
      </span>
      {!collapsed ? (
        <span className="flex min-w-0 flex-col leading-tight">
          <span className="truncate text-sm font-semibold text-sidebar-foreground">
            Legal Platform
          </span>
          <span className="truncate text-xs text-muted-foreground">
            Case Management
          </span>
        </span>
      ) : null}
    </Link>
  );
}
