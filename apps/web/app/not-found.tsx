"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { Compass } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ROUTES } from "@/lib/routes";

/**
 * Global 404 Not Found page.
 *
 * Rendered within the root layout (fonts + providers), which is what lets it be
 * translated — unlike `app/global-error.tsx`, which renders its own `<html>`
 * because it catches failures of that layout and therefore cannot assume the
 * locale provider exists.
 */
export default function NotFound() {
  const t = useTranslations("pages.notFound");
  const tShared = useTranslations("shared.accessDenied");

  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-4 px-4 text-center">
      <span className="flex size-16 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <Compass className="h-10 w-10" aria-hidden="true" />
      </span>
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-primary">404</p>
        <h1 className="text-2xl font-semibold text-foreground">{t("title")}</h1>
        <p className="max-w-md text-sm text-muted-foreground">{t("description")}</p>
      </div>
      <Button asChild>
        <Link href={ROUTES.dashboard}>{tShared("backToDashboard")}</Link>
      </Button>
    </div>
  );
}
