"use client";

import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

/**
 * Indeterminate loading spinner (Lucide `Loader2`, animated).
 */
export function Spinner({ className }: { className?: string }) {
  const t = useTranslations("shared.spinner");

  return (
    <Loader2
      role="status"
      aria-label={t("label")}
      className={cn("h-5 w-5 animate-spin text-muted-foreground", className)}
    />
  );
}
