"use client";

import Link from "next/link";
import { Eye, KeyRound, MoreHorizontal, Pencil, UserCheck, UserX } from "lucide-react";

import { Protected } from "@/components/auth/protected";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { userRoute } from "@/lib/routes";
import { PERMISSION } from "@/types/authorization";
import type { ManagedUser } from "@/types/user";

/**
 * Per-row actions: View, Edit, Reset Password, Activate / Deactivate.
 *
 * Each destructive or privileged item is wrapped in `<Protected>` and names a
 * *permission*, never a role — so a policy change in `core/roles.py` reaches this
 * menu with no edit here. An administrator viewing their own row does not see
 * Deactivate, because the API refuses it: offering an action that can only fail
 * is worse than not offering it.
 */
export function UserRowActions({
  user,
  isSelf,
  onEdit,
  onResetPassword,
  onDeactivate,
  onActivate,
}: {
  user: ManagedUser;
  /** Whether this row is the signed-in administrator's own account. */
  isSelf: boolean;
  onEdit: (user: ManagedUser) => void;
  onResetPassword: (user: ManagedUser) => void;
  onDeactivate: (user: ManagedUser) => void;
  onActivate: (user: ManagedUser) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          aria-label={`Actions for ${user.fullName}`}
        >
          <MoreHorizontal className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuLabel>Actions</DropdownMenuLabel>

        <DropdownMenuItem asChild>
          <Link href={userRoute(user.id)}>
            <Eye className="h-4 w-4" />
            View details
          </Link>
        </DropdownMenuItem>

        <Protected permission={PERMISSION.usersUpdate}>
          <DropdownMenuItem onSelect={() => onEdit(user)}>
            <Pencil className="h-4 w-4" />
            Edit
          </DropdownMenuItem>

          <DropdownMenuItem onSelect={() => onResetPassword(user)}>
            <KeyRound className="h-4 w-4" />
            Reset password
          </DropdownMenuItem>
        </Protected>

        {isSelf ? null : (
          <>
            <DropdownMenuSeparator />

            {user.isActive ? (
              <Protected permission={PERMISSION.usersDelete}>
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={() => onDeactivate(user)}
                >
                  <UserX className="h-4 w-4" />
                  Deactivate
                </DropdownMenuItem>
              </Protected>
            ) : (
              <Protected permission={PERMISSION.usersUpdate}>
                <DropdownMenuItem onSelect={() => onActivate(user)}>
                  <UserCheck className="h-4 w-4" />
                  Activate
                </DropdownMenuItem>
              </Protected>
            )}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
