"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronRight, Home } from "lucide-react";

import { buildBreadcrumbs } from "@/lib/breadcrumbs";
import { ROUTES } from "@/lib/routes";
import { cn } from "@/lib/utils";

/**
 * Breadcrumb trail for the current route.
 *
 * Auto-generates from the pathname via {@link buildBreadcrumbs}. The final
 * segment is the current page (non-interactive, `aria-current="page"`); earlier
 * segments link back up the tree.
 */
export function Breadcrumbs({ className }: { className?: string }) {
  const pathname = usePathname();
  const crumbs = buildBreadcrumbs(pathname);

  return (
    <nav aria-label="Breadcrumb" className={cn("min-w-0", className)}>
      <ol className="flex items-center gap-1.5 text-sm text-muted-foreground">
        <li className="flex items-center">
          <Link
            href={ROUTES.dashboard}
            aria-label="Dashboard home"
            className="flex items-center rounded-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Home className="h-4 w-4" />
          </Link>
        </li>
        {crumbs.map((crumb) => (
          <li key={crumb.href} className="flex min-w-0 items-center gap-1.5">
            <ChevronRight
              className="h-4 w-4 shrink-0 text-muted-foreground/60"
              aria-hidden="true"
            />
            {crumb.isCurrent ? (
              <span
                aria-current="page"
                className="truncate font-medium text-foreground"
              >
                {crumb.label}
              </span>
            ) : (
              <Link
                href={crumb.href}
                className="truncate rounded-sm transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {crumb.label}
              </Link>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}
