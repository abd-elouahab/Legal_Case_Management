"use client";

import { useTranslations } from "next-intl";

import { Spinner } from "@/components/shared/spinner";
import { cn } from "@/lib/utils";

/**
 * Centered loading indicator with an optional label.
 *
 * Reusable full-region loading state for suspense/loading boundaries.
 */
export function LoadingState({
  label,
  className,
}: {
  /** Overrides the default label. Falls back to the shared loading wording. */
  label?: string;
  className?: string;
}) {
  const t = useTranslations("shared.loading");

  return (
    <div
      className={cn(
        "flex min-h-64 flex-col items-center justify-center gap-3 text-center",
        className,
      )}
    >
      <Spinner className="h-6 w-6" />
      <p className="text-sm text-muted-foreground">{label ?? t("label")}</p>
    </div>
  );
}
