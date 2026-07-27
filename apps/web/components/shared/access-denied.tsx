import Link from "next/link";
import { ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ROUTES } from "@/lib/routes";
import { cn } from "@/lib/utils";

/**
 * Access Denied — PLACEHOLDER.
 *
 * Presentational 403 state for use once Role-Based Access Control lands. No
 * authorization logic is evaluated in this spec; the component only defines the
 * shape/appearance of a denied-access screen.
 */
export function AccessDenied({
  title = "Access denied",
  description = "You don’t have permission to view this page. If you believe this is a mistake, contact an administrator.",
  className,
}: {
  title?: string;
  description?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex min-h-[60vh] flex-col items-center justify-center gap-4 px-4 text-center",
        className,
      )}
    >
      <span className="flex size-16 items-center justify-center rounded-full bg-warning/10 text-warning">
        <ShieldAlert className="h-10 w-10" aria-hidden="true" />
      </span>
      <div className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold text-foreground">{title}</h1>
        <p className="max-w-md text-sm text-muted-foreground">{description}</p>
      </div>
      <Button asChild>
        <Link href={ROUTES.dashboard}>Back to Dashboard</Link>
      </Button>
    </div>
  );
}
