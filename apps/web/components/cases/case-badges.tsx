import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  CASE_PRIORITY_LABELS,
  CASE_STATUS_LABELS,
  type CasePriority,
  type CaseStatus,
} from "@/types/case";

/**
 * Status and priority badges.
 *
 * Colour comes from the platform's state tokens (`--state-success` and friends)
 * via the `success` / `warning` / `error` / `info` utilities, never from a
 * hardcoded hex value. Each badge also carries its label as text, so state is
 * never conveyed by colour alone — a WCAG requirement, and the reason these are
 * not bare dots.
 */

const STATUS_STYLES: Record<CaseStatus, string> = {
  // A draft is not yet part of the workload, so it reads as inactive.
  draft: "border-border bg-muted text-muted-foreground",
  open: "border-info/30 bg-info/10 text-info",
  in_progress: "border-primary/40 bg-primary/10 text-primary",
  // Waiting on the court is the state most likely to need chasing.
  waiting_for_hearing: "border-warning/30 bg-warning/10 text-warning",
  closed: "border-success/30 bg-success/10 text-success",
  archived: "border-border bg-muted text-muted-foreground",
};

export function CaseStatusBadge({
  status,
  className,
}: {
  status: CaseStatus;
  className?: string;
}) {
  return (
    <Badge variant="outline" className={cn(STATUS_STYLES[status], className)}>
      {CASE_STATUS_LABELS[status]}
    </Badge>
  );
}

/**
 * The case's priority.
 *
 * Deliberately escalating in weight rather than four equally loud colours: a
 * table where everything is coloured tells the reader nothing about what to look
 * at first.
 */
const PRIORITY_STYLES: Record<CasePriority, string> = {
  low: "border-border bg-muted text-muted-foreground",
  medium: "border-border text-foreground",
  high: "border-warning/30 bg-warning/10 text-warning",
  urgent: "border-destructive/40 bg-destructive/10 text-destructive",
};

export function CasePriorityBadge({
  priority,
  className,
}: {
  priority: CasePriority;
  className?: string;
}) {
  return (
    <Badge variant="outline" className={cn(PRIORITY_STYLES[priority], className)}>
      {CASE_PRIORITY_LABELS[priority]}
    </Badge>
  );
}
