"use client";

import Link from "next/link";
import { ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { ROUTES } from "@/lib/routes";
import { cn } from "@/lib/utils";

/**
 * Unauthorized state.
 *
 * The screen a signed-in user sees when they lack the permission a page
 * requires — rendered in place of the protected content by `ProtectedRoute`, and
 * served directly at `/access-denied`.
 *
 * Purely presentational: it evaluates nothing itself, so the same screen serves
 * every denial. The copy deliberately does not say *which* permission was
 * missing (the API's 403 responses do not either) and offers a way back to a
 * page the user can reach.
 */
export function AccessDenied({
  title,
  description,
  className,
}: {
  /** Overrides the default copy. Both fall back to the shared refusal wording. */
  title?: string;
  description?: string;
  className?: string;
}) {
  const t = useTranslations("shared.accessDenied");

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
        <h1 className="text-xl font-semibold text-foreground">
          {title ?? t("title")}
        </h1>
        <p className="max-w-md text-sm text-muted-foreground">
          {description ?? t("description")}
        </p>
      </div>
      <Button asChild>
        <Link href={ROUTES.dashboard}>{t("backToDashboard")}</Link>
      </Button>
    </div>
  );
}
