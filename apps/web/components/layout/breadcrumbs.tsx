"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronRight, Home } from "lucide-react";
import { useTranslations } from "next-intl";

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
  const t = useTranslations("navigation.routes");
  const tCommon = useTranslations("common.a11y");

  return (
    <nav aria-label={tCommon("breadcrumb")} className={cn("min-w-0", className)}>
      <ol className="flex items-center gap-1.5 text-sm text-muted-foreground">
        <li className="flex items-center">
          <Link
            href={ROUTES.dashboard}
            aria-label={tCommon("dashboardHome")}
            className="flex items-center rounded-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Home className="h-4 w-4" />
          </Link>
        </li>
        {crumbs.map((crumb) => {
          // A segment the navigation config does not name — a case id, a user id
          // — has no key and renders its humanized form. That is the one place a
          // raw path could have reached a screen, and it is why `buildBreadcrumbs`
          // carries a fallback rather than a key it knows nothing about.
          const label = crumb.labelKey ? t(crumb.labelKey) : crumb.fallbackLabel;

          return (
          <li key={crumb.href} className="flex min-w-0 items-center gap-1.5">
            <ChevronRight
              data-flip-rtl
              className="h-4 w-4 shrink-0 text-muted-foreground/60"
              aria-hidden="true"
            />
            {crumb.isCurrent ? (
              <span
                aria-current="page"
                className="truncate font-medium text-foreground"
              >
                {label}
              </span>
            ) : (
              <Link
                href={crumb.href}
                className="truncate rounded-sm transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {label}
              </Link>
            )}
          </li>
          );
        })}
      </ol>
    </nav>
  );
}
