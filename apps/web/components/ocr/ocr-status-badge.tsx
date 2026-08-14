"use client";

import { useTranslations } from "next-intl";
import { CheckCircle2, Clock, Loader2, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { OcrStatus } from "@/types/ocr";

/**
 * Status badge for one text-extraction run.
 *
 * Colour comes from the platform's state tokens (`success` / `warning` / `info`),
 * never from a hardcoded value. Each badge carries its label as text and its icon
 * is `aria-hidden`, so the state is never conveyed by colour or shape alone — the
 * same WCAG rule the case, document, and user badges follow.
 *
 * The spinner turns only while the run is actually moving. A queued run gets a
 * clock instead: an animation that never stops is what makes a stalled pipeline
 * look busy.
 */

const STATUS_STYLES: Record<OcrStatus, string> = {
  pending: "border-border bg-muted text-muted-foreground",
  processing: "border-info/30 bg-info/10 text-info",
  completed: "border-success/30 bg-success/10 text-success",
  failed: "border-destructive/30 bg-destructive/10 text-destructive",
};

const STATUS_ICONS: Record<OcrStatus, typeof Clock> = {
  pending: Clock,
  processing: Loader2,
  completed: CheckCircle2,
  failed: TriangleAlert,
};

export function OcrStatusBadge({
  status,
  className,
}: {
  status: OcrStatus;
  className?: string;
}) {
  const Icon = STATUS_ICONS[status];
  const t = useTranslations("ocr.statuses");

  return (
    <Badge variant="outline" className={cn("gap-1.5", STATUS_STYLES[status], className)}>
      <Icon
        className={cn("h-3.5 w-3.5", status === "processing" && "animate-spin")}
        aria-hidden="true"
      />
      {t(status)}
    </Badge>
  );
}
