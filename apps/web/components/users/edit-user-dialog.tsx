"use client";

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { Save } from "lucide-react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/shared/spinner";
import { UserFormFieldset } from "@/components/users/user-form";
import { fieldErrors, useUpdateUser, userErrorMessage } from "@/hooks/use-users";
import { editUserFormSchema, type EditUserFormValues } from "@/lib/validation/user";
import type { ManagedUser } from "@/types/user";
import type { UpdateUserPayload } from "@/types/user-management";

/**
 * Edit User dialog.
 *
 * Deliberately sends **only the fields that changed**. A PATCH that echoed every
 * field back would work, but it would also overwrite a concurrent edit by another
 * administrator with values this dialog loaded before that edit happened. Sending
 * a diff keeps an unrelated change intact.
 *
 * There is no password field: changing a password revokes sessions, so it goes
 * through the reset action instead.
 */

function toFormValues(user: ManagedUser): EditUserFormValues {
  return {
    firstName: user.firstName,
    lastName: user.lastName,
    email: user.email,
    phone: user.phone ?? "",
    role: user.role,
    status: user.status,
  };
}

/** The fields whose value differs from the record being edited. */
function changedFields(user: ManagedUser, values: EditUserFormValues): UpdateUserPayload {
  const payload: UpdateUserPayload = {};

  if (values.firstName !== user.firstName) payload.firstName = values.firstName;
  if (values.lastName !== user.lastName) payload.lastName = values.lastName;
  if (values.email !== user.email) payload.email = values.email;
  if (values.role !== user.role) payload.role = values.role;
  if (values.status !== user.status) payload.status = values.status;

  // An emptied phone field is a deliberate clear, which the API expresses as an
  // explicit null — distinct from omitting the key, which means "leave it".
  const phone = values.phone === "" ? null : values.phone;
  if (phone !== (user.phone ?? null)) payload.phone = phone;

  return payload;
}

export function EditUserDialog({
  user,
  open,
  onOpenChange,
}: {
  user: ManagedUser | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const updateUser = useUpdateUser();
  const [formError, setFormError] = React.useState<string | null>(null);

  const form = useForm<EditUserFormValues>({
    resolver: zodResolver(editUserFormSchema),
    defaultValues: {
      firstName: "",
      lastName: "",
      email: "",
      phone: "",
      role: "lawyer",
      status: "active",
    },
    mode: "onBlur",
  });

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    setValue,
    formState: { errors },
  } = form;

  // See the note in `create-user-dialog.tsx`: `useWatch` keeps this component
  // memoizable and limits re-renders to the two fields that need them.
  const role = useWatch({ control, name: "role" });
  const status = useWatch({ control, name: "status" });

  // Drop a stale server error when the dialog is opened on a (possibly
  // different) user. Adjusted during render rather than in an effect, so the
  // previous attempt's banner is never painted again
  // (https://react.dev/learn/you-might-not-need-an-effect).
  const target = open ? (user?.id ?? null) : null;
  const [previousTarget, setPreviousTarget] = React.useState(target);
  if (target !== previousTarget) {
    setPreviousTarget(target);
    if (target) setFormError(null);
  }

  // Reload the form whenever a different user is opened, so the dialog never
  // shows the previous target's details. An effect because `reset` mutates React
  // Hook Form's store — the external-system synchronization effects exist for.
  React.useEffect(() => {
    if (open && user) reset(toFormValues(user));
  }, [open, user, reset]);

  const onSubmit = handleSubmit(async (values) => {
    if (!user) return;
    setFormError(null);

    const payload = changedFields(user, values);

    if (Object.keys(payload).length === 0) {
      // The API rejects an empty PATCH; there is genuinely nothing to save.
      toast.info("No changes to save.");
      onOpenChange(false);
      return;
    }

    try {
      const updated = await updateUser.mutateAsync({ id: user.id, payload });
      toast.success(`${updated.fullName} was updated.`);
      onOpenChange(false);
    } catch (error) {
      const fields = fieldErrors(error);
      for (const [field, message] of Object.entries(fields)) {
        setError(field as keyof EditUserFormValues, { message });
      }
      if (Object.keys(fields).length === 0) setFormError(userErrorMessage(error));
    }
  });

  const isPending = updateUser.isPending;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit user</DialogTitle>
          <DialogDescription>
            {user
              ? `Update ${user.fullName}'s details, role, or account status.`
              : "Update this user's details."}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-6">
          <UserFormFieldset
            idPrefix="edit-user"
            fields={{
              firstName: register("firstName"),
              lastName: register("lastName"),
              email: register("email"),
              phone: register("phone"),
            }}
            errors={{
              firstName: errors.firstName?.message,
              lastName: errors.lastName?.message,
              email: errors.email?.message,
              phone: errors.phone?.message,
              role: errors.role?.message,
              status: errors.status?.message,
            }}
            role={role}
            onRoleChange={(value) => setValue("role", value, { shouldValidate: true })}
            status={status}
            onStatusChange={(value) => setValue("status", value, { shouldValidate: true })}
            disabled={isPending}
            formError={formError}
          />

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending ? (
                <>
                  <Spinner className="h-4 w-4 text-current" />
                  Saving…
                </>
              ) : (
                <>
                  <Save className="h-4 w-4" />
                  Save changes
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
