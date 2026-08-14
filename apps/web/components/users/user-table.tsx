"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { UserAvatar } from "@/components/users/user-avatar";
import { UserRoleBadge, UserStatusBadge } from "@/components/users/user-badges";
import { UserRowActions } from "@/components/users/user-row-actions";
import { useDateFormat } from "@/hooks/use-date-format";
import { userRoute } from "@/lib/routes";
import { cn } from "@/lib/utils";
import type { ManagedUser } from "@/types/user";
import type { SortOrder, UserSortField } from "@/types/user-management";

/**
 * The user directory table.
 *
 * Sortable columns are `<button>`s inside the header cell rather than click
 * handlers on the cell itself, so they are reachable and operable by keyboard,
 * and each carries `aria-sort` so a screen reader announces the current order.
 *
 * On small screens the table scrolls horizontally inside its own container. The
 * less critical columns (created date, last sign-in) hide below `lg`, so a phone
 * shows identity, role, and status without a horizontal scroll at all.
 */

/** A sortable column heading, translating its own label from the field. */
function SortButton({
  field,
  sortBy,
  sortOrder,
  onToggle,
}: {
  field: UserSortField;
  sortBy: UserSortField;
  sortOrder: SortOrder;
  onToggle: (field: UserSortField) => void;
}) {
  const t = useTranslations("users.table");
  const tSort = useTranslations("common.sort");

  const isActive = sortBy === field;
  const Icon = !isActive ? ChevronsUpDown : sortOrder === "asc" ? ArrowUp : ArrowDown;
  const label = t(`columns.${field}`);

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => onToggle(field)}
      className={cn(
        "-ms-2 h-8 gap-1 px-2 font-medium",
        isActive ? "text-foreground" : "text-muted-foreground",
      )}
    >
      {label}
      <Icon className="h-4 w-4" aria-hidden="true" />
      <span className="sr-only">
        {isActive
          ? tSort(sortOrder === "asc" ? "sortedAscending" : "sortedDescending")
          : tSort("sortBy", { column: label })}
      </span>
    </Button>
  );
}

function ariaSort(
  field: UserSortField,
  sortBy: UserSortField,
  sortOrder: SortOrder,
): "ascending" | "descending" | "none" {
  if (sortBy !== field) return "none";
  return sortOrder === "asc" ? "ascending" : "descending";
}

export function UserTable({
  users,
  sortBy,
  sortOrder,
  onToggleSort,
  currentUserId,
  onEdit,
  onResetPassword,
  onDeactivate,
  onActivate,
  isRefreshing = false,
}: {
  users: ManagedUser[];
  sortBy: UserSortField;
  sortOrder: SortOrder;
  onToggleSort: (field: UserSortField) => void;
  /** The signed-in administrator, so their own row can hide self-only actions. */
  currentUserId: string | null;
  onEdit: (user: ManagedUser) => void;
  onResetPassword: (user: ManagedUser) => void;
  onDeactivate: (user: ManagedUser) => void;
  onActivate: (user: ManagedUser) => void;
  /** Dim the table while a new page or filter is loading. */
  isRefreshing?: boolean;
}) {
  const { formatDate, formatDateTime } = useDateFormat();
  const t = useTranslations("users.table");
  const tTime = useTranslations("common.time");
  const tActions = useTranslations("common.actions");

  return (
    <div
      className={cn(
        "w-full overflow-x-auto rounded-lg border border-border transition-opacity",
        isRefreshing && "opacity-60",
      )}
      aria-busy={isRefreshing}
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead aria-sort={ariaSort("name", sortBy, sortOrder)}>
              <SortButton
                field="name"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead aria-sort={ariaSort("email", sortBy, sortOrder)}>
              <SortButton
                field="email"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead>{t("columns.role")}</TableHead>
            <TableHead>{t("columns.status")}</TableHead>
            <TableHead
              className="hidden lg:table-cell"
              aria-sort={ariaSort("last_login", sortBy, sortOrder)}
            >
              <SortButton
                field="last_login"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead
              className="hidden lg:table-cell"
              aria-sort={ariaSort("created_at", sortBy, sortOrder)}
            >
              <SortButton
                field="created_at"
                sortBy={sortBy}
                sortOrder={sortOrder}
                onToggle={onToggleSort}
              />
            </TableHead>
            <TableHead className="w-12 text-end">
              <span className="sr-only">{tActions("openMenu")}</span>
            </TableHead>
          </TableRow>
        </TableHeader>

        <TableBody>
          {users.map((user) => (
            <TableRow key={user.id}>
              <TableCell>
                <div className="flex items-center gap-3">
                  <UserAvatar name={user.fullName} imageUrl={user.profileImage} />
                  <div className="flex flex-col">
                    <Link
                      href={userRoute(user.id)}
                      className="font-medium text-foreground hover:underline"
                    >
                      {user.fullName}
                    </Link>
                    {user.phone ? (
                      <span className="text-xs text-muted-foreground">{user.phone}</span>
                    ) : null}
                  </div>
                </div>
              </TableCell>

              <TableCell className="text-muted-foreground">{user.email}</TableCell>

              <TableCell>
                <UserRoleBadge role={user.role} />
              </TableCell>

              <TableCell>
                <UserStatusBadge status={user.status} />
              </TableCell>

              <TableCell className="hidden text-muted-foreground lg:table-cell">
                {formatDateTime(user.lastLoginAt, tTime("never"))}
              </TableCell>

              <TableCell className="hidden text-muted-foreground lg:table-cell">
                {formatDate(user.createdAt)}
              </TableCell>

              <TableCell className="text-end">
                <UserRowActions
                  user={user}
                  isSelf={user.id === currentUserId}
                  onEdit={onEdit}
                  onResetPassword={onResetPassword}
                  onDeactivate={onDeactivate}
                  onActivate={onActivate}
                />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
