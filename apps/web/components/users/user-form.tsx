"use client";

import * as React from "react";
import { AlertCircle, Eye, EyeOff } from "lucide-react";
import type { UseFormRegisterReturn } from "react-hook-form";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ROLE_LABELS, STATUS_LABELS, USER_ROLES, USER_STATUSES } from "@/types/user";
import type { UserRole, UserStatus } from "@/types/user";

/**
 * Shared form fields for creating and editing a user.
 *
 * One component rather than two near-identical forms: the two dialogs differ
 * only in whether a password is set, and duplicating the fields would mean every
 * future addition (a locale preference, a department) has to be made twice — and
 * would eventually be made once.
 *
 * It takes **already-registered** inputs rather than a form object, so it stays
 * non-generic while each dialog keeps its own precisely-typed `useForm`. The
 * alternative — a component generic over the form's value type — forces `Path<T>`
 * casts at every field and gives up the type safety it was reaching for.
 *
 * Built entirely from Design System components. Validation is React Hook Form +
 * Zod (`lib/validation/user.ts`); this component only renders the result.
 */

/** Text inputs both forms have. */
export type SharedUserField = "firstName" | "lastName" | "email" | "phone";

/** Every field that can carry a validation message. */
export type UserFieldName = SharedUserField | "password" | "role" | "status";

export interface UserFormFieldsetProps {
  /** Registered text inputs, from the owning form's `register`. */
  fields: Record<SharedUserField, UseFormRegisterReturn>;
  /** Validation messages, keyed by field. Absent means valid. */
  errors: Partial<Record<UserFieldName, string>>;

  role: UserRole;
  onRoleChange: (role: UserRole) => void;
  status: UserStatus;
  onStatusChange: (status: UserStatus) => void;

  /** Supplied by the create form only; omitted, the password fields are hidden. */
  password?: {
    field: UseFormRegisterReturn;
    mustChange: boolean;
    onMustChangeChange: (value: boolean) => void;
  };

  disabled?: boolean;
  /** A server-side failure that is not tied to one field. */
  formError?: string | null;
  /** Prefix for input ids, so two forms can coexist on one page. */
  idPrefix: string;
}

/** One labelled field with its inline validation message. */
function Field({
  id,
  label,
  error,
  hint,
  children,
}: {
  id: string;
  label: string;
  error?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-sm text-destructive">
          {error}
        </p>
      ) : hint ? (
        <p className="text-xs text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  );
}

export function UserFormFieldset({
  fields,
  errors,
  role,
  onRoleChange,
  status,
  onStatusChange,
  password,
  disabled = false,
  formError,
  idPrefix,
}: UserFormFieldsetProps) {
  const [showPassword, setShowPassword] = React.useState(false);

  const id = (field: string) => `${idPrefix}-${field}`;
  const describedBy = (field: UserFieldName, elementId: string) =>
    errors[field] ? `${elementId}-error` : undefined;

  return (
    <div className="flex flex-col gap-5">
      {formError ? (
        <Alert variant="destructive" role="alert">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{formError}</AlertDescription>
        </Alert>
      ) : null}

      <div className="grid gap-5 sm:grid-cols-2">
        <Field id={id("first-name")} label="First name" error={errors.firstName}>
          <Input
            id={id("first-name")}
            autoComplete="given-name"
            placeholder="Amina"
            disabled={disabled}
            aria-invalid={Boolean(errors.firstName)}
            aria-describedby={describedBy("firstName", id("first-name"))}
            {...fields.firstName}
          />
        </Field>

        <Field id={id("last-name")} label="Last name" error={errors.lastName}>
          <Input
            id={id("last-name")}
            autoComplete="family-name"
            placeholder="Benali"
            disabled={disabled}
            aria-invalid={Boolean(errors.lastName)}
            aria-describedby={describedBy("lastName", id("last-name"))}
            {...fields.lastName}
          />
        </Field>
      </div>

      <Field id={id("email")} label="Email" error={errors.email}>
        <Input
          id={id("email")}
          type="email"
          inputMode="email"
          autoComplete="email"
          placeholder="amina.benali@example.com"
          disabled={disabled}
          aria-invalid={Boolean(errors.email)}
          aria-describedby={describedBy("email", id("email"))}
          {...fields.email}
        />
      </Field>

      <Field id={id("phone")} label="Phone" error={errors.phone} hint="Optional.">
        <Input
          id={id("phone")}
          type="tel"
          inputMode="tel"
          autoComplete="tel"
          placeholder="+212 612 345 678"
          disabled={disabled}
          aria-invalid={Boolean(errors.phone)}
          aria-describedby={describedBy("phone", id("phone"))}
          {...fields.phone}
        />
      </Field>

      {password ? (
        <Field
          id={id("password")}
          label="Password"
          error={errors.password}
          hint="The user can change it once they sign in."
        >
          <div className="relative">
            <Input
              id={id("password")}
              type={showPassword ? "text" : "password"}
              autoComplete="new-password"
              placeholder="Choose an initial password"
              className="pr-10"
              disabled={disabled}
              aria-invalid={Boolean(errors.password)}
              aria-describedby={describedBy("password", id("password"))}
              {...password.field}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => setShowPassword((visible) => !visible)}
              disabled={disabled}
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              className="absolute right-1 top-1/2 h-8 w-8 -translate-y-1/2 text-muted-foreground"
            >
              {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </Button>
          </div>
        </Field>
      ) : null}

      <div className="grid gap-5 sm:grid-cols-2">
        <Field id={id("role")} label="Role" error={errors.role}>
          <Select
            value={role}
            disabled={disabled}
            onValueChange={(value) => onRoleChange(value as UserRole)}
          >
            <SelectTrigger id={id("role")} aria-invalid={Boolean(errors.role)}>
              <SelectValue placeholder="Select a role" />
            </SelectTrigger>
            <SelectContent>
              {USER_ROLES.map((option) => (
                <SelectItem key={option} value={option}>
                  {ROLE_LABELS[option]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        <Field id={id("status")} label="Status" error={errors.status}>
          <Select
            value={status}
            disabled={disabled}
            onValueChange={(value) => onStatusChange(value as UserStatus)}
          >
            <SelectTrigger id={id("status")} aria-invalid={Boolean(errors.status)}>
              <SelectValue placeholder="Select a status" />
            </SelectTrigger>
            <SelectContent>
              {USER_STATUSES.map((option) => (
                <SelectItem key={option} value={option}>
                  {STATUS_LABELS[option]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
      </div>

      {password ? (
        <div className="flex items-start gap-3 rounded-lg border border-border p-3">
          <Checkbox
            id={id("must-change")}
            checked={password.mustChange}
            disabled={disabled}
            onCheckedChange={(checked) => password.onMustChangeChange(checked === true)}
          />
          <div className="flex flex-col gap-0.5">
            <Label htmlFor={id("must-change")} className="cursor-pointer">
              Require a password change at first sign-in
            </Label>
            <p className="text-xs text-muted-foreground">
              The user keeps the password above only until they choose their own.
            </p>
          </div>
        </div>
      ) : null}
    </div>
  );
}
