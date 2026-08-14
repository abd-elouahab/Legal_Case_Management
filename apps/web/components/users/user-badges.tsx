"use client";

import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { roleMessageKey, type UserRole, type UserStatus } from "@/types/user";

/**
 * Role and status badges.
 *
 * Colour comes from the platform's state tokens (`--state-success` and friends)
 * via the `success` / `warning` / `muted` utilities, never from a hardcoded hex
 * value. Each badge also carries its label as text, so status is never conveyed
 * by colour alone — a WCAG requirement, and the reason these are not bare dots.
 */

const STATUS_STYLES: Record<UserStatus, string> = {
  active: "border-success/30 bg-success/10 text-success",
  inactive: "border-border bg-muted text-muted-foreground",
  suspended: "border-warning/30 bg-warning/10 text-warning",
};

export function UserStatusBadge({
  status,
  className,
}: {
  status: UserStatus;
  className?: string;
}) {
  const t = useTranslations("users.statuses");

  return (
    <Badge variant="outline" className={cn(STATUS_STYLES[status], className)}>
      {t(status)}
    </Badge>
  );
}

/**
 * The user's platform role.
 *
 * Administrators are visually distinct because "who can manage everyone else" is
 * the single most consequential thing in this table.
 */
export function UserRoleBadge({ role, className }: { role: UserRole; className?: string }) {
  const t = useTranslations("users.roles");

  return (
    <Badge
      variant="outline"
      className={cn(
        role === "administrator" && "border-primary/40 bg-primary/10 text-primary",
        className,
      )}
    >
      {t(roleMessageKey(role))}
    </Badge>
  );
}
