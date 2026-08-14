"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2 } from "lucide-react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";

import { UserAvatar } from "@/components/users/user-avatar";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useFieldError } from "@/hooks/use-field-error";
import { useSettingsErrorMessage, useUpdateProfile } from "@/hooks/use-settings";
import { roleMessageKey } from "@/types/user";
import {
  emptyToNull,
  profileFormSchema,
  type ProfileFormValues,
} from "@/lib/validation/settings";
import type { Profile } from "@/types/settings";

/**
 * The Profile section.
 *
 * The four fields `20-settings.md` names — full name, profile picture, phone
 * number, job title — and deliberately not a fifth. **Email, role, and status are
 * shown and cannot be edited**: the spec puts email changes out of scope unless
 * the authentication system already supports them (it does not — email is the
 * login identifier), and role and status are administrative decisions about an
 * account rather than preferences its owner holds. They are rendered read-only
 * rather than hidden, because "what am I on this platform?" is a reasonable
 * question to answer on the page where you edit the rest.
 *
 * **This form has a Save button**, unlike every other section on the page, and
 * the difference is worth naming: a preference is one value and saving it
 * immediately loses nothing, while a name is typed a character at a time and a
 * save-per-keystroke would put a hundred requests behind one edit. The
 * distinction is *free text versus a choice*, not *important versus not*.
 *
 * The profile picture is a **reference** — an object-storage key or a URL —
 * following the convention `users.profile_image` has had since User Management.
 * Uploading an avatar would need a storage endpoint, a validation pipeline, and a
 * retention story; none of that is in this spec, and inventing it here would be
 * the Settings module growing a subsystem.
 */

export interface ProfileSettingsFormProps {
  profile: Profile;
}

export function ProfileSettingsForm({ profile }: ProfileSettingsFormProps) {
  const update = useUpdateProfile();
  const t = useTranslations("settings.profile");
  const tRoles = useTranslations("users.roles");
  const errorMessage = useSettingsErrorMessage();
  const fieldError = useFieldError();

  const {
    register,
    handleSubmit,
    reset,
    control,
    formState: { errors, isDirty },
  } = useForm<ProfileFormValues>({
    resolver: zodResolver(profileFormSchema),
    defaultValues: {
      firstName: profile.firstName,
      lastName: profile.lastName,
      phone: profile.phone ?? "",
      jobTitle: profile.jobTitle ?? "",
      profileImage: profile.profileImage ?? "",
    },
  });

  // Re-seed when the server's answer changes (another tab, or the response to
  // this form's own save), so the fields never drift from what is stored.
  React.useEffect(() => {
    reset({
      firstName: profile.firstName,
      lastName: profile.lastName,
      phone: profile.phone ?? "",
      jobTitle: profile.jobTitle ?? "",
      profileImage: profile.profileImage ?? "",
    });
  }, [profile, reset]);

  // `useWatch` rather than the form's `watch()`: the latter returns a function
  // React Compiler cannot memoize safely, so a component using it is skipped for
  // optimization entirely. This subscribes to the two fields and nothing else.
  const [firstName, lastName] = useWatch({
    control,
    name: ["firstName", "lastName"],
  });
  const previewName = `${firstName ?? ""} ${lastName ?? ""}`.trim();

  const onSubmit = handleSubmit((values) => {
    update.mutate(
      {
        firstName: values.firstName,
        lastName: values.lastName,
        // Blank means "remove this", which is how somebody withdraws a phone
        // number they no longer want the platform to hold.
        phone: emptyToNull(values.phone),
        jobTitle: emptyToNull(values.jobTitle),
        profileImage: emptyToNull(values.profileImage),
      },
      {
        onSuccess: () => toast.success(t("saved")),
        onError: (error) => toast.error(errorMessage(error)),
      },
    );
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>

      <form onSubmit={onSubmit} noValidate>
        <CardContent className="flex flex-col gap-6">
          <div className="flex items-center gap-4">
            <UserAvatar
              name={previewName || profile.fullName}
              imageUrl={profile.profileImage}
              className="h-14 w-14"
            />
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">
                {previewName || profile.fullName}
              </p>
              <p className="truncate text-sm text-muted-foreground">{profile.email}</p>
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              id="profile-first-name"
              label={t("firstName")}
              error={fieldError(errors.firstName?.message)}
            >
              <Input
                id="profile-first-name"
                autoComplete="given-name"
                disabled={update.isPending}
                aria-invalid={errors.firstName ? true : undefined}
                {...register("firstName")}
              />
            </Field>

            <Field
              id="profile-last-name"
              label={t("lastName")}
              error={fieldError(errors.lastName?.message)}
            >
              <Input
                id="profile-last-name"
                autoComplete="family-name"
                disabled={update.isPending}
                aria-invalid={errors.lastName ? true : undefined}
                {...register("lastName")}
              />
            </Field>

            <Field
              id="profile-phone"
              label={t("phone")}
              hint={t("phoneHint")}
              error={fieldError(errors.phone?.message)}
            >
              <Input
                id="profile-phone"
                type="tel"
                autoComplete="tel"
                placeholder="+212 6 00 00 00 00"
                disabled={update.isPending}
                aria-invalid={errors.phone ? true : undefined}
                {...register("phone")}
              />
            </Field>

            <Field
              id="profile-job-title"
              label={t("jobTitle")}
              hint={t("jobTitleHint")}
              error={fieldError(errors.jobTitle?.message)}
            >
              <Input
                id="profile-job-title"
                placeholder={t("jobTitlePlaceholder")}
                disabled={update.isPending}
                aria-invalid={errors.jobTitle ? true : undefined}
                {...register("jobTitle")}
              />
            </Field>

            <Field
              id="profile-image"
              label={t("profilePicture")}
              hint={t("profilePictureHint")}
              error={fieldError(errors.profileImage?.message)}
              className="sm:col-span-2"
            >
              <Input
                id="profile-image"
                type="url"
                placeholder="https://…"
                disabled={update.isPending}
                aria-invalid={errors.profileImage ? true : undefined}
                {...register("profileImage")}
              />
            </Field>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <ReadOnly label={t("emailAddress")} value={profile.email} />
            <ReadOnly label={t("role")} value={tRoles(roleMessageKey(profile.role))} />
          </div>
        </CardContent>

        <CardFooter className="justify-end gap-2 border-t pt-6">
          <Button
            type="button"
            variant="ghost"
            disabled={!isDirty || update.isPending}
            onClick={() => reset()}
          >
            {t("discard")}
          </Button>
          <Button type="submit" disabled={!isDirty || update.isPending}>
            {update.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            Save changes
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}

/** One labelled field with its inline validation message. */
function Field({
  id,
  label,
  hint,
  error,
  className,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`flex flex-col gap-1.5 ${className ?? ""}`}>
      <Label htmlFor={id}>{label}</Label>
      {children}
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : hint ? (
        <p className="text-sm text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  );
}

/** A value the owner may see and not change. */
function ReadOnly({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm font-medium">{label}</span>
      <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
        {value}
      </p>
    </div>
  );
}
