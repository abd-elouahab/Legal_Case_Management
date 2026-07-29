"use client";

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { UserPlus } from "lucide-react";
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
import { fieldErrors, useCreateUser, userErrorMessage } from "@/hooks/use-users";
import { createUserFormSchema, type CreateUserFormValues } from "@/lib/validation/user";

/**
 * Create User dialog.
 *
 * Owns nothing but presentation and form state: the request, cache
 * invalidation, and error translation all live in `useCreateUser` /
 * `userErrorMessage`, per the standard that components carry no business logic.
 *
 * Server-side validation failures are mapped back onto the offending fields
 * rather than shown only as a banner, so a rejected email lands next to the email
 * input — the same place the client-side rule would have complained.
 */

const EMPTY_FORM: CreateUserFormValues = {
  firstName: "",
  lastName: "",
  email: "",
  phone: "",
  password: "",
  role: "lawyer",
  status: "active",
  mustChangePassword: false,
};

export function CreateUserDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const createUser = useCreateUser();
  const [formError, setFormError] = React.useState<string | null>(null);

  const form = useForm<CreateUserFormValues>({
    resolver: zodResolver(createUserFormSchema),
    defaultValues: EMPTY_FORM,
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

  // `useWatch` rather than `watch()`: it is a real hook, so the React Compiler
  // can memoize this component instead of skipping it, and it re-renders only
  // for the fields named here rather than for every keystroke in the form.
  const role = useWatch({ control, name: "role" });
  const status = useWatch({ control, name: "status" });
  const mustChangePassword = useWatch({ control, name: "mustChangePassword" });

  // Drop a stale server error as the dialog reopens. Adjusted during render
  // rather than in an effect, so the previous attempt's banner is never painted
  // again (https://react.dev/learn/you-might-not-need-an-effect).
  const [wasOpen, setWasOpen] = React.useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setFormError(null);
  }

  // Start from a clean form each time the dialog opens; a half-filled draft from
  // a cancelled attempt is more confusing than helpful. This one *is* an effect:
  // `reset` mutates React Hook Form's store, which is exactly the external-system
  // synchronization effects are for.
  React.useEffect(() => {
    if (open) reset(EMPTY_FORM);
  }, [open, reset]);

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null);

    try {
      const user = await createUser.mutateAsync({
        email: values.email,
        firstName: values.firstName,
        lastName: values.lastName,
        password: values.password,
        phone: values.phone || null,
        role: values.role,
        status: values.status,
        mustChangePassword: values.mustChangePassword,
      });

      toast.success(`${user.fullName} was added.`);
      onOpenChange(false);
    } catch (error) {
      const fields = fieldErrors(error);
      for (const [field, message] of Object.entries(fields)) {
        setError(field as keyof CreateUserFormValues, { message });
      }
      // Only surface the banner when nothing could be attached to a field —
      // otherwise the same complaint would appear twice.
      if (Object.keys(fields).length === 0) setFormError(userErrorMessage(error));
    }
  });

  const isPending = createUser.isPending;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add user</DialogTitle>
          <DialogDescription>
            Create an account and assign its role. The user signs in with the email
            and password you set here.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-6">
          <UserFormFieldset
            idPrefix="create-user"
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
              password: errors.password?.message,
              role: errors.role?.message,
              status: errors.status?.message,
            }}
            role={role}
            onRoleChange={(value) => setValue("role", value, { shouldValidate: true })}
            status={status}
            onStatusChange={(value) => setValue("status", value, { shouldValidate: true })}
            password={{
              field: register("password"),
              mustChange: mustChangePassword,
              onMustChangeChange: (value) => setValue("mustChangePassword", value),
            }}
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
                  Creating…
                </>
              ) : (
                <>
                  <UserPlus className="h-4 w-4" />
                  Create user
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
