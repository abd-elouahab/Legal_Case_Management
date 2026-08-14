"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { SearchX, UserPlus, Users } from "lucide-react";
import { toast } from "sonner";

import { Protected } from "@/components/auth/protected";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";
import { CreateUserDialog } from "@/components/users/create-user-dialog";
import { DeactivateUserDialog } from "@/components/users/deactivate-user-dialog";
import { EditUserDialog } from "@/components/users/edit-user-dialog";
import { ResetPasswordDialog } from "@/components/users/reset-password-dialog";
import { UserFilters } from "@/components/users/user-filters";
import { UserPagination } from "@/components/users/user-pagination";
import { UserTable } from "@/components/users/user-table";
import { UserTableSkeleton } from "@/components/users/user-table-skeleton";
import { useSession } from "@/hooks/use-session";
import { useUserListQuery } from "@/hooks/use-user-list-query";
import { useActivateUser, useUserErrorMessage, useUsers } from "@/hooks/use-users";
import { PERMISSION } from "@/types/authorization";
import type { ManagedUser } from "@/types/user";

/**
 * The user directory: search, filters, table, pagination, and the dialogs.
 *
 * A client component because everything here is interactive. It holds only *UI*
 * state — which dialog is open and which row it targets. Query state lives in
 * `useUserListQuery`, server state in `useUsers`, so this component composes
 * them rather than implementing either.
 */

/** Which dialog is open, and on whom. */
type DialogState =
  | { kind: "none" }
  | { kind: "create" }
  | { kind: "edit"; user: ManagedUser }
  | { kind: "reset"; user: ManagedUser }
  | { kind: "deactivate"; user: ManagedUser };

export function UserDirectory() {
  const { user: currentUser } = useSession();
  const list = useUserListQuery();
  const { data, isLoading, isFetching, isError, error, refetch } = useUsers(list.query);
  const activateUser = useActivateUser();

  const t = useTranslations("users");
  const tActions = useTranslations("common.actions");
  const errorMessage = useUserErrorMessage();
  const [dialog, setDialog] = React.useState<DialogState>({ kind: "none" });
  const close = React.useCallback(() => setDialog({ kind: "none" }), []);

  const handleActivate = React.useCallback(
    async (user: ManagedUser) => {
      try {
        await activateUser.mutateAsync(user.id);
        toast.success(t("actions.activated", { name: user.fullName }));
      } catch (cause) {
        toast.error(errorMessage(cause));
      }
    },
    [activateUser, errorMessage, t],
  );

  const users = data?.items ?? [];
  // `isFetching` without `isLoading` is a background refresh — a new page or
  // filter — where the previous data is still on screen.
  const isRefreshing = isFetching && !isLoading;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-4">
        <div className="flex justify-end">
          <Protected permission={PERMISSION.usersCreate}>
            <Button onClick={() => setDialog({ kind: "create" })}>
              <UserPlus className="h-4 w-4" />
              {t("createDialog.title")}
            </Button>
          </Protected>
        </div>

        <UserFilters
          search={list.searchInput}
          role={list.query.role}
          status={list.query.status}
          onSearchChange={list.setSearch}
          onRoleChange={list.setRole}
          onStatusChange={list.setStatus}
          onReset={list.reset}
          isFiltered={list.isFiltered}
        />
      </div>

      {isLoading ? (
        <UserTableSkeleton />
      ) : isError ? (
        <ErrorState
          title={t("errors.listTitle")}
          description={errorMessage(error)}
          onRetry={() => void refetch()}
        />
      ) : users.length === 0 ? (
        // Two genuinely different situations: an empty directory needs a way to
        // add someone, a fruitless search needs a way back to the full list.
        list.isFiltered ? (
          <EmptyState
            icon={SearchX}
            titleKey="users.empty.filteredTitle"
            descriptionKey="users.empty.filteredDescription"
            action={
              <Button variant="outline" onClick={list.reset}>
                {tActions("clearFilters")}
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={Users}
            titleKey="users.empty.title"
            descriptionKey="users.empty.description"
            action={
              <Protected permission={PERMISSION.usersCreate}>
                <Button onClick={() => setDialog({ kind: "create" })}>
                  <UserPlus className="h-4 w-4" />
                  {t("createDialog.title")}
                </Button>
              </Protected>
            }
          />
        )
      ) : (
        <>
          <UserTable
            users={users}
            sortBy={list.query.sortBy}
            sortOrder={list.query.sortOrder}
            onToggleSort={list.toggleSort}
            currentUserId={currentUser?.id ?? null}
            onEdit={(user) => setDialog({ kind: "edit", user })}
            onResetPassword={(user) => setDialog({ kind: "reset", user })}
            onDeactivate={(user) => setDialog({ kind: "deactivate", user })}
            onActivate={handleActivate}
            isRefreshing={isRefreshing}
          />

          <UserPagination
            page={data?.page ?? 1}
            pageSize={data?.pageSize ?? list.query.pageSize}
            totalPages={data?.totalPages ?? 1}
            totalRecords={data?.totalRecords ?? 0}
            onPageChange={list.setPage}
            disabled={isRefreshing}
          />
        </>
      )}

      <CreateUserDialog
        open={dialog.kind === "create"}
        onOpenChange={(open) => (open ? setDialog({ kind: "create" }) : close())}
      />

      <EditUserDialog
        user={dialog.kind === "edit" ? dialog.user : null}
        open={dialog.kind === "edit"}
        onOpenChange={(open) => (open ? undefined : close())}
      />

      <ResetPasswordDialog
        user={dialog.kind === "reset" ? dialog.user : null}
        open={dialog.kind === "reset"}
        onOpenChange={(open) => (open ? undefined : close())}
      />

      <DeactivateUserDialog
        user={dialog.kind === "deactivate" ? dialog.user : null}
        open={dialog.kind === "deactivate"}
        onOpenChange={(open) => (open ? undefined : close())}
      />
    </div>
  );
}
