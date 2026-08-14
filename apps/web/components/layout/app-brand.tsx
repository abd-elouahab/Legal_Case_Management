"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { Scale } from "lucide-react";

import { ROUTES } from "@/lib/routes";
import { cn } from "@/lib/utils";

/**
 * Product brand mark used in the sidebar header.
 *
 * `collapsed` hides the wordmark, leaving only the icon for the desktop rail.
 *
 * A Client Component since Localization: the product name is translated like
 * every other string on the platform, and `useTranslations` needs the locale
 * context the shell provides. It renders no state of its own.
 */
export function AppBrand({
  collapsed = false,
  className,
}: {
  collapsed?: boolean;
  className?: string;
}) {
  const t = useTranslations("shell.brand");

  return (
    <Link
      href={ROUTES.dashboard}
      className={cn(
        "flex items-center gap-2.5 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
        className,
      )}
      aria-label={t("homeLabel")}
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <Scale className="h-5 w-5" />
      </span>
      {!collapsed ? (
        <span className="flex min-w-0 flex-col leading-tight">
          <span className="truncate text-sm font-semibold text-sidebar-foreground">
            {t("name")}
          </span>
          <span className="truncate text-xs text-muted-foreground">
            {t("tagline")}
          </span>
        </span>
      ) : null}
    </Link>
  );
}
