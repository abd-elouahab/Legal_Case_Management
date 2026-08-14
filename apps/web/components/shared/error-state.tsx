"use client";

import { AlertTriangle } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Error state.
 *
 * Reusable failure display for error boundaries and failed sections. Provides
 * an optional retry action. Rendered inside client error boundaries; keep
 * copy user-friendly and never expose stack traces (per code standards).
 */
export function ErrorState({
  title,
  description,
  onRetry,
  retryLabel,
  className,
}: {
  /** Overrides the default copy. Each falls back to the shared failure wording. */
  title?: string;
  description?: string;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}) {
  const t = useTranslations("shared.error");
  const tActions = useTranslations("common.actions");

  return (
    <div
      role="alert"
      className={cn(
        "flex min-h-64 flex-col items-center justify-center gap-3 rounded-xl border border-border p-8 text-center",
        className,
      )}
    >
      <span className="flex size-16 items-center justify-center rounded-full bg-destructive/10 text-destructive">
        <AlertTriangle className="h-10 w-10" aria-hidden="true" />
      </span>
      <div className="flex flex-col gap-1">
        <h3 className="text-base font-medium text-foreground">
          {title ?? t("title")}
        </h3>
        <p className="max-w-sm text-sm text-muted-foreground">
          {description ?? t("description")}
        </p>
      </div>
      {onRetry ? (
        <Button variant="outline" onClick={onRetry} className="mt-2">
          {retryLabel ?? tActions("retry")}
        </Button>
      ) : null}
    </div>
  );
}
