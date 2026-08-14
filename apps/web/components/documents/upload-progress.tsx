"use client";

import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

/**
 * Upload progress bar.
 *
 * Hand-built from two `div`s and design tokens rather than pulled in as another
 * Radix primitive: it is a static bar with no interaction, and `components/ui/*`
 * is treated as generated code that this feature should not add to for a
 * presentational detail.
 *
 * `role="progressbar"` with the three `aria-value*` attributes is what makes the
 * percentage available to a screen reader, which the visual fill alone is not.
 * When the total size is unknown the bar renders indeterminate — `aria-valuenow`
 * is omitted, which is precisely how that state is expressed.
 */
export function UploadProgress({
  percent,
  label,
  className,
}: {
  /** 0–100, or `null` while the total size is unknown. */
  percent: number | null;
  /** Already translated by the caller; falls back to the shared wording. */
  label?: string;
  className?: string;
}) {
  const t = useTranslations("documents.upload");
  const isIndeterminate = percent === null;
  const clamped = isIndeterminate ? 0 : Math.min(100, Math.max(0, percent));

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <div
        role="progressbar"
        aria-label={label ?? t("progressLabel")}
        aria-valuemin={0}
        aria-valuemax={100}
        {...(isIndeterminate ? {} : { "aria-valuenow": clamped })}
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className={cn(
            "h-full rounded-full bg-primary transition-[width] duration-200",
            isIndeterminate && "w-1/3 animate-pulse",
          )}
          style={isIndeterminate ? undefined : { width: `${clamped}%` }}
        />
      </div>

      <p className="text-xs text-muted-foreground">
        {isIndeterminate ? t("uploading") : t("uploadingPercent", { percent: clamped })}
      </p>
    </div>
  );
}
