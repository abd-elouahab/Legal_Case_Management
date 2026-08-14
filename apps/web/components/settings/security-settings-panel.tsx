"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { zodResolver } from "@hookform/resolvers/zod";
import {
  AlertCircle,
  Eye,
  EyeOff,
  Loader2,
  MonitorSmartphone,
  ShieldAlert,
} from "lucide-react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
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
import { Separator } from "@/components/ui/separator";
import { useDateFormat } from "@/hooks/use-date-format";
import { useFieldError } from "@/hooks/use-field-error";
import {
  useSettingsErrorMessage,
  useChangeSettingsPassword,
  useRevokeOtherSessions,
  useSessions,
} from "@/hooks/use-settings";
import {
  changePasswordFormSchema,
  type ChangePasswordFormValues,
} from "@/lib/validation/settings";
import type { Session } from "@/types/settings";

/**
 * The Account & Security section: change password, and the devices you are on.
 *
 * **Both controls end other sessions, and both say so before they are used.**
 * `20-settings.md`'s Password Change Policy requires that a successful change
 * invalidate every other session; the platform has done that since Authentication
 * shipped, by bumping a generation counter. What this page adds is telling
 * somebody it is about to happen — a security control whose consequence is a
 * surprise is a control people avoid.
 *
 * **The current device stays signed in.** Both calls hand back a replacement
 * token pair, which `lib/api/settings.ts` swaps into the token store before the
 * promise resolves — so neither of these logs the person out of the page they are
 * standing on.
 *
 * **A session is a sign-in, not a credential.** The identifier is stable across
 * the token rotations a browser performs every fifteen minutes, so one laptop
 * appears once rather than as dozens of devices by the end of a day.
 */

export function SecuritySettingsPanel({ mustChangePassword }: { mustChangePassword: boolean }) {
  return (
    <div className="flex flex-col gap-6">
      {mustChangePassword ? (
        <Alert variant="destructive">
          <ShieldAlert className="h-4 w-4" aria-hidden="true" />
          <AlertDescription>
            Your password was set by an administrator. Choose your own before
            continuing.
          </AlertDescription>
        </Alert>
      ) : null}

      <ChangePasswordCard />
      <ActiveSessionsCard />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Password
// --------------------------------------------------------------------------- //

function ChangePasswordCard() {
  const t = useTranslations("settings.security");
  const fieldError = useFieldError();
  const errorMessage = useSettingsErrorMessage();
  const change = useChangeSettingsPassword();
  const [visible, setVisible] = React.useState(false);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<ChangePasswordFormValues>({
    resolver: zodResolver(changePasswordFormSchema),
    defaultValues: { currentPassword: "", newPassword: "", confirmPassword: "" },
  });

  const onSubmit = handleSubmit((values) => {
    change.mutate(
      {
        currentPassword: values.currentPassword,
        // `confirmPassword` is never sent. "Did you type it twice the same" is a
        // question about a form, not about a password, and sending it would be
        // sending a credential the server has no use for.
        newPassword: values.newPassword,
      },
      {
        onSuccess: (message) => {
          toast.success(message);
          reset();
        },
        onError: (error) => toast.error(errorMessage(error)),
      },
    );
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("changePassword")}</CardTitle>
        <CardDescription>
          Changing your password signs you out on every other device. This one stays
          signed in.
        </CardDescription>
      </CardHeader>

      <form onSubmit={onSubmit} noValidate>
        <CardContent className="flex flex-col gap-4">
          <PasswordField
            id="current-password"
            label={t("currentPassword")}
            autoComplete="current-password"
            visible={visible}
            disabled={change.isPending}
            error={fieldError(errors.currentPassword?.message)}
            registration={register("currentPassword")}
          />

          <PasswordField
            id="new-password"
            label={t("newPassword")}
            autoComplete="new-password"
            hint={t("newPasswordHint")}
            visible={visible}
            disabled={change.isPending}
            error={fieldError(errors.newPassword?.message)}
            registration={register("newPassword")}
          />

          <PasswordField
            id="confirm-password"
            label={t("repeatPassword")}
            autoComplete="new-password"
            visible={visible}
            disabled={change.isPending}
            error={fieldError(errors.confirmPassword?.message)}
            registration={register("confirmPassword")}
          />

          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="self-start"
            onClick={() => setVisible((current) => !current)}
          >
            {visible ? (
              <EyeOff className="h-4 w-4" aria-hidden="true" />
            ) : (
              <Eye className="h-4 w-4" aria-hidden="true" />
            )}
            {visible ? t("hidePasswords") : t("showPasswords")}
          </Button>
        </CardContent>

        <CardFooter className="justify-end border-t pt-6">
          <Button type="submit" disabled={change.isPending}>
            {change.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            {t("changePassword")}
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}

function PasswordField({
  id,
  label,
  hint,
  autoComplete,
  visible,
  disabled,
  error,
  registration,
}: {
  id: string;
  label: string;
  hint?: string;
  autoComplete: string;
  visible: boolean;
  disabled: boolean;
  error?: string;
  registration: ReturnType<ReturnType<typeof useForm<ChangePasswordFormValues>>["register"]>;
}) {
  return (
    <div className="flex flex-col gap-1.5 sm:max-w-sm">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={visible ? "text" : "password"}
        autoComplete={autoComplete}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        {...registration}
      />
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

// --------------------------------------------------------------------------- //
// Sessions
// --------------------------------------------------------------------------- //

function ActiveSessionsCard() {
  const t = useTranslations("settings.security");
  const errorMessage = useSettingsErrorMessage();
  const { data, isPending, isError, error } = useSessions();
  const revoke = useRevokeOtherSessions();

  const others = data?.sessions.filter((session) => !session.isCurrent).length ?? 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("activeSessions")}</CardTitle>
        <CardDescription>
          Every device currently signed in to your account. Signing out elsewhere ends
          all of them at once — including any you do not recognise.
        </CardDescription>
      </CardHeader>

      <CardContent className="px-6 py-0">
        {isPending ? (
          <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            {t("loadingSessions")}
          </p>
        ) : isError ? (
          <p role="alert" className="py-6 text-sm text-destructive">
            {errorMessage(error)}
          </p>
        ) : !data.available ? (
          /* `available: false` means the registry could not be read — the list is
             unavailable, not empty. Saying "you have no sessions" here would be
             false, and the two states deserve different sentences. */
          <Alert className="my-6">
            <AlertCircle className="h-4 w-4" aria-hidden="true" />
            <AlertDescription>
              The list of devices is unavailable right now. Signing out of other
              sessions still works — it does not depend on this list.
            </AlertDescription>
          </Alert>
        ) : (
          data.sessions.map((session, index) => (
            <React.Fragment key={session.sessionId}>
              {index > 0 ? <Separator /> : null}
              <SessionRow session={session} />
            </React.Fragment>
          ))
        )}
      </CardContent>

      <CardFooter className="justify-between gap-4 border-t pt-6">
        <p className="text-sm text-muted-foreground">
          {others > 0
            ? `${others} other ${others === 1 ? "device is" : "devices are"} signed in.`
            : t("noOtherDevices")}
        </p>
        <Button
          variant="destructive"
          disabled={revoke.isPending}
          onClick={() =>
            revoke.mutate(undefined, {
              onSuccess: (message) => toast.success(message),
              onError: (failure) => toast.error(errorMessage(failure)),
            })
          }
        >
          {revoke.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : null}
          Sign out everywhere else
        </Button>
      </CardFooter>
    </Card>
  );
}

function SessionRow({ session }: { session: Session }) {
  const t = useTranslations("settings.security");
  const { formatDateTime } = useDateFormat();

  return (
    <div className="flex items-start gap-3 py-4">
      <MonitorSmartphone
        className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm font-medium">
            {session.userAgent ?? t("unknownDevice")}
          </span>
          {session.isCurrent ? <Badge variant="secondary">{t("thisDevice")}</Badge> : null}
        </div>
        <p className="text-sm text-muted-foreground">
          Signed in {formatDateTime(session.createdAt)}
          {session.ipAddress ? ` · ${session.ipAddress}` : ""}
        </p>
        <p className="text-sm text-muted-foreground">
          Last seen {formatDateTime(session.lastSeenAt)}
        </p>
      </div>
    </div>
  );
}
