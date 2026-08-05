import type { LucideIcon } from "lucide-react";
import { FileText, Flag, FolderOpen, RefreshCw, User } from "lucide-react";

import { cn } from "@/lib/utils";
import type { TimelineCategory } from "@/types/timeline";

/**
 * The icon for one event, chosen by its category.
 *
 * The five families `08-timeline.md` names, with the icons it names: a folder for
 * case events, a file for document events, a user for assignments, a refresh for
 * status changes, and a flag for priority changes.
 *
 * The **category comes from the server**, computed from the event type, so the
 * icon cannot disagree with what happened — and an event type this build has
 * never seen still arrives with a usable category rather than no icon at all.
 *
 * Colour is carried alongside the icon, never instead of the label: every entry
 * shows its title as text, so nothing here conveys meaning by colour alone.
 */

const CATEGORY_ICONS: Record<TimelineCategory, LucideIcon> = {
  case: FolderOpen,
  status: RefreshCw,
  priority: Flag,
  assignment: User,
  document: FileText,
};

const CATEGORY_STYLES: Record<TimelineCategory, string> = {
  // Quiet by default; the two that change what a case *is* — its status and its
  // priority — carry the accent, so scanning a long history surfaces them.
  case: "border-border bg-muted text-muted-foreground",
  status: "border-primary/40 bg-primary/10 text-primary",
  priority: "border-warning/30 bg-warning/10 text-warning",
  assignment: "border-info/30 bg-info/10 text-info",
  document: "border-border bg-muted text-muted-foreground",
};

export function TimelineIcon({
  category,
  className,
}: {
  category: TimelineCategory;
  className?: string;
}) {
  const Icon = CATEGORY_ICONS[category] ?? FolderOpen;

  return (
    <span
      className={cn(
        "flex size-8 shrink-0 items-center justify-center rounded-full border",
        CATEGORY_STYLES[category] ?? CATEGORY_STYLES.case,
        className,
      )}
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
    </span>
  );
}
