import { Clock, FileCheck2, Loader2, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { REPORT_STATUS_LABELS, type ReportStatus } from "@/types/report";

/**
 * Status badge for one report generation run.
 *
 * Colour comes from the platform's state tokens (`success` / `info` /
 * `destructive`), never from a hardcoded value. Each badge carries its label as
 * text and its icon is `aria-hidden`, so the state is never conveyed by colour or
 * shape alone — the same WCAG rule every other badge on the platform follows.
 *
 * The spinner turns only while the run is actually moving. A queued run gets a
 * clock instead: an animation that never stops is what makes a stalled pipeline
 * look busy — the same reasoning {@link IndexStatusBadge} records.
 */

const STATUS_STYLES: Record<ReportStatus, string> = {
  pending: "border-border bg-muted text-muted-foreground",
  processing: "border-info/30 bg-info/10 text-info",
  completed: "border-success/30 bg-success/10 text-success",
  failed: "border-destructive/30 bg-destructive/10 text-destructive",
};

const STATUS_ICONS: Record<ReportStatus, typeof Clock> = {
  pending: Clock,
  processing: Loader2,
  completed: FileCheck2,
  failed: TriangleAlert,
};

export function ReportStatusBadge({
  status,
  className,
}: {
  status: ReportStatus;
  className?: string;
}) {
  const Icon = STATUS_ICONS[status];

  return (
    <Badge variant="outline" className={cn("gap-1.5", STATUS_STYLES[status], className)}>
      <Icon
        className={cn("h-3.5 w-3.5", status === "processing" && "animate-spin")}
        aria-hidden="true"
      />
      {REPORT_STATUS_LABELS[status]}
    </Badge>
  );
}
