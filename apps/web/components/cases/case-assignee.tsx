import { UserAvatar } from "@/components/users/user-avatar";
import { cn } from "@/lib/utils";
import type { CaseUserSummary } from "@/types/case";

/**
 * One of a case's assignees, or an explicit "unassigned".
 *
 * Reuses `UserAvatar` from User Management rather than growing a second avatar
 * component — the two render the same thing, and duplicating it would mean two
 * places to change when profile images finally land.
 *
 * "Unassigned" is stated in words rather than left blank: an empty cell reads as
 * missing data, while the absence of a lawyer is a fact the reader needs.
 */
export function CaseAssignee({
  user,
  emptyLabel = "Unassigned",
  className,
}: {
  user: CaseUserSummary | null;
  emptyLabel?: string;
  className?: string;
}) {
  if (!user) {
    return <span className={cn("text-sm text-muted-foreground", className)}>{emptyLabel}</span>;
  }

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <UserAvatar name={user.fullName} className="size-7" />
      <div className="flex min-w-0 flex-col">
        <span className="truncate text-sm text-foreground">{user.fullName}</span>
        <span className="truncate text-xs text-muted-foreground">{user.email}</span>
      </div>
    </div>
  );
}
