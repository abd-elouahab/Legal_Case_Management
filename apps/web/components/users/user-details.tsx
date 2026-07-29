"use client";

import * as React from "react";
import Link from "next/link";
import {
  ArrowLeft,
  KeyRound,
  Mail,
  Pencil,
  Phone,
  UserCheck,
  UserX,
} from "lucide-react";
import { toast } from "sonner";

import { Protected } from "@/components/auth/protected";
import { ErrorState } from "@/components/shared/error-state";
import { LoadingState } from "@/components/shared/loading-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { DeactivateUserDialog } from "@/components/users/deactivate-user-dialog";
import { EditUserDialog } from "@/components/users/edit-user-dialog";
import { ResetPasswordDialog } from "@/components/users/reset-password-dialog";
import { UserAvatar } from "@/components/users/user-avatar";
import { UserRoleBadge, UserStatusBadge } from "@/components/users/user-badges";
import { useSession } from "@/hooks/use-session";
import { useActivateUser, useUser, userErrorMessage } from "@/hooks/use-users";
import { formatDateTime } from "@/lib/format";
import { ROUTES } from "@/lib/routes";
import { PERMISSION } from "@/types/authorization";
import type { ManagedUser } from "@/types/user";

/**
 * One user's full record.
 *
 * Groups the fields the way an administrator reads them — who this person is,
 * how to reach them, what they may do, and what has happened to the account —
 * rather than in the order the API happens to return them.
 */

type DialogKind = "none" | "edit" | "reset" | "deactivate";

/** One label/value pair inside a details card. */
function DetailRow({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
}) {
  return (
    <div className="flex flex-col gap-1 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
      <dt className="flex items-center gap-2 text-sm text-muted-foreground">
        {Icon ? <Icon className="h-4 w-4" /> : null}
        {label}
      </dt>
      <dd className="text-sm text-foreground sm:text-right">{value}</dd>
    </div>
  );
}

function DetailsCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="divide-y divide-border">{children}</dl>
      </CardContent>
    </Card>
  );
}

function UserDetailsContent({ user }: { user: ManagedUser }) {
  const { user: currentUser } = useSession();
  const activateUser = useActivateUser();
  const [dialog, setDialog] = React.useState<DialogKind>("none");

  const isSelf = currentUser?.id === user.id;

  async function activate() {
    try {
      await activateUser.mutateAsync(user.id);
      toast.success(`${user.fullName} was activated.`);
    } catch (error) {
      toast.error(userErrorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Button asChild variant="ghost" size="sm" className="w-fit -ml-2">
        <Link href={ROUTES.users}>
          <ArrowLeft className="h-4 w-4" />
          Back to users
        </Link>
      </Button>

      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-center gap-4">
          <UserAvatar name={user.fullName} imageUrl={user.profileImage} className="size-14" />
          <div className="flex flex-col gap-1.5">
            <h2 className="text-xl font-semibold text-foreground">{user.fullName}</h2>
            <div className="flex flex-wrap items-center gap-2">
              <UserRoleBadge role={user.role} />
              <UserStatusBadge status={user.status} />
              {user.mustChangePassword ? (
                <Badge variant="outline" className="border-warning/30 bg-warning/10 text-warning">
                  Password change required
                </Badge>
              ) : null}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Protected permission={PERMISSION.usersUpdate}>
            <Button variant="outline" onClick={() => setDialog("edit")}>
              <Pencil className="h-4 w-4" />
              Edit
            </Button>
            <Button variant="outline" onClick={() => setDialog("reset")}>
              <KeyRound className="h-4 w-4" />
              Reset password
            </Button>
          </Protected>

          {isSelf ? null : user.isActive ? (
            <Protected permission={PERMISSION.usersDelete}>
              <Button variant="destructive" onClick={() => setDialog("deactivate")}>
                <UserX className="h-4 w-4" />
                Deactivate
              </Button>
            </Protected>
          ) : (
            <Protected permission={PERMISSION.usersUpdate}>
              <Button onClick={activate} disabled={activateUser.isPending}>
                <UserCheck className="h-4 w-4" />
                Activate
              </Button>
            </Protected>
          )}
        </div>
      </div>

      <Separator />

      <div className="grid gap-6 lg:grid-cols-2">
        <DetailsCard title="Personal information">
          <DetailRow label="First name" value={user.firstName} />
          <DetailRow label="Last name" value={user.lastName} />
        </DetailsCard>

        <DetailsCard title="Contact information">
          <DetailRow
            label="Email"
            icon={Mail}
            value={
              <a href={`mailto:${user.email}`} className="hover:underline">
                {user.email}
              </a>
            }
          />
          <DetailRow
            label="Phone"
            icon={Phone}
            value={
              user.phone ? (
                <a href={`tel:${user.phone}`} className="hover:underline">
                  {user.phone}
                </a>
              ) : (
                <span className="text-muted-foreground">Not provided</span>
              )
            }
          />
        </DetailsCard>

        <DetailsCard title="Access">
          <DetailRow label="Role" value={<UserRoleBadge role={user.role} />} />
          <DetailRow label="Status" value={<UserStatusBadge status={user.status} />} />
          <DetailRow
            label="Can sign in"
            value={user.isActive ? "Yes" : "No"}
          />
        </DetailsCard>

        <DetailsCard title="Account activity">
          <DetailRow label="Last sign-in" value={formatDateTime(user.lastLoginAt, "Never")} />
          <DetailRow label="Created" value={formatDateTime(user.createdAt)} />
          <DetailRow label="Last updated" value={formatDateTime(user.updatedAt)} />
        </DetailsCard>
      </div>

      <EditUserDialog
        user={user}
        open={dialog === "edit"}
        onOpenChange={(open) => (open ? undefined : setDialog("none"))}
      />
      <ResetPasswordDialog
        user={user}
        open={dialog === "reset"}
        onOpenChange={(open) => (open ? undefined : setDialog("none"))}
      />
      <DeactivateUserDialog
        user={user}
        open={dialog === "deactivate"}
        onOpenChange={(open) => (open ? undefined : setDialog("none"))}
      />
    </div>
  );
}

export function UserDetails({ userId }: { userId: string }) {
  const { data, isLoading, isError, error, refetch } = useUser(userId);

  if (isLoading) return <LoadingState label="Loading user…" />;

  if (isError || !data) {
    return (
      <ErrorState
        title="Could not load this user"
        description={userErrorMessage(error)}
        onRetry={() => void refetch()}
      />
    );
  }

  return <UserDetailsContent user={data} />;
}
