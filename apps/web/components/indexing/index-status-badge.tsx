import { Clock, Loader2, Search, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { INDEX_STATUS_LABELS, type IndexStatus } from "@/types/indexing";

/**
 * Status badge for one document-indexing run.
 *
 * Colour comes from the platform's state tokens (`success` / `info` /
 * `destructive`), never from a hardcoded value. Each badge carries its label as
 * text and its icon is `aria-hidden`, so the state is never conveyed by colour or
 * shape alone — the same WCAG rule every other badge on the platform follows.
 *
 * The spinner turns only while the run is actually moving. A queued run gets a
 * clock instead: an animation that never stops is what makes a stalled pipeline
 * look busy.
 *
 * The success state uses a **magnifying glass** rather than a tick, because what
 * a successful index gives the reader is that the document is *searchable* — the
 * label says so too.
 */

const STATUS_STYLES: Record<IndexStatus, string> = {
  pending: "border-border bg-muted text-muted-foreground",
  indexing: "border-info/30 bg-info/10 text-info",
  indexed: "border-success/30 bg-success/10 text-success",
  failed: "border-destructive/30 bg-destructive/10 text-destructive",
};

const STATUS_ICONS: Record<IndexStatus, typeof Clock> = {
  pending: Clock,
  indexing: Loader2,
  indexed: Search,
  failed: TriangleAlert,
};

export function IndexStatusBadge({
  status,
  className,
}: {
  status: IndexStatus;
  className?: string;
}) {
  const Icon = STATUS_ICONS[status];

  return (
    <Badge variant="outline" className={cn("gap-1.5", STATUS_STYLES[status], className)}>
      <Icon
        className={cn("h-3.5 w-3.5", status === "indexing" && "animate-spin")}
        aria-hidden="true"
      />
      {INDEX_STATUS_LABELS[status]}
    </Badge>
  );
}
