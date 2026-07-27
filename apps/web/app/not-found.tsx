import Link from "next/link";
import { Compass } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ROUTES } from "@/lib/routes";

/**
 * Global 404 Not Found page.
 *
 * Rendered within the root layout (fonts + providers). Standalone centered
 * surface with a route back into the app.
 */
export default function NotFound() {
  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-4 px-4 text-center">
      <span className="flex size-16 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <Compass className="h-10 w-10" aria-hidden="true" />
      </span>
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-primary">404</p>
        <h1 className="text-2xl font-semibold text-foreground">
          Page not found
        </h1>
        <p className="max-w-md text-sm text-muted-foreground">
          The page you’re looking for doesn’t exist or may have been moved.
        </p>
      </div>
      <Button asChild>
        <Link href={ROUTES.dashboard}>Back to Dashboard</Link>
      </Button>
    </div>
  );
}
